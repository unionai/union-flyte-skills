"""Unit and end-to-end tests for the union-handoff skill's scripts, against
the fake machine in evals/fixtures/handoff_world.py."""

from __future__ import annotations

import json
import os
import shutil
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
    for k in ("CODEX_HOME", "CLAUDE_CONFIG_DIR", "PI_CODING_AGENT_DIR", "HERMES_HOME", "OPENCODE_DB", "XDG_DATA_HOME", "KUBECONFIG",
              "XDG_CONFIG_HOME", "GH_CONFIG_DIR", "AWS_CONFIG_FILE", "AWS_SHARED_CREDENTIALS_FILE", "CLOUDSDK_CONFIG",
              "AWS_PROFILE"):
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
    assert world.CANARY not in proc.stdout + proc.stderr
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
    assert {"gh", "aws", "claude-login", f"file:{machine['webapp']}/.env"} <= set(creds)
    assert creds["claude-login"]["copyable"] is False
    assert "MY_SERVICE_API_KEY" in rep["flagged_env_names"]
    arrives = {s["key"]: s["arrives"] for s in rep["sessions"]}
    assert arrives[machine["claude"]] == "resumable"
    assert arrives[machine["codex"]] == "resumable"
    assert arrives[machine["pi"]] == "handoff note"


def test_report_text_never_prints_secrets(machine):
    proc = run_cli(machine, "report", *sum((["--session", machine[h]] for h in ("claude", "codex", "pi", "opencode", "hermes")), []))
    assert proc.returncode == 0, proc.stderr
    assert world.CANARY not in proc.stdout + proc.stderr
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


def test_rewrite_cwds_touches_only_cwd_metadata():
    pm = stg.PathMap()
    pm.add("/Users/a/web", "/root/src/web")
    claude = {"cwd": "/Users/a/web", "message": {"content": [
        {"type": "thinking", "thinking": "/Users/a/web", "signature": "s"},
        {"type": "tool_use", "name": "Bash", "input": {"command": "ls", "cwd": "/Users/a/web"}}]}}
    out = stg.rewrite_cwds(claude, pm)
    assert out["cwd"] == "/root/src/web"
    assert out["message"] == claude["message"]  # nested cwd keys inside a message are content
    codex = {"type": "turn_context", "payload": {"workspace_roots": ["/Users/a/web"], "cwd": "/Users/a/web/x"}}
    assert stg.rewrite_cwds(codex, pm)["payload"] == {"workspace_roots": ["/root/src/web"], "cwd": "/root/src/web/x"}
    item = {"type": "response_item", "payload": {"type": "function_call", "cwd": "/Users/a/web"}}
    assert stg.rewrite_cwds(item, pm) == item


def test_claude_slug():
    assert hz.claude_slug("/root/src/my_repo.v2") == "-root-src-my-repo-v2"


# --------------------------------------------------------------------------- #
# Stage


def stage_all(machine, tmp_path, remote, *, creds=(), ship=True, all_branches=False):
    """Stage every fixture session. ``ship`` sends whole repos (the bundle
    path); ship=False lets repos with a remote travel by reference."""
    pool = hz.discover()
    sessions = [hz.find(machine[h], pool) for h in ("claude", "codex", "pi", "opencode", "hermes")]
    out = tmp_path / "out"
    repos = {str(machine["webapp"]), str(machine["infra"])} if ship else set()
    manifest = stg.stage(sessions, stg.StageOptions(out=out, remote_harness=remote, credentials=set(creds),
                                                    ship_repos=repos, all_branches=all_branches, log=lambda m: None))
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
    assert _git(web, "config", "--get", "remote.origin.url").strip() == "https://github.com/example/webapp.git"
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
    assert world.CANARY not in staged_text


def test_stage_copies_only_picked_credentials(machine, tmp_path):
    env_id = f"file:{machine['webapp']}/.env"
    home, manifest = stage_all(machine, tmp_path, "claude", creds=["gh", env_id])
    gh = home / ".handoff/xdg-config/gh/hosts.yml"  # a Union Volume reserves .config at its root
    assert gh.exists() and oct(gh.stat().st_mode & 0o777) == "0o600"
    assert not (home / ".config").exists()
    assert (home / "src/webapp/.env").exists()
    assert not (home / ".aws").exists()
    assert set(manifest["credentials_copied"]) == {"gh", env_id}


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


def test_merge_finished_never_replaces():
    existing = {"sessions": [{"key": "handoff:claude:1", "name": "fix-tests", "spec": {"name": "fix-tests"}}]}
    rows = [{"key": "handoff:claude:1", "name": "fix-tests", "spec": {"name": "fix-tests"}},
            {"key": "handoff:claude:2", "name": "fix-tests", "spec": {"name": "fix-tests"}}]
    merged, skipped = stg.merge_finished(existing, rows)
    assert skipped == ["handoff:claude:1"]
    assert [(r["key"], r["name"], r["spec"]["name"]) for r in merged["sessions"]] == [
        ("handoff:claude:1", "fix-tests", "fix-tests"), ("handoff:claude:2", "fix-tests-2", "fix-tests-2")]


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
    assert (out / "home/.handoff/repos/webapp/repo.json").is_file()  # webapp has a remote: by reference


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


# --------------------------------------------------------------------------- #
# Regressions from review


def test_every_copy_path_leaves_unpicked_secrets_behind(machine, tmp_path):
    web = Path(machine["webapp"])
    (web / ".npmrc").write_text(f"//registry/:_authToken={world.CANARY}\n")
    (web / "id_ed25519").write_text(world.CANARY)
    plain = Path(machine["home"]) / "notes"
    plain.mkdir()
    (plain / ".env").write_text(world.CANARY)
    (plain / "todo.md").write_text("x")
    skill = Path(machine["home"]) / ".claude/skills/my-skill"
    (skill / "login.json").symlink_to(Path(machine["home"]) / ".claude/.credentials.json")
    mem = next(Path(machine["home"]).glob(".claude/projects/*/memory"))
    (mem / "token.json").write_text(world.CANARY)
    t = tmp_path / "t.jsonl"
    t.write_text("{}\n")
    pool = hz.discover()
    sessions = [hz.find(machine["claude"], pool), hz.find(f"file:{t}@{plain}")]
    out = tmp_path / "o"
    m = stg.stage(sessions, stg.StageOptions(out=out, remote_harness="claude", log=lambda x: None))
    home = out / "home"
    assert not (home / "src/webapp/.npmrc").exists() and not (home / "src/webapp/id_ed25519").exists()
    assert (home / "src/notes/todo.md").exists() and not (home / "src/notes/.env").exists()
    assert not (home / ".claude/skills/my-skill/login.json").exists()
    assert (home / ".claude/skills/my-skill/SKILL.md").exists()
    assert not list(home.rglob("token.json"))
    everything = b"".join(f.read_bytes() for f in home.rglob("*") if f.is_file())
    assert world.CANARY.encode() not in everything
    assert m["extras"]["left_behind"]


def test_secrets_in_a_transcript_stop_staging(machine, tmp_path):
    t = next(Path(machine["home"]).glob(".claude/projects/*/*.jsonl"))
    token = "ghp_" + "A1b2C3d4" * 5
    with t.open("a") as fh:
        fh.write(json.dumps({"type": "user", "cwd": str(machine["webapp"]), "message": {"role": "user", "content": f"use {token}"}}) + "\n")
    s = hz.find(machine["claude"])
    with pytest.raises(SystemExit) as e:
        stg.stage([s], stg.StageOptions(out=tmp_path / "o", remote_harness="claude", log=lambda x: None))
    assert "GitHub token" in str(e.value) and token not in str(e.value)
    assert not (tmp_path / "o" / "home").exists()
    rep = __import__("handoff").build_report([s], None)
    assert rep["sessions"][0]["flagged_in_transcript"] == {"GitHub token": 1}
    assert token not in json.dumps(rep)
    stg.stage([s], stg.StageOptions(out=tmp_path / "o2", remote_harness="claude", allow_transcript_secrets=True, log=lambda x: None))


def test_remote_url_credentials_are_redacted(machine, tmp_path):
    web = Path(machine["webapp"])
    subprocess.run(["git", "-C", str(web), "remote", "set-url", "origin",
                    f"https://bob:{world.CANARY}@github.com/example/webapp.git"], check=True)
    proc = run_cli(machine, "report", "--session", machine["claude"])
    assert world.CANARY not in proc.stdout and "https://github.com/example/webapp.git" in proc.stdout
    home, _ = stage_all(machine, tmp_path, "claude")
    assert world.CANARY not in (home / "src/webapp/.git/config").read_text()


def test_bundle_with_default_branch_main(machine, tmp_path, monkeypatch):
    # git init in the destination would check out `main`, and the bundle has a main.
    monkeypatch.setenv("GIT_CONFIG_COUNT", "1")
    monkeypatch.setenv("GIT_CONFIG_KEY_0", "init.defaultBranch")
    monkeypatch.setenv("GIT_CONFIG_VALUE_0", "main")
    infra = Path(machine["infra"])
    assert _git(infra, "rev-parse", "--abbrev-ref", "HEAD").strip() == "main"
    home, _ = stage_all(machine, tmp_path, "claude")
    assert _git(home / "src/infra", "rev-parse", "--abbrev-ref", "HEAD").strip() == "main"
    assert "union-handoff-unborn" not in _git(home / "src/infra", "branch")


def test_untrusted_codex_id_is_not_a_path(machine, tmp_path):
    f = next(Path(machine["home"]).glob(".codex/sessions/**/*.jsonl"))
    lines = f.read_text().splitlines()
    meta = json.loads(lines[0])
    meta["payload"]["id"] = "../../../escape"
    f.write_text("\n".join([json.dumps(meta)] + lines[1:]) + "\n")
    s = hz.find(machine["codex"])
    assert s.id == world.CODEX_ID  # falls back to the id in the file name
    out = tmp_path / "o"
    stg.stage([s], stg.StageOptions(out=out, remote_harness="codex", log=lambda x: None))
    assert not (tmp_path / "escape").exists()


def test_shell_only_repo_is_found(machine, tmp_path):
    other = Path(machine["home"]) / "code" / "tools"
    other.mkdir()
    subprocess.run(["git", "init", "-q", str(other)], check=True)
    t = tmp_path / "s.jsonl"
    t.write_text(json.dumps({"tool": {"command": f"cd {other} && make"}}) + "\n")
    rep = __import__("handoff").build_report([hz.find(f"file:{t}@{machine['webapp']}")], None)
    assert str(other) in {r["path"] for r in rep["repos"]}


def test_database_handoff_carries_the_whole_conversation(machine, tmp_path):
    db = Path(machine["home"]) / ".hermes/state.db"
    import sqlite3
    con = sqlite3.connect(db)
    con.execute("insert into messages (session_id, role, content) values (?,?,?)", (world.HERMES_ID, "user", "a distinctive middle message"))
    con.execute("insert into messages (session_id, role, content) values (?,?,?)", (world.HERMES_ID, "assistant", "the final answer"))
    con.commit()
    con.close()
    out = tmp_path / "o"
    stg.stage([hz.find(machine["hermes"])], stg.StageOptions(out=out, remote_harness="claude", log=lambda x: None))
    text = (out / "home/handoff/write-docs/transcript.md").read_text()
    assert "Write the docs" in text and "a distinctive middle message" in text
    assert '[tool terminal] {"command": "mkdocs build"}' in text


def test_non_utf8_untracked_filename(machine, tmp_path):
    web = os.fsencode(str(machine["webapp"]))
    with open(web + b"/notes-\xff.txt", "wb") as fh:
        fh.write(b"bytes")
    home, _ = stage_all(machine, tmp_path, "claude")
    assert os.path.exists(os.fsencode(str(home / "src/webapp")) + b"/notes-\xff.txt")


def test_out_inside_a_copied_tree_is_refused(machine, tmp_path):
    with pytest.raises(SystemExit):
        stg.stage([hz.find(machine["claude"])],
                  stg.StageOptions(out=Path(machine["webapp"]) / "handoff-out", remote_harness="claude", log=lambda x: None))


def test_unknown_or_ambiguous_credential_ids_are_refused(machine, tmp_path):
    for bad in ("file:/etc/passwd", f"file:{machine['webapp']}/missing.env", "nope"):
        with pytest.raises(SystemExit):
            stage_all(machine, tmp_path / bad.replace("/", "_"), "claude", creds=[bad])


def test_setup_script_fails_without_marking_done(tmp_path):
    tool = inv.Tool("definitely-not-a-tool", 1, None, "unknown", None, "true")
    script = tmp_path / "setup.sh"
    script.write_text(stg.setup_script([tool]))
    (tmp_path / ".handoff").mkdir()
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    for b in ("stat", "id", "touch", "true"):
        (bin_dir / b).symlink_to(__import__("shutil").which(b))
    proc = subprocess.run(["/bin/bash", str(script)], env={"HOME": str(tmp_path), "PATH": str(bin_dir)},
                          text=True, capture_output=True)
    assert proc.returncode == 1 and "definitely-not-a-tool" in proc.stderr
    assert not (tmp_path / ".handoff/setup.done").exists()


def _fake_flyte(tmp_path) -> Path:
    flyte = tmp_path / "flyte"
    flyte.write_text(FAKE_FLYTE)
    flyte.chmod(0o755)
    return flyte


def test_merge_refuses_a_home_that_already_has_the_repo(machine, tmp_path, monkeypatch):
    home, _ = stage_all(machine, tmp_path, "claude")
    mnt = tmp_path / "mnt"
    (mnt / "src/webapp").mkdir(parents=True)
    (mnt / "src/webapp/theirs.txt").write_text("theirs")
    monkeypatch.setattr(sl.os.path, "ismount", lambda p: Path(p, ".mounted").exists())
    fake = FAKE_FLYTE.replace('mnt = argv[argv.index("--mount") + 1] if "--mount" in argv else argv[2]', "mnt = argv[2]")
    flyte = tmp_path / "flyte"
    flyte.write_text(fake)
    flyte.chmod(0o755)
    with pytest.raises(SystemExit) as e:
        sl.seal(sl.SealOptions(staged_home=home, merge_artifact="agent-home", flyte=str(flyte), mountpoint=mnt,
                               mount_timeout=30, publish_timeout=30))
    assert "src/webapp" in str(e.value)
    assert not (mnt / "src/webapp/.git").exists() and not (mnt / ".agent-sessions").exists()


def test_merge_never_writes_through_a_symlink(tmp_path):
    src, dest, outside = tmp_path / "src", tmp_path / "dest", tmp_path / "outside"
    (src / "a/b").mkdir(parents=True)
    (src / "a/b/f.txt").write_text("x")
    outside.mkdir()
    dest.mkdir()
    (dest / "a").symlink_to(outside)
    with pytest.raises(sl.Collision):
        sl.copy_into(src, dest, merge=True)
    assert not list(outside.iterdir())


def test_interrupted_copy_leaves_the_mount_unpublished(machine, tmp_path, monkeypatch):
    home, _ = stage_all(machine, tmp_path, "claude")
    flyte = _fake_flyte(tmp_path)
    mnt = tmp_path / "mnt"
    monkeypatch.setattr(sl.os.path, "ismount", lambda p: Path(p, ".mounted").exists())

    def boom(*a, **k):
        raise KeyboardInterrupt

    monkeypatch.setattr(sl, "copy_into", boom)
    with pytest.raises(SystemExit) as e:
        sl.seal(sl.SealOptions(staged_home=home, volume="v", flyte=str(flyte), mountpoint=mnt, mount_timeout=30))
    msg = str(e.value)
    assert "nothing has been published" in msg
    pid = int(msg.split("pid ")[1].split(",")[0])
    os.kill(pid, 0)  # the mount is still up: it was neither signalled nor published
    os.kill(pid, 9)


# --------------------------------------------------------------------------- #
# Regressions from the second review


def test_secret_directories_are_left_behind(machine, tmp_path):
    plain = Path(machine["home"]) / "ops"
    (plain / ".kube").mkdir(parents=True)
    (plain / ".kube" / "config").write_text(world.CANARY)
    (plain / "gh").mkdir()
    (plain / "gh" / "hosts.yml").write_text(world.CANARY)
    (plain / "README.md").write_text("ops")
    t = tmp_path / "t.jsonl"
    t.write_text("{}\n")
    out = tmp_path / "o"
    stg.stage([hz.find(f"file:{t}@{plain}")], stg.StageOptions(out=out, remote_harness="claude", log=lambda x: None))
    assert (out / "home/src/ops/README.md").exists()
    assert world.CANARY.encode() not in b"".join(f.read_bytes() for f in (out / "home").rglob("*") if f.is_file())


def test_staged_secret_reverted_in_worktree_is_caught(machine, tmp_path):
    web = Path(machine["webapp"])
    (web / "config.py").write_text("KEY = 'ghp_" + "Z9y8X7w6" * 5 + "'\n")
    subprocess.run(["git", "-C", str(web), "add", "config.py"], check=True)
    (web / "config.py").write_text("KEY = None\n")
    with pytest.raises(SystemExit) as e:
        stage_all(machine, tmp_path, "claude")
    assert "GitHub token" in str(e.value)
    rep = __import__("handoff").build_report([hz.find(machine["claude"])], None)
    assert any(r["flagged_in_patch"] for r in rep["repos"])


def test_redaction_happens_before_truncation():
    token = "ghp_" + "Q" * 36
    text = "x" * 190 + " " + token
    shown = inv.redact_values(text)
    assert token[:8] not in hz._short(shown, 200)
    assert inv.redact_values("see https://bob:pw@host/r") == "see https://<redacted>@host/r"


def test_tree_copy_drops_url_credentials_and_nested_worktree_pointers(machine, tmp_path):
    plain = Path(machine["home"]) / "plain"
    plain.mkdir()
    (plain / "a.txt").write_text("a")
    nested = plain / "wt"
    nested.mkdir()
    (nested / ".git").write_text("gitdir: /old/machine/.git/worktrees/wt\n")
    empty = Path(machine["home"]) / "fresh"
    subprocess.run(["git", "init", "-q", str(empty)], check=True)
    subprocess.run(["git", "-C", str(empty), "remote", "add", "origin", f"https://bob:{world.CANARY}@example.com/r.git"], check=True)
    ts = []
    for d in (plain, empty):
        t = tmp_path / f"{d.name}.jsonl"
        t.write_text("{}\n")
        ts.append(hz.find(f"file:{t}@{d}"))
    out = tmp_path / "o"
    stg.stage(ts, stg.StageOptions(out=out, remote_harness="claude", log=lambda x: None))
    assert not (out / "home/src/plain/wt/.git").exists()
    assert world.CANARY not in (out / "home/src/fresh/.git/config").read_text()


def test_temp_branch_never_collides(machine, tmp_path):
    infra = Path(machine["infra"])
    subprocess.run(["git", "-C", str(infra), "branch", "union-handoff-unborn"], check=True)
    home, _ = stage_all(machine, tmp_path, "claude")
    assert "union-handoff-unborn" in _git(home / "src/infra", "branch")
    assert _git(home / "src/infra", "rev-parse", "--abbrev-ref", "HEAD").strip() == "main"


def test_out_inside_a_harness_dir_is_refused(machine, tmp_path):
    with pytest.raises(SystemExit):
        stg.stage([hz.find(machine["claude"])], stg.StageOptions(
            out=Path(machine["home"]) / ".claude/skills/out", remote_harness="claude", log=lambda x: None))


def test_relative_cd_and_printed_git_commands():
    assert inv.command_dirs("cd ../tools && make", "/w/app") == ["/w/tools"]
    assert inv.command_dirs("echo git -C /private/repo") == []
    assert inv.command_dirs("sudo git -C /real status") == ["/real"]


def test_safe_internal_symlinks_in_skills_survive(machine, tmp_path):
    skill = Path(machine["home"]) / ".claude/skills/my-skill"
    (skill / "v1").mkdir()
    (skill / "v1" / "ref.md").write_text("ref")
    (skill / "current").symlink_to("v1")
    home, _ = stage_all(machine, tmp_path, "claude")
    link = home / ".claude/skills/my-skill/current"
    assert link.is_symlink() and (link / "ref.md").read_text() == "ref"


def test_unknown_tools_keep_setup_failing(tmp_path):
    t = inv.Tool("mystery", 1, None, "unknown", None, None)
    t.linux_install = inv.linux_install_line(t)
    assert t.linux_install.startswith("# TODO")
    script = stg.setup_script([t])
    assert "need mystery ':'" in script


def test_report_does_not_offer_secret_directories_as_files(machine):
    plain = Path(machine["home"]) / "ops2"
    (plain / ".kube").mkdir(parents=True)
    (plain / ".kube" / "config").write_text("x")
    r = inv.describe_repo(plain)
    assert r.secret_files == [".kube/config"]


SEAL_DRIVER = textwrap.dedent("""\
    import sys, time
    from pathlib import Path
    sys.path.insert(0, sys.argv[1])
    import seal as sl
    sl.os.path.ismount = lambda p: Path(p, ".mounted").exists()
    real = sl.copy_into
    def slow(*a, **k):
        print("COPYING", flush=True)
        time.sleep(30)
        return real(*a, **k)
    sl.copy_into = slow
    sl.seal(sl.SealOptions(staged_home=Path(sys.argv[2]), volume="v", flyte=sys.argv[3],
                           mountpoint=Path(sys.argv[4]), mount_timeout=30))
""")


def test_terminal_ctrl_c_does_not_reach_the_mount(machine, tmp_path):
    import signal as _signal
    home, _ = stage_all(machine, tmp_path, "claude")
    flyte = tmp_path / "flyte"
    flyte.write_text(FAKE_FLYTE.replace('print("published"', 'open(mnt + "/../PUBLISHED", "w").close(); print("published"'))
    flyte.chmod(0o755)
    driver = tmp_path / "driver.py"
    driver.write_text(SEAL_DRIVER)
    mnt = tmp_path / "mnt"
    # Its own process group, standing in for the terminal's foreground group.
    proc = subprocess.Popen([sys.executable, str(driver), str(SCRIPTS), str(home), str(flyte), str(mnt)],
                            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, start_new_session=True)
    assert proc.stdout.readline().strip() == "COPYING"
    os.killpg(proc.pid, _signal.SIGINT)  # what Ctrl-C does: signal the whole foreground group
    _, err = proc.communicate(timeout=30)
    assert "nothing has been published" in err
    assert not (tmp_path / "PUBLISHED").exists()
    pid = int(err.split("pid ")[1].split(",")[0])
    os.kill(pid, 0)
    os.kill(pid, 9)


# --------------------------------------------------------------------------- #
# Repos by reference: re-cloned on first boot, only local work shipped


def _fake_github(machine, tmp_path, monkeypatch) -> dict:
    """The env that points https://github.com/example/ at the fixture's bare
    repos, for a process whose HOME is not the fixture machine's."""
    return {"GIT_CONFIG_COUNT": "1", "GIT_CONFIG_KEY_0": f"url.{Path(machine['github']).as_uri()}/.insteadOf",
            "GIT_CONFIG_VALUE_0": "https://github.com/example/"}


def _restore(home: Path, env_extra: dict) -> subprocess.CompletedProcess:
    env = {**os.environ, "HOME": str(home), **world.ENV, **env_extra}
    return subprocess.run([sys.executable, str(home / ".handoff/restore_repos.py")], env=env, text=True, capture_output=True)


def test_report_plans_repos_by_reference(machine):
    rep = __import__("handoff").build_report([hz.find(machine["claude"])], None)
    repos = {r["path"]: r for r in rep["repos"]}
    web = repos[str(machine["webapp"])]
    assert web["plan"] == "reference"
    assert web["clone_url"] == "https://github.com/example/webapp.git"
    assert web["unpushed_commits"] == 1


def test_repos_by_reference_restore_on_first_boot(machine, tmp_path, monkeypatch):
    env = _fake_github(machine, tmp_path, monkeypatch)
    home, manifest = stage_all(machine, tmp_path, "claude", ship=False)
    assert not (home / "src/webapp").exists()  # nothing of the repo itself is shipped
    meta = home / ".handoff/repos/webapp"
    spec = json.loads((meta / "repo.json").read_text())
    assert spec["url"] == "https://github.com/example/webapp.git" and spec["branch"] == "feature/login"
    assert (meta / "unpushed.bundle").is_file() and (meta / "staged.patch").is_file()
    assert (meta / "untracked/notes.txt").is_file() and not (meta / "untracked/.env").exists()
    assert not (home / ".handoff/repos/infra/unpushed.bundle").exists()  # infra has nothing unpushed
    assert manifest["repos"][str(machine["webapp"])]["unpushed_commits"] == 1
    setup = (home / ".handoff/setup.sh").read_text()
    assert "restore_repos.py" in setup

    agent_home = tmp_path / "agent-home"
    shutil.copytree(home, agent_home, symlinks=True)
    proc = _restore(agent_home, env)
    assert proc.returncode == 0, proc.stderr
    web = agent_home / "src/webapp"
    assert _git(web, "rev-parse", "--abbrev-ref", "HEAD").strip() == "feature/login"
    assert _git(web, "rev-parse", "HEAD").strip() == _git(Path(machine["webapp"]), "rev-parse", "HEAD").strip()
    assert _git(web, "diff", "--cached", "--name-only").split() == ["README.md"]
    assert _git(web, "diff", "--name-only").split() == ["app.py"]
    assert (web / "notes.txt").read_text() == "untracked notes\n" and not (web / ".env").exists()
    assert _git(web, "config", "--get", "remote.origin.url").strip() == "https://github.com/example/webapp.git"
    assert _git(agent_home / "src/infra", "rev-parse", "--abbrev-ref", "HEAD").strip() == "main"

    again = _restore(agent_home, env)  # idempotent
    assert again.returncode == 0 and "already restored" in again.stdout


def test_worktrees_are_recreated_as_worktrees(machine, tmp_path, monkeypatch):
    env = _fake_github(machine, tmp_path, monkeypatch)
    web = Path(machine["webapp"])
    wt = web.parent / "webapp-wt"
    subprocess.run(["git", "-C", str(web), "worktree", "add", "-q", "-b", "wt-branch", str(wt), "main"], check=True,
                   env={**os.environ, **world.ENV})
    (wt / "wt.txt").write_text("wt")
    subprocess.run(["git", "-C", str(wt), "add", "wt.txt"], check=True)
    subprocess.run(["git", "-C", str(wt), "commit", "-q", "-m", "worktree commit"], check=True, env={**os.environ, **world.ENV})
    t = tmp_path / "s.jsonl"
    t.write_text(json.dumps({"tool": {"command": f"cd {web} && make"}}) + "\n")
    s = hz.find(f"file:{t}@{wt}")
    out = tmp_path / "o"
    stg.stage([s], stg.StageOptions(out=out, remote_harness="claude", log=lambda m: None))
    spec = json.loads((out / "home/.handoff/repos/webapp-wt/repo.json").read_text())
    assert spec["worktree_of"] == "src/webapp" and spec["branch"] == "wt-branch"
    agent_home = tmp_path / "agent-home"
    shutil.copytree(out / "home", agent_home, symlinks=True)
    proc = _restore(agent_home, env)
    assert proc.returncode == 0, proc.stderr
    restored = agent_home / "src/webapp-wt"
    assert (restored / ".git").is_file()  # a worktree of src/webapp, as it was
    assert _git(restored, "rev-parse", "--abbrev-ref", "HEAD").strip() == "wt-branch"
    assert (restored / "wt.txt").read_text() == "wt"
    assert "webapp-wt" in _git(agent_home / "src/webapp", "worktree", "list")


def test_restore_reports_a_clone_it_cannot_do(machine, tmp_path):
    home, _ = stage_all(machine, tmp_path, "claude", ship=False)
    proc = _restore(home, {"GIT_TERMINAL_PROMPT": "0", "GIT_CONFIG_COUNT": "1",
                           "GIT_CONFIG_KEY_0": "url.file:///nonexistent/.insteadOf",
                           "GIT_CONFIG_VALUE_0": "https://github.com/example/"})
    assert proc.returncode == 1 and "gh auth login" in proc.stderr


def test_ship_forces_the_whole_history(machine, tmp_path):
    home, manifest = stage_all(machine, tmp_path, "claude", ship=True)
    assert (home / "src/webapp/.git").is_dir()
    assert not (home / ".handoff/repos").exists()
    assert not (home / ".handoff/restore_repos.py").exists()


# --------------------------------------------------------------------------- #
# Regressions from the third review (repos by reference)


def _commit(repo: Path, name: str, text: str, msg: str) -> str:
    (repo / name).write_text(text)
    subprocess.run(["git", "-C", str(repo), "add", name], check=True)
    subprocess.run(["git", "-C", str(repo), "commit", "-q", "-m", msg], check=True, env={**os.environ, **world.ENV})
    return _git(repo, "rev-parse", "HEAD").strip()


def test_other_branches_and_tags_with_unpushed_work_survive(machine, tmp_path, monkeypatch):
    infra = Path(machine["infra"])  # on main == origin/main, so HEAD has nothing unpushed
    subprocess.run(["git", "-C", str(infra), "checkout", "-q", "-b", "experiment"], check=True)
    exp = _commit(infra, "exp.txt", "exp", "experiment")
    subprocess.run(["git", "-C", str(infra), "tag", "v-local"], check=True)
    subprocess.run(["git", "-C", str(infra), "checkout", "-q", "main"], check=True)
    # By default only the session's branch (and worktrees') travel: the other
    # branch is reported separately and left behind.
    rep = __import__("handoff").build_report([hz.find(machine["codex"])], None)
    r = {r["path"]: r for r in rep["repos"]}[str(infra)]
    assert (r["unpushed_commits"], r["head_unpushed"], r["other_branches_unpushed"]) == (0, 0, {"experiment": 1})
    default, _ = stage_all(machine, tmp_path / "default", "claude", ship=False)
    assert not (default / ".handoff/repos/infra/unpushed.bundle").exists()
    assert "experiment" not in json.loads((default / ".handoff/repos/infra/repo.json").read_text())["branches"]

    rep = __import__("handoff").build_report([hz.find(machine["codex"])], None, all_branches=True)
    assert {r["path"]: r for r in rep["repos"]}[str(infra)]["unpushed_commits"] == 1
    home, m = stage_all(machine, tmp_path, "claude", ship=False, all_branches=True)
    assert (home / ".handoff/repos/infra/unpushed.bundle").is_file()
    agent = tmp_path / "agent"
    shutil.copytree(home, agent, symlinks=True)
    assert _restore(agent, _fake_github(machine, tmp_path, monkeypatch)).returncode == 0
    r = agent / "src/infra"
    assert _git(r, "rev-parse", "experiment").strip() == exp
    assert _git(r, "rev-parse", "v-local^{commit}").strip() == exp
    assert _git(r, "rev-parse", "--abbrev-ref", "HEAD").strip() == "main"
    assert not _git(r, "for-each-ref", "refs/handoff")


def test_stale_tracking_ref_is_not_trusted(machine, tmp_path, monkeypatch):
    # The local origin/main says the remote has commit A; the remote was since
    # force-pushed to an unrelated history. Only what ls-remote shows counts.
    web = Path(machine["webapp"])
    bare = Path(machine["github"]) / "webapp.git"
    other = tmp_path / "other"
    subprocess.run(["git", "init", "-q", "-b", "main", str(other)], check=True)
    _commit(other, "x.txt", "x", "rewritten history")
    subprocess.run(["git", "-C", str(other), "push", "-q", "--force", str(bare), "main:main"], check=True)
    plan = inv.reference_plan(web)
    assert plan["has"] == [] and "shares no commits" in plan["note"]
    home, _ = stage_all(machine, tmp_path, "claude", ship=False)
    assert (home / "src/webapp/.git").is_dir()  # falls back to shipping it whole


def test_unreachable_remote_ships_whole(machine, tmp_path, monkeypatch):
    (Path(machine["home"]) / ".gitconfig").unlink()  # github.com/example is not real
    monkeypatch.setenv("GIT_TERMINAL_PROMPT", "0")
    home, _ = stage_all(machine, tmp_path, "claude", ship=False)
    assert (home / "src/webapp/.git").is_dir() and not (home / ".handoff/repos/webapp").exists()


def test_failed_restore_leaves_nothing_and_retries(machine, tmp_path, monkeypatch):
    home, _ = stage_all(machine, tmp_path, "claude", ship=False)
    agent = tmp_path / "agent"
    shutil.copytree(home, agent, symlinks=True)
    patch = agent / ".handoff/repos/webapp/unstaged.patch"
    good = patch.read_bytes()
    patch.write_bytes(b"not a patch\n")
    env = _fake_github(machine, tmp_path, monkeypatch)
    bad = _restore(agent, env)
    assert bad.returncode == 1 and "src/webapp" in bad.stderr
    assert not (agent / "src/webapp").exists() and not list((agent / "src").glob(".webapp*"))
    patch.write_bytes(good)
    ok = _restore(agent, env)
    assert ok.returncode == 0, ok.stderr
    assert "src/infra: already restored" in ok.stdout and "src/webapp: restored" in ok.stdout
    assert _git(agent / "src/webapp", "diff", "--name-only").split() == ["app.py"]


def test_restore_refuses_a_destination_with_something_in_it(machine, tmp_path, monkeypatch):
    home, _ = stage_all(machine, tmp_path, "claude", ship=False)
    (home / "src/webapp").mkdir(parents=True)
    (home / "src/webapp/mine.txt").write_text("mine")
    proc = _restore(home, _fake_github(machine, tmp_path, monkeypatch))
    assert proc.returncode == 1 and "already holds something" in proc.stderr
    assert (home / "src/webapp/mine.txt").read_text() == "mine"


def test_ssh_urls_keep_their_user():
    assert inv.redact_url("ssh://git:pw@example.com/team/repo.git") == "ssh://git@example.com/team/repo.git"
    assert inv.redact_url("git@github.com:team/repo.git") == "git@github.com:team/repo.git"
    assert inv.redact_url("https://ghp_x@github.com/t/r.git") == "https://github.com/t/r.git"


def test_report_ship_flag(machine):
    proc = run_cli(machine, "report", "--json", "--session", machine["claude"], "--ship", str(machine["webapp"]))
    assert proc.returncode == 0, proc.stderr
    plans = {r["path"]: r["plan"] for r in json.loads(proc.stdout)["repos"]}
    assert plans[str(machine["webapp"])] == "bundle"


def test_picked_secret_in_a_reference_repo_is_recorded(machine, tmp_path):
    env_id = f"file:{machine['webapp']}/.env"
    _, m = stage_all(machine, tmp_path, "claude", creds=[env_id], ship=False)
    assert env_id in m["credentials_copied"]


def test_merge_collision_covers_reference_repos(machine, tmp_path):
    home, _ = stage_all(machine, tmp_path, "claude", ship=False)
    dest = tmp_path / "existing-home"
    (dest / "src/webapp").mkdir(parents=True)
    assert "src/webapp" in sl.collisions(home, dest)


def _fake_python(tmp_path: Path, body: str) -> Path:
    """Stands in for the flyte CLI's Python: records the launch payload."""
    py = tmp_path / "python"
    py.write_text("#!/bin/sh\n" + body)
    py.chmod(0o755)
    return py


def test_launch_uses_the_python_api(machine, tmp_path):
    py = _fake_python(tmp_path, 'printf "%s" "$3" > "$0.payload"\n'
                      'echo \'{"run": "r123", "url": "https://x/r123", "verdict": "on", "error": ""}\'\n')
    args = ["launch", "--locator", "s3://b/v/x.metadata.json", "--identifier", "my-agent", "--python", str(py)]
    dry = run_cli(machine, *args, "--dry-run")
    assert dry.returncode == 0 and "agent-workspace-launcher.launch_agent_workspace in system/production" in dry.stdout
    assert not Path(str(py) + ".payload").exists()
    real = run_cli(machine, *args)
    assert real.returncode == 0, real.stderr
    assert json.loads(real.stdout.strip().splitlines()[-1])["run"] == "r123"
    payload = json.loads(Path(str(py) + ".payload").read_text())
    assert payload == {"task": "agent-workspace-launcher.launch_agent_workspace", "project": "system",
                       "domain": "production", "identifier": "my-agent",
                       "locator": "s3://b/v/x.metadata.json", "config": None, "wait": 180, "check_secret": True}


def test_launch_snippet_is_valid_python():
    import ast
    ast.parse(__import__("handoff").LAUNCH_SNIPPET)


def test_launch_finds_the_flyte_clis_python(tmp_path, monkeypatch):
    cli = tmp_path / "flyte"
    cli.write_text("#!/opt/somewhere/bin/python3.12\nprint('x')\n")
    cli.chmod(0o755)
    monkeypatch.setenv("PATH", f"{tmp_path}:{os.environ['PATH']}")
    assert __import__("handoff").flyte_python("flyte") == "/opt/somewhere/bin/python3.12"


def test_launch_explains_an_agent_that_is_on(machine, tmp_path):
    py = _fake_python(tmp_path, "echo 'AgentAlreadyOn: my-agent is on' >&2\nexit 1\n")
    proc = run_cli(machine, "launch", "--locator", "l", "--identifier", "my-agent", "--python", str(py))
    assert proc.returncode == 1 and "Turn it off first" in proc.stderr


def test_launch_reports_a_refusal(machine, tmp_path):
    msg = "Secret 'me-claude-session' is already in use by agent 'me'. turn that one off first"
    py = _fake_python(tmp_path, "echo '" + json.dumps({"run": "r1", "url": "u", "verdict": "refused", "error": msg}) + "'\n")
    proc = run_cli(machine, "launch", "--locator", "l", "--identifier", "a", "--python", str(py))
    assert proc.returncode == 1
    assert "Union refused to start 'a'" in proc.stderr and "login of its own" in proc.stderr


# --------------------------------------------------------------------------- #
# Findings from a real macOS handoff


def test_virtualenvs_in_skills_memory_and_untracked_files_stay_behind(machine, tmp_path):
    skill = Path(machine["home"]) / ".claude/skills/my-skill"
    (skill / ".venv/lib").mkdir(parents=True)
    (skill / ".venv/lib/_native.cpython-312-darwin.so").write_bytes(b"\0" * 1000)
    (skill / ".venv/bin").mkdir()
    (skill / ".venv/bin/python").symlink_to("/usr/bin/python3")  # points outside: not a login file either
    (skill / "node_modules/x").mkdir(parents=True)
    (skill / "node_modules/x/index.js").write_text("x")
    (skill / "._SKILL.md").write_text("appledouble")
    mem = Path(machine["home"]) / ".claude/projects" / hz.claude_slug(str(machine["webapp"])) / "memory"
    (mem / "__pycache__").mkdir()
    (mem / "__pycache__/m.pyc").write_bytes(b"x")
    (Path(machine["webapp"]) / ".venv/lib").mkdir(parents=True)  # untracked and not ignored
    (Path(machine["webapp"]) / ".venv/lib/big.dylib").write_bytes(b"\0" * 1000)
    home, manifest = stage_all(machine, tmp_path, "claude", ship=False)
    staged = home / ".claude/skills/my-skill"
    assert (staged / "SKILL.md").exists()
    assert not (staged / ".venv").exists() and not (staged / "node_modules").exists()
    assert not (staged / "._SKILL.md").exists()
    assert not (home / ".claude/projects/-root-src-webapp/memory/__pycache__").exists()
    assert not (home / ".handoff/repos/webapp/untracked/.venv").exists()
    assert (home / ".handoff/repos/webapp/untracked/notes.txt").exists()
    assert not any(str(skill / ".venv") in p for p in manifest["extras"]["left_behind"])


def test_stage_warns_when_far_above_the_estimate(machine, tmp_path):
    skill = Path(machine["home"]) / ".claude/skills/my-skill"
    (skill / "data.bin").write_bytes(b"\0" * (30 * 1024 * 1024))
    pool = hz.discover()
    logs = []
    manifest = stg.stage([hz.find(machine["claude"], pool)], stg.StageOptions(
        out=tmp_path / "out", remote_harness="claude", log=logs.append))
    assert manifest["bytes"] > 30 * 1024 * 1024 > manifest["estimate_bytes"]
    text = "\n".join(logs)
    assert "far above" in text and ".claude/skills/my-skill" in text


def test_dot_config_is_relocated_and_setup_puts_it_back(machine, tmp_path):
    home, manifest = stage_all(machine, tmp_path, "claude", creds=["gh"])
    assert not (home / ".config").exists()
    assert manifest["relocated"] == {".config": ".handoff/xdg-config"}
    assert stg.reserved_in(home) == []
    setup = (home / ".handoff/setup.sh").read_text()
    assert "xdg-config" in setup
    # Run only the relocation block, as on an agent whose ~/.config is a file
    # (a Union Volume's root) and as on one where it is a directory.
    block = stg.SETUP_HEADER.split("if have apt-get &&")[0].replace("set -u", "set -u") + stg.XDG_CONFIG_BLOCK
    for case in ("file", "dir"):
        agent = tmp_path / f"agent-{case}"
        shutil.copytree(home, agent, symlinks=True)
        if case == "file":
            (agent / ".config").write_text("{}")
        script = tmp_path / f"block-{case}.sh"
        script.write_text(block + '\necho "FAILED=$FAILED"\n')
        env = {"HOME": str(agent), "PATH": os.environ["PATH"]}
        proc = subprocess.run(["bash", str(script)], env=env, text=True, capture_output=True)
        assert proc.returncode == 0 and "FAILED=\n" in proc.stdout, proc.stderr
        where = agent / (".xdg-config" if case == "file" else ".config")
        assert (where / "gh/hosts.yml").is_file()
        if case == "file":
            assert 'XDG_CONFIG_HOME="$HOME/.xdg-config"' in (agent / ".bashrc").read_text()


def test_seal_refuses_reserved_names_before_mounting(machine, tmp_path):
    home, _ = stage_all(machine, tmp_path, "claude")
    (home / ".config/gh").mkdir(parents=True)
    for dry in (True, False):
        with pytest.raises(SystemExit) as e:
            sl.seal(sl.SealOptions(staged_home=home, volume="v", dry_run=dry, flyte="/nonexistent/flyte"))
        assert ".config" in str(e.value) and "reserves" in str(e.value)


def test_copy_skips_and_sweeps_appledouble_files(tmp_path):
    src, dest = tmp_path / "src", tmp_path / "dest"
    (src / "d").mkdir(parents=True)
    (src / "d/a.txt").write_text("a")
    (src / "d/a.txt").chmod(0o640)
    (src / "._junk").write_text("x")
    dest.mkdir()
    (dest / "d").mkdir()
    (dest / "d/._a.txt").write_text("written by macOS")  # as if the kernel had added it
    result = sl.copy_into(src, dest, merge=False)
    assert (dest / "d/a.txt").read_text() == "a"
    assert oct((dest / "d/a.txt").stat().st_mode & 0o777) == "0o640"
    assert not (dest / "._junk").exists() and not (dest / "d/._a.txt").exists()
    assert result["appledouble_deleted"] == ["d/._a.txt"]


def test_gh_login_kept_in_the_keyring_is_not_offered(machine, tmp_path):
    hosts = Path(machine["home"]) / ".config/gh/hosts.yml"
    hosts.write_text("github.com:\n  user: someone\n  git_protocol: ssh\n")
    creds = {c.id: c for c in inv.credentials(Path(machine["home"]))[0]}
    assert not creds["gh"].copyable and "copies no token" in creds["gh"].advice
    with pytest.raises(SystemExit):
        stage_all(machine, tmp_path, "claude", creds=["gh"])
    proc = run_cli(machine, "report", "--session", machine["claude"])
    assert "copies no token" in proc.stdout and "[never copied]" in proc.stdout


def _ssh_remote(machine):
    web = Path(machine["webapp"])
    subprocess.run(["git", "-C", str(web), "remote", "set-url", "origin", "git@github.com:example/webapp.git"], check=True)
    gc = Path(machine["home"]) / ".gitconfig"
    gc.write_text(gc.read_text() + f'[url "{Path(machine["github"]).as_uri()}/"]\n\tinsteadOf = git@github.com:example/\n')


def test_ssh_remote_without_a_key_restores_over_https(machine, tmp_path):
    _ssh_remote(machine)
    home, manifest = stage_all(machine, tmp_path, "claude", ship=False)
    assert manifest["repos"][str(machine["webapp"])]["clone_url"] == "git@github.com:example/webapp.git"
    setup = (home / ".handoff/setup.sh").read_text()
    assert "gh auth setup-git" in setup and "git config --global --add url.https://github.com/.insteadOf" in setup
    assert setup.index("gh auth setup-git") < setup.index("restore_repos.py")
    subprocess.run(["bash", "-n", str(home / ".handoff/setup.sh")], check=True)


def test_ssh_remote_with_a_key_copied_keeps_ssh(machine, tmp_path):
    _ssh_remote(machine)
    key = Path(machine["home"]) / ".ssh/id_ed25519"
    key.parent.mkdir()
    key.write_text("not a real key\n")
    home, _ = stage_all(machine, tmp_path, "claude", ship=False, creds=["ssh:id_ed25519"])
    assert "gh auth setup-git" not in (home / ".handoff/setup.sh").read_text()


def test_credentials_follow_their_environment_variables(machine, tmp_path, monkeypatch):
    h = Path(machine["home"])
    (h / ".aws/config").write_text("[default]\nregion = us-east-1\n")
    (h / ".aws/config.merged").write_text("[profile a]\n[profile b]\n")
    monkeypatch.setenv("AWS_CONFIG_FILE", str(h / ".aws/config.merged"))
    kube = tmp_path / "kube-a.yaml"
    kube.write_text("apiVersion: v1\n")
    monkeypatch.setenv("KUBECONFIG", str(kube))
    creds = {c.id: c for c in inv.credentials(h)[0]}
    assert creds["aws-config"].path == str(h / ".aws/config.merged") and creds["aws-config"].rel == ".aws/config"
    assert "AWS_CONFIG_FILE" in creds["aws-config"].kind
    assert creds["kubeconfig"].path == str(kube) and creds["kubeconfig"].rel == ".kube/config"
    home, _ = stage_all(machine, tmp_path, "claude", creds=["aws-config", "kubeconfig"])
    assert (home / ".aws/config").read_text() == "[profile a]\n[profile b]\n"
    assert (home / ".kube/config").read_text() == "apiVersion: v1\n"


FAKE_FLYTE_ENV = FAKE_FLYTE.replace(
    'open(os.path.join(sys.argv[0] + ".argv"), "w").write(" ".join(argv))',
    'open(os.path.join(sys.argv[0] + ".argv"), "w").write(" ".join(argv))\n'
    'open(sys.argv[0] + ".env", "w").write(os.environ.get("AWS_PROFILE", "") + " " + os.environ.get("COPYFILE_DISABLE", ""))')


def test_seal_passes_config_and_aws_profile(machine, tmp_path, monkeypatch):
    home, _ = stage_all(machine, tmp_path, "claude")
    proc = run_cli(machine, "seal", "--staged", str(home.parent), "--volume", "v", "--config", "/x/config.yaml",
                   "--aws-profile", "sso-writer", "--dry-run")
    assert proc.returncode == 0, proc.stderr
    assert json.loads(proc.stdout)["command"].startswith("AWS_PROFILE=sso-writer flyte --config /x/config.yaml create volume v")
    flyte = tmp_path / "flyte"
    flyte.write_text(FAKE_FLYTE_ENV)
    flyte.chmod(0o755)
    monkeypatch.setattr(sl.os.path, "ismount", lambda p: Path(p, ".mounted").exists())
    sl.seal(sl.SealOptions(staged_home=home, volume="v", flyte=str(flyte), mountpoint=tmp_path / "mnt",
                           config="/x/config.yaml", aws_profile="sso-writer", mount_timeout=30, publish_timeout=30))
    assert Path(str(flyte) + ".argv").read_text().startswith("--config /x/config.yaml create volume v")
    assert Path(str(flyte) + ".env").read_text() == "sso-writer 1"


def test_probes_and_missing_tools_are_not_tools(tmp_path):
    cmds = ["which sky", "command -v sky && sky status", "command -v kubectl", "pipx list | grep sky",
            "terraform --version", "terraform plan", "helm version"]
    names = {t.name for t in inv.tools_from(cmds, tmp_path, not_found={"pipx"})}
    assert "terraform" in names and "sky" in names  # `sky status` ran
    assert not {"kubectl", "pipx", "helm", "which"} & names
    assert inv.not_found_names("zsh: command not found: pipx\nbash: line 1: sky: command not found\n"
                               "sh: 1: fd: not found") == {"pipx", "sky", "fd"}


def test_claude_parse_collects_not_found(machine):
    path = hz.find(machine["claude"]).path
    rec = {"type": "user", "cwd": str(machine["webapp"]), "message": {"role": "user", "content": [
        {"type": "tool_result", "tool_use_id": "t9", "content": "zsh: command not found: pipx"}]}}
    with path.open("a") as fh:
        fh.write(json.dumps(rec) + "\n")
    assert "pipx" in hz.parse(hz.find(machine["claude"])).not_found


def test_report_separates_the_sessions_branch(machine):
    web = Path(machine["webapp"])
    subprocess.run(["git", "-C", str(web), "branch", "old-work", "main"], check=True)
    subprocess.run(["git", "-C", str(web), "checkout", "-q", "old-work"], check=True)
    _commit(web, "old.txt", "old", "old work")
    subprocess.run(["git", "-C", str(web), "checkout", "-q", "feature/login"], check=True)
    proc = run_cli(machine, "report", "--session", machine["claude"])
    assert "1 commit(s) on feature/login (the session's branch)" in proc.stdout
    assert "1 commit(s) on 1 other local branch(es)" in proc.stdout and "old-work (1)" in proc.stdout
    assert "--all-branches" in proc.stdout


def test_refresh_picks_up_what_the_session_said_after_staging(machine, tmp_path):
    home, _ = stage_all(machine, tmp_path, "claude")
    src = hz.find(machine["claude"]).path
    with src.open("a") as fh:
        fh.write(json.dumps({"type": "user", "cwd": str(machine["webapp"]), "uuid": "late",
                             "message": {"role": "user", "content": "one more thing"}}) + "\n")
    staged = home / ".claude/projects/-root-src-webapp" / f"{world.CLAUDE_ID}.jsonl"
    assert "one more thing" not in staged.read_text()
    proc = run_cli(machine, "stage", "--refresh", "--out", str(home.parent))
    assert proc.returncode == 0, proc.stderr
    last = json.loads(staged.read_text().splitlines()[-1])
    assert last["message"]["content"] == "one more thing" and last["cwd"] == "/root/src/webapp"
    assert (home / "handoff/upgrade-terraform-providers/transcript.jsonl").exists()

    with src.open("a") as fh:  # a credential pasted after staging: refused, nothing written
        fh.write(json.dumps({"type": "user", "message": {"role": "user", "content": "ghp_" + "a" * 36}}) + "\n")
    before = staged.read_text()
    with pytest.raises(SystemExit) as e:
        stg.refresh_transcripts(home, log=lambda m: None)
    assert "GitHub token" in str(e.value) and staged.read_text() == before


def test_launch_checks_for_a_login_secret_first(machine, tmp_path):
    out = {"run": None, "url": None, "verdict": "missing_secret", "wanted": ["a-claude-token", "a-claude-session"],
           "mint_link": "https://mint.example/?name=a-claude-session&project=p&domain=d", "error": "none"}
    py = _fake_python(tmp_path, "echo '" + json.dumps(out) + "'\n")
    proc = run_cli(machine, "launch", "--locator", "l", "--identifier", "a", "--project", "p", "--domain", "d",
                   "--python", str(py))
    assert proc.returncode == 1
    assert "Nothing was started" in proc.stderr and "a-claude-token, a-claude-session" in proc.stderr
    assert "claude setup-token" in proc.stderr and "--project p --domain d" in proc.stderr
    assert "https://mint.example/" in proc.stderr
