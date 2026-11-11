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
    env_id = f"file:{machine['webapp']}/.env"
    home, manifest = stage_all(machine, tmp_path, "claude", creds=["gh", env_id])
    gh = home / ".config/gh/hosts.yml"
    assert gh.exists() and oct(gh.stat().st_mode & 0o777) == "0o600"
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


# --------------------------------------------------------------------------- #
# Regressions from review


def test_every_copy_path_leaves_unpicked_secrets_behind(machine, tmp_path):
    web = Path(machine["webapp"])
    (web / ".npmrc").write_text(f"//registry/:_authToken={world.FAKE_SECRET}\n")
    (web / "id_ed25519").write_text(world.FAKE_SECRET)
    plain = Path(machine["home"]) / "notes"
    plain.mkdir()
    (plain / ".env").write_text(world.FAKE_SECRET)
    (plain / "todo.md").write_text("x")
    skill = Path(machine["home"]) / ".claude/skills/my-skill"
    (skill / "login.json").symlink_to(Path(machine["home"]) / ".claude/.credentials.json")
    mem = next(Path(machine["home"]).glob(".claude/projects/*/memory"))
    (mem / "token.json").write_text(world.FAKE_SECRET)
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
    assert world.FAKE_SECRET.encode() not in everything
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
    assert rep["sessions"][0]["secrets_in_transcript"] == {"GitHub token": 1}
    assert token not in json.dumps(rep)
    stg.stage([s], stg.StageOptions(out=tmp_path / "o2", remote_harness="claude", allow_transcript_secrets=True, log=lambda x: None))


def test_remote_url_credentials_are_redacted(machine, tmp_path):
    web = Path(machine["webapp"])
    subprocess.run(["git", "-C", str(web), "remote", "set-url", "origin",
                    f"https://bob:{world.FAKE_SECRET}@github.com/example/webapp.git"], check=True)
    proc = run_cli(machine, "report", "--session", machine["claude"])
    assert world.FAKE_SECRET not in proc.stdout and "https://github.com/example/webapp.git" in proc.stdout
    home, _ = stage_all(machine, tmp_path, "claude")
    assert world.FAKE_SECRET not in (home / "src/webapp/.git/config").read_text()


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
    assert "Write the docs" in text and "a distinctive middle message" in text and "$ mkdocs build" in text


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
