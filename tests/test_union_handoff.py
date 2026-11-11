"""Unit and end-to-end tests for the union-handoff skill's scripts, against
the fake machine in evals/fixtures/handoff_world.py."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

from evals.fixtures import handoff_world as world

REPO = Path(__file__).resolve().parents[1]
SCRIPTS = REPO / "plugins/union/skills/union-handoff/scripts"
sys.path.insert(0, str(SCRIPTS))

import harnesses as hz  # noqa: E402
import inventory as inv  # noqa: E402
import seal as sl  # noqa: E402
import stage as stg  # noqa: E402


@pytest.fixture()
def machine(tmp_path, monkeypatch):
    v = world.build(tmp_path)
    monkeypatch.setenv("HOME", str(v["home"]))
    for k in ("CODEX_HOME", "CLAUDE_CONFIG_DIR", "PI_CODING_AGENT_DIR", "HERMES_HOME", "OPENCODE_DB", "XDG_DATA_HOME", "KUBECONFIG"):
        monkeypatch.delenv(k, raising=False)
    for k, val in world.ENV.items():
        monkeypatch.setenv(k, val)
    return v


def run_cli(machine, *args: str) -> subprocess.CompletedProcess:
    env = {k: v for k, v in os.environ.items()}
    return subprocess.run([sys.executable, str(SCRIPTS / "handoff.py"), *args], cwd=machine["home"],
                          env=env, text=True, capture_output=True)


# --------------------------------------------------------------------------- #
# Discovery


def test_discover_finds_every_harness(machine):
    by = {s.key: s for s in hz.discover()}
    for h in ("claude", "codex", "pi", "opencode", "hermes"):
        assert machine[h] in by, (h, list(by))
    assert by[machine["claude"]].cwd == str(machine["webapp"])
    assert by[machine["claude"]].title == "Login page and deploy"
    assert by[machine["codex"]].cwd == str(machine["infra"])
    assert by[machine["opencode"]].cwd == str(machine["infra"])
    assert by[machine["hermes"]].cwd == str(machine["webapp"])
    assert by[machine["pi"]].cwd == str(machine["webapp"])


def test_parsers_extract_commands_and_paths(machine):
    pool = hz.discover()
    claude = hz.parse(hz.find(machine["claude"], pool))
    assert "gh pr list && kubectl get pods -n web | jq ." in claude.commands
    assert str(machine["infra"] / "app.py") in claude.paths
    assert claude.first_user == "Add a login page and deploy it"
    assert claude.last_user == "Now run the tests"
    codex = hz.parse(hz.find(machine["codex"], pool))
    assert codex.commands == ["terraform init -upgrade", "terraform plan | tee plan.txt"]
    assert hz.parse(hz.find(machine["pi"], pool)).commands == ["npx prettier --write ."]
    assert hz.parse(hz.find(machine["opencode"], pool)).commands == ["openssl req -new"]
    assert hz.parse(hz.find(machine["hermes"], pool)).commands == ["mkdocs build"]


def test_find_by_prefix_and_ambiguity(machine):
    pool = hz.discover()
    assert hz.find("claude:1111", pool).id == world.CLAUDE_ID
    with pytest.raises(LookupError):
        hz.find("nope:123", pool)


def test_codex_zstd_rollout(machine, tmp_path):
    zstd = __import__("shutil").which("zstd")
    if not zstd:
        pytest.skip("zstd CLI not installed")
    src = next(Path(machine["home"], ".codex/sessions").rglob("*.jsonl"))
    subprocess.run([zstd, "-q", "--rm", str(src)], check=True)
    s = hz.find(machine["codex"])
    assert s.path.name.endswith(".jsonl.zst")
    assert s.cwd == str(machine["infra"])
    assert hz.parse(s).commands[0] == "terraform init -upgrade"


def test_command_names():
    cases = {
        "cd /x && gh pr view 1 | jq .": ["cd", "gh", "jq"],
        'FOO=1 env -u A python3 -c "import x\nprint(1)"': ["python3"],
        "cat > f <<'EOF'\nawait notacommand\nEOF\nkubectl get pods": ["cat", "kubectl"],
        "git -C /a status; timeout 30 uv run pytest": ["git", "uv"],
        "echo $(helm version) && rg x  # comment": ["echo", "helm", "rg"],
        "sudo -u bob make \\\n  build": ["make"],
    }
    for cmd, want in cases.items():
        assert inv.command_names(cmd) == want, cmd


# --------------------------------------------------------------------------- #
# Report


def test_report_cli(machine):
    proc = run_cli(machine, "report", "--json", "--session", machine["claude"], "--session", machine["codex"], "--session", machine["pi"])
    assert proc.returncode == 0, proc.stderr
    assert world.FAKE_SECRET not in proc.stdout + proc.stderr
    rep = json.loads(proc.stdout)
    repos = {r["path"]: r for r in rep["repos"]}
    web = repos[str(machine["webapp"])]
    assert web["branch"] == "feature/login"
    assert web["unpushed_commits"] == 1
    assert web["modified_files"] == 2
    assert web["secret_files"] == [".env"]
    assert str(machine["infra"]) in repos  # reached only through the Edit tool
    tools = {t["name"]: t for t in rep["tools"]}
    assert {"gh", "kubectl", "helm", "terraform", "uv", "npx"} <= set(tools)
    assert "notacommand" not in tools and "await" not in tools  # heredoc bodies are data
    creds = {c["id"]: c for c in rep["credentials"]}
    assert {"gh", "aws", "claude-login", "repo:webapp/.env"} <= set(creds)
    assert creds["claude-login"]["copyable"] is False
    assert "MY_SERVICE_API_KEY" in rep["flagged_env_names"]
    arrives = {s["key"]: s["arrives"] for s in rep["sessions"]}
    assert arrives[machine["claude"]] == "resumable"
    assert arrives[machine["codex"]] == "resumable"
    assert arrives[machine["pi"]] == "handoff note"


def test_report_text_never_prints_secrets(machine):
    proc = run_cli(machine, "report", *sum((["--session", machine[h]] for h in ("claude", "codex", "pi", "opencode", "hermes")), []))
    assert proc.returncode == 0, proc.stderr
    assert world.FAKE_SECRET not in proc.stdout + proc.stderr
    for heading in ("SESSIONS", "REPOS", "TOOLS", "CREDENTIALS", "SIZE"):
        assert heading in proc.stdout


def test_remote_harness_changes_how_sessions_arrive(machine):
    proc = run_cli(machine, "report", "--json", "--remote-harness", "codex", "--session", machine["claude"])
    assert json.loads(proc.stdout)["sessions"][0]["arrives"] == "handoff note"


# --------------------------------------------------------------------------- #
# Path re-keying


def test_path_map_longest_prefix():
    pm = stg.PathMap()
    pm.add("/Users/a", "/root")
    pm.add("/Users/a/code/web", "/root/src/web")
    assert pm.map("/Users/a/code/web/sub") == "/root/src/web/sub"
    assert pm.map("/Users/a/code/web") == "/root/src/web"
    assert pm.map("/Users/a/code/webapp") == "/root/code/webapp"  # not a prefix match on "web"
    assert pm.map("/opt/x") == "/opt/x"


def test_rewrite_cwds_touches_only_cwd_fields():
    pm = stg.PathMap()
    pm.add("/Users/a/web", "/root/src/web")
    rec = {"cwd": "/Users/a/web", "message": {"content": [{"type": "thinking", "thinking": "/Users/a/web", "signature": "s"}]},
           "payload": {"workspace_roots": ["/Users/a/web"], "cwd": "/Users/a/web/x"}}
    out = stg.rewrite_cwds(rec, pm)
    assert out["cwd"] == "/root/src/web"
    assert out["payload"] == {"workspace_roots": ["/root/src/web"], "cwd": "/root/src/web/x"}
    assert out["message"] == rec["message"]


def test_claude_slug():
    assert hz.claude_slug("/root/src/my_repo.v2") == "-root-src-my-repo-v2"


# --------------------------------------------------------------------------- #
# Stage


def stage_all(machine, tmp_path, remote, *, creds=()):
    pool = hz.discover()
    sessions = [hz.find(machine[h], pool) for h in ("claude", "codex", "pi", "opencode", "hermes")]
    out = tmp_path / "out"
    manifest = stg.stage(sessions, stg.StageOptions(out=out, remote_harness=remote, credentials=set(creds), log=lambda m: None))
    return out / "home", manifest


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(["git", "-C", str(repo), *args], text=True, capture_output=True, check=True).stdout


def test_stage_for_claude(machine, tmp_path):
    home, manifest = stage_all(machine, tmp_path, "claude")

    # Repos: branch, history, staged and unstaged work, untracked files.
    web = home / "src/webapp"
    assert _git(web, "rev-parse", "--abbrev-ref", "HEAD").strip() == "feature/login"
    assert _git(web, "log", "--format=%s").split("\n")[:2] == ["add login", "initial"]
    assert _git(web, "diff", "--cached", "--name-only").split() == ["README.md"]
    assert _git(web, "diff", "--name-only").split() == ["app.py"]
    assert (web / "notes.txt").read_text() == "untracked notes\n"
    assert not (web / ".env").exists()  # a secret file stays behind unless picked
    assert _git(web, "remote", "get-url", "origin").strip() == "https://github.com/example/webapp.git"
    assert "ahead 1" in _git(web, "status", "-sb").splitlines()[0]
    assert (home / "src/infra/app.py").exists()

    # The Claude transcript: re-keyed directory and cwd fields, nothing else.
    t = home / ".claude/projects/-root-src-webapp" / f"{world.CLAUDE_ID}.jsonl"
    recs = [json.loads(line) for line in t.read_text().splitlines()]
    assert {r["cwd"] for r in recs if "cwd" in r} == {"/root/src/webapp"}
    thinking = recs[2]["message"]["content"][0]
    assert thinking["signature"] == world.SIGNATURE
    assert thinking["thinking"] == f"The repo is at {machine['webapp']}."
    assert (t.parent / world.CLAUDE_ID / "subagents/agent-1.jsonl").exists()
    assert (t.parent / "memory/MEMORY.md").exists()
    assert (home / ".claude/CLAUDE.md").read_text().startswith("Always run tests")
    assert (home / ".claude/skills/my-skill/SKILL.md").exists()

    # Only the Claude session resumes; the rest arrive as handoff notes.
    rows = json.loads((home / ".agent-sessions/finished.json").read_text())["sessions"]
    assert len(rows) == 1
    assert rows[0]["spec"]["resume_session_id"] == world.CLAUDE_ID
    assert rows[0]["spec"]["cwd"] == "src/webapp"
    assert rows[0]["agent"] == "claude"
    for name in ("upgrade-terraform-providers", "fix-the-css", "rotate-certs", "write-docs"):
        assert (home / "handoff" / name / "HANDOFF.md").exists(), name
    note = (home / "handoff/upgrade-terraform-providers/HANDOFF.md").read_text()
    assert "/root/src/infra" in note and "Upgrade terraform providers" in note

    # Setup script, pointer, and no credentials unless picked.
    setup = (home / ".handoff/setup.sh").read_text()
    assert "need terraform" in setup and "need gh" in setup
    subprocess.run(["bash", "-n", str(home / ".handoff/setup.sh")], check=True)
    assert "setup.sh" in (home / "CLAUDE.md").read_text()
    assert not (home / ".config/gh").exists() and not (home / ".aws").exists()
    assert not (home / ".claude/.credentials.json").exists()
    staged_text = "".join(f.read_text(errors="replace") for f in home.rglob("*") if f.is_file() and ".git/" not in str(f))
    assert world.FAKE_SECRET not in staged_text


def test_stage_copies_only_picked_credentials(machine, tmp_path):
    home, manifest = stage_all(machine, tmp_path, "claude", creds=["gh", "repo:webapp/.env"])
    gh = home / ".config/gh/hosts.yml"
    assert gh.exists() and oct(gh.stat().st_mode & 0o777) == "0o600"
    assert (home / "src/webapp/.env").exists()
    assert not (home / ".aws").exists()
    assert set(manifest["credentials_copied"]) == {"gh", "repo:webapp/.env"}


def test_stage_refuses_agent_logins(machine, tmp_path):
    with pytest.raises(SystemExit):
        stage_all(machine, tmp_path, "claude", creds=["claude-login"])


def test_stage_for_codex(machine, tmp_path):
    home, _ = stage_all(machine, tmp_path, "codex")
    rollouts = list((home / ".agent-sessions/codex/sessions" / world.CODEX_ID / "sessions/2026/11/02").glob("rollout-*.jsonl"))
    assert len(rollouts) == 1
    recs = [json.loads(line) for line in rollouts[0].read_text().splitlines()]
    assert recs[0]["payload"]["cwd"] == "/root/src/infra"
    assert recs[1]["payload"]["workspace_roots"] == ["/root/src/infra"]
    rows = json.loads((home / ".agent-sessions/finished.json").read_text())["sessions"]
    assert [(r["agent"], r["spec"]["resume_session_id"], r["spec"]["cwd"]) for r in rows] == [("codex", world.CODEX_ID, "src/infra")]
    assert (home / "handoff/login-page-and-deploy/HANDOFF.md").exists()
    assert (home / ".agents/skills/my-skill/SKILL.md").exists()
    assert "setup.sh" in (home / "AGENTS.md").read_text()


def test_stage_refuses_non_empty_out(machine, tmp_path):
    (tmp_path / "out").mkdir()
    (tmp_path / "out" / "x").write_text("x")
    with pytest.raises(SystemExit):
        stage_all(machine, tmp_path, "claude")


def test_stage_cli_end_to_end(machine, tmp_path):
    out = tmp_path / "cli-out"
    proc = run_cli(machine, "stage", "--session", machine["claude"], "--out", str(out), "--remote-harness", "claude", "--remote-home", "/home/agent", "--json")
    assert proc.returncode == 0, proc.stderr
    m = json.loads(proc.stdout)
    assert m["remote_home"] == "/home/agent"
    assert (out / "home/.claude/projects/-home-agent-src-webapp" / f"{world.CLAUDE_ID}.jsonl").exists()


# --------------------------------------------------------------------------- #
# Seal


def test_seal_commands():
    o = sl.SealOptions(staged_home=Path("/x"), volume="v", artifact="a", bucket="s3://b/vols", project="p", domain="d")
    assert sl.command(o, Path("/m")) == ["flyte", "create", "volume", "v", "--mount", "/m", "--artifact", "a",
                                         "--bucket", "s3://b/vols", "--project", "p", "--domain", "d"]
    m = sl.SealOptions(staged_home=Path("/x"), merge_artifact="home", fork_name="home-handoff-1")
    assert sl.command(m, Path("/m")) == ["flyte", "mount", "volume", "/m", "--artifact", "home", "--fork-name", "home-handoff-1"]


def test_copy_into_merge_never_overwrites(tmp_path):
    src, dest = tmp_path / "src", tmp_path / "dest"
    (src / ".agent-sessions").mkdir(parents=True)
    (src / ".agent-sessions/finished.json").write_text(json.dumps({"sessions": [{"key": "handoff:new", "name": "new"}]}))
    (src / "CLAUDE.md").write_text(stg.HANDOFF_POINTER)
    (src / "notes.txt").write_text("ours")
    (src / "fresh.txt").write_text("fresh")
    (dest / ".agent-sessions").mkdir(parents=True)
    (dest / ".agent-sessions/finished.json").write_text(json.dumps({"sessions": [{"key": "r:old", "name": "old"}]}))
    (dest / "CLAUDE.md").write_text("# Theirs\n")
    (dest / "notes.txt").write_text("theirs")
    r = sl.copy_into(src, dest, merge=True)
    assert (dest / "notes.txt").read_text() == "theirs"
    assert r["skipped_existing"] == ["notes.txt"]
    assert (dest / "fresh.txt").read_text() == "fresh"
    keys = [x["key"] for x in json.loads((dest / ".agent-sessions/finished.json").read_text())["sessions"]]
    assert keys == ["r:old", "handoff:new"]
    claude_md = (dest / "CLAUDE.md").read_text()
    assert claude_md.startswith("# Theirs") and "setup.sh" in claude_md


FAKE_FLYTE = textwrap.dedent("""\
    #!/usr/bin/env python3
    # Stands in for `flyte create volume NAME --mount DIR`: "mounts" by writing
    # a marker, then on SIGINT reports what was in DIR and a locator.
    import os, signal, sys, time
    argv = sys.argv[1:]
    mnt = argv[argv.index("--mount") + 1] if "--mount" in argv else argv[2]
    open(os.path.join(sys.argv[0] + ".argv"), "w").write(" ".join(argv))
    def stop(*_):
        files = sorted(os.path.relpath(os.path.join(d, f), mnt) for d, _, fs in os.walk(mnt) for f in fs)
        print("published", len(files), "files")
        print("locator s3://bucket/versions/abc.metadata.json")
        sys.exit(0)
    signal.signal(signal.SIGINT, stop)
    open(os.path.join(mnt, ".mounted"), "w").close()
    while True:
        time.sleep(0.1)
""")


def test_seal_end_to_end_with_a_fake_flyte(machine, tmp_path, monkeypatch):
    home, _ = stage_all(machine, tmp_path, "claude")
    flyte = tmp_path / "flyte"
    flyte.write_text(FAKE_FLYTE)
    flyte.chmod(0o755)
    mnt = tmp_path / "mnt"
    monkeypatch.setattr(sl.os.path, "ismount", lambda p: Path(p, ".mounted").exists())
    result = sl.seal(sl.SealOptions(staged_home=home, volume="agent-home", artifact="agent-home",
                                    flyte=str(flyte), mountpoint=mnt, mount_timeout=30, publish_timeout=30))
    assert result["returncode"] == 0
    assert result["locator"] == "s3://bucket/versions/abc.metadata.json"
    assert (mnt / ".agent-sessions/finished.json").exists()
    assert (mnt / "src/webapp/.git").is_dir()
    assert "create volume agent-home --mount" in Path(str(flyte) + ".argv").read_text()


def test_seal_rejects_unstaged_dir(tmp_path):
    with pytest.raises(SystemExit):
        sl.seal(sl.SealOptions(staged_home=tmp_path, volume="v", dry_run=True))


def test_seal_dry_run_cli(machine, tmp_path):
    home, _ = stage_all(machine, tmp_path, "claude")
    proc = run_cli(machine, "seal", "--staged", str(home.parent), "--volume", "v", "--artifact", "v", "--dry-run")
    assert proc.returncode == 0, proc.stderr
    assert json.loads(proc.stdout)["command"].startswith("flyte create volume v --mount ")
    both = run_cli(machine, "seal", "--staged", str(home.parent), "--volume", "v", "--merge-artifact", "h", "--dry-run")
    assert both.returncode != 0


def test_seal_reports_flyte_output_when_mount_fails(machine, tmp_path):
    home, _ = stage_all(machine, tmp_path, "claude")
    flyte = tmp_path / "flyte"
    flyte.write_text("#!/bin/sh\necho 'fusermount3: failed to open /dev/fuse: Operation not permitted'\nexit 1\n")
    flyte.chmod(0o755)
    with pytest.raises(SystemExit) as e:
        sl.seal(sl.SealOptions(staged_home=home, volume="v", flyte=str(flyte), mountpoint=tmp_path / "m", mount_timeout=10))
    assert "/dev/fuse" in str(e.value)


def test_unknown_harness_by_file(machine, tmp_path):
    t = tmp_path / "chat.jsonl"
    t.write_text(json.dumps({"role": "tool", "input": {"cmd": "cargo build --release"}}) + "\n")
    key = f"file:{t}@{machine['webapp']}"
    s = hz.find(key)
    assert s.harness == "other" and s.cwd == str(machine["webapp"])
    assert hz.parse(s).commands == ["cargo build --release"]
    out = tmp_path / "o"
    stg.stage([s], stg.StageOptions(out=out, remote_harness="claude", log=lambda m: None))
    assert (out / "home/handoff/chat-jsonl/HANDOFF.md").exists()
    assert (out / "home/handoff/chat-jsonl/transcript.jsonl").exists()
    assert (out / "home/src/webapp/.git").is_dir()


def test_non_git_cwd_and_empty_repo_travel_as_trees(machine, tmp_path):
    plain = Path(machine["home"]) / "scratch"
    plain.mkdir()
    (plain / "data.csv").write_text("a,b\n")
    (plain / "node_modules").mkdir()
    (plain / "node_modules" / "big.js").write_text("x")
    empty = Path(machine["home"]) / "fresh"
    empty.mkdir()
    subprocess.run(["git", "init", "-q", str(empty)], check=True)
    (empty / "main.py").write_text("print(1)\n")
    sessions = []
    for i, d in enumerate((plain, empty)):
        t = tmp_path / f"t{i}.jsonl"
        t.write_text("{}\n")
        sessions.append(hz.find(f"file:{t}@{d}"))
    rep = __import__("handoff").build_report(sessions, "claude")
    assert {r["path"] for r in rep["repos"]} == {str(plain), str(empty)}
    out = tmp_path / "o"
    stg.stage(sessions, stg.StageOptions(out=out, remote_harness="claude", log=lambda m: None))
    assert (out / "home/src/scratch/data.csv").exists()
    assert not (out / "home/src/scratch/node_modules").exists()
    assert (out / "home/src/fresh/main.py").exists()
