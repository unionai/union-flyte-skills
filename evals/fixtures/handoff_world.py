"""A fake developer machine for the union-handoff skill.

``build(root)`` makes ``root/home`` with:

  ~/code/webapp     git repo, branch feature/login, one unpushed commit, a
                    staged change, an unstaged change, an untracked file and an
                    untracked .env (a secret file)
  ~/code/infra      git repo on main, clean
  sessions          one per harness: claude (in webapp, also edits infra),
                    codex (in infra), pi, opencode (SQLite), hermes (SQLite)
  credentials       gh, aws, a Claude login; each holds a FAKE_SECRET value
                    that must never appear in any output
  claude extras     ~/.claude/CLAUDE.md, a project memory, a user skill

Returns the placeholders scenarios use: home, the session keys, repo paths.
"""

from __future__ import annotations

import json
import os
import re
import sqlite3
import subprocess
from pathlib import Path

FAKE_SECRET = "FAKE_SECRET_9f8e7d6c5b4a"
CLAUDE_ID = "11111111-2222-4333-8444-555555555555"
CODEX_ID = "019a0000-aaaa-7bbb-8ccc-dddddddddddd"
PI_ID = "pi-session-0001"
OPENCODE_ID = "ses_opencode0001"
HERMES_ID = "hermes-0001"
SIGNATURE = "c2lnbmVkLXRoaW5raW5nLWJsb2Nr"  # a thinking block's signature: must survive byte for byte

ENV = {
    "GIT_AUTHOR_NAME": "Fixture",
    "GIT_AUTHOR_EMAIL": "fixture@example.com",
    "GIT_COMMITTER_NAME": "Fixture",
    "GIT_COMMITTER_EMAIL": "fixture@example.com",
    "MY_SERVICE_API_KEY": FAKE_SECRET,
}


def _git(repo: Path, *args: str) -> None:
    env = {**os.environ, **ENV, "HOME": str(repo.parent.parent)}
    subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True, env=env)


def _jsonl(path: Path, records: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(json.dumps(r) + "\n" for r in records))


def _repo(home: Path, name: str, branch: str) -> Path:
    repo = home / "code" / name
    repo.mkdir(parents=True)
    _git(repo, "init", "-q", "-b", "main")
    (repo / "README.md").write_text(f"# {name}\n")
    (repo / "app.py").write_text("print('v1')\n")
    _git(repo, "add", ".")
    _git(repo, "commit", "-q", "-m", "initial")
    _git(repo, "remote", "add", "origin", f"https://github.com/example/{name}.git")
    _git(repo, "update-ref", "refs/remotes/origin/main", "HEAD")
    if branch != "main":
        _git(repo, "checkout", "-q", "-b", branch)
        _git(repo, "branch", "--set-upstream-to=origin/main")
    return repo


def build(root: Path) -> dict:
    home = root / "home"
    home.mkdir(parents=True)

    webapp = _repo(home, "webapp", "feature/login")
    (webapp / "login.py").write_text("def login():\n    return True\n")
    _git(webapp, "add", "login.py")
    _git(webapp, "commit", "-q", "-m", "add login")  # unpushed
    (webapp / "README.md").write_text("# webapp\n\nstaged change\n")
    _git(webapp, "add", "README.md")  # staged
    (webapp / "app.py").write_text("print('v2, unstaged')\n")  # unstaged
    (webapp / "notes.txt").write_text("untracked notes\n")
    (webapp / ".env").write_text(f"API_KEY={FAKE_SECRET}\n")
    infra = _repo(home, "infra", "main")

    # Claude Code: a session in webapp that also edits infra.
    proj = home / ".claude" / "projects" / re.sub(r"[^A-Za-z0-9]", "-", str(webapp))
    base = {"sessionId": CLAUDE_ID, "cwd": str(webapp), "gitBranch": "feature/login", "version": "2.0.0"}
    _jsonl(proj / f"{CLAUDE_ID}.jsonl", [
        {"type": "permission-mode", "permissionMode": "default", "sessionId": CLAUDE_ID},
        {**base, "type": "user", "uuid": "u1", "timestamp": "2026-11-01T10:00:00Z",
         "message": {"role": "user", "content": "Add a login page and deploy it"}},
        {**base, "type": "assistant", "uuid": "a1", "timestamp": "2026-11-01T10:00:05Z",
         "message": {"role": "assistant", "content": [
             {"type": "thinking", "thinking": f"The repo is at {webapp}.", "signature": SIGNATURE},
             {"type": "text", "text": f"I will work in {webapp}."},
             {"type": "tool_use", "id": "t1", "name": "Bash", "input": {"command": "gh pr list && kubectl get pods -n web | jq ."}},
             {"type": "tool_use", "id": "t2", "name": "Bash", "input": {"command": "cat > deploy.yaml <<EOF\nawait notacommand\nEOF\nhelm upgrade web ./chart"}},
             {"type": "tool_use", "id": "t3", "name": "Edit", "input": {"file_path": str(infra / "app.py"), "old_string": "v1", "new_string": "v2"}},
         ]}},
        {**base, "type": "user", "uuid": "u2", "timestamp": "2026-11-01T10:01:00Z",
         "message": {"role": "user", "content": [{"type": "tool_result", "tool_use_id": "t1", "content": "ok"}]}},
        {**base, "type": "user", "uuid": "u3", "timestamp": "2026-11-01T10:02:00Z",
         "message": {"role": "user", "content": "Now run the tests"}},
        {**base, "type": "assistant", "uuid": "a2", "timestamp": "2026-11-01T10:02:05Z",
         "message": {"role": "assistant", "content": [
             {"type": "tool_use", "id": "t4", "name": "Bash", "input": {"command": "uv run pytest -q"}},
             {"type": "text", "text": "Tests pass."}]}},
        {"type": "ai-title", "aiTitle": "Login page and deploy", "sessionId": CLAUDE_ID},
    ])
    _jsonl(proj / CLAUDE_ID / "subagents" / "agent-1.jsonl", [
        {**base, "type": "user", "isSidechain": True, "message": {"role": "user", "content": "explore"}},
    ])
    (proj / "memory").mkdir()
    (proj / "memory" / "MEMORY.md").write_text("- [Deploys](deploys.md) - use helm\n")
    (home / ".claude" / "CLAUDE.md").write_text("Always run tests before committing.\n")
    skill = home / ".claude" / "skills" / "my-skill"
    skill.mkdir(parents=True)
    (skill / "SKILL.md").write_text("---\nname: my-skill\ndescription: A user skill from the fixture.\n---\nHello.\n")
    (home / ".claude" / ".credentials.json").write_text(json.dumps({"token": FAKE_SECRET}))

    # Codex: a session in infra.
    codex_file = home / ".codex" / "sessions" / "2026" / "11" / "02" / f"rollout-2026-11-02T09-00-00-{CODEX_ID}.jsonl"
    _jsonl(codex_file, [
        {"timestamp": "2026-11-02T09:00:00Z", "type": "session_meta",
         "payload": {"id": CODEX_ID, "timestamp": "2026-11-02T09:00:00Z", "cwd": str(infra), "originator": "codex_cli_rs",
                     "cli_version": "0.150.0", "git": {"branch": "main"}}},
        {"timestamp": "2026-11-02T09:00:01Z", "type": "turn_context", "payload": {"cwd": str(infra), "workspace_roots": [str(infra)]}},
        {"timestamp": "2026-11-02T09:00:01Z", "type": "response_item",
         "payload": {"type": "message", "role": "user", "content": [{"type": "input_text", "text": "Upgrade terraform providers"}]}},
        {"timestamp": "2026-11-02T09:00:02Z", "type": "response_item",
         "payload": {"type": "function_call", "name": "exec_command", "call_id": "c1",
                     "arguments": json.dumps({"cmd": "terraform init -upgrade", "workdir": str(infra)})}},
        {"timestamp": "2026-11-02T09:00:03Z", "type": "response_item",
         "payload": {"type": "function_call", "name": "shell", "call_id": "c2",
                     "arguments": json.dumps({"command": ["bash", "-lc", "terraform plan | tee plan.txt"]})}},
        {"timestamp": "2026-11-02T09:00:04Z", "type": "response_item",
         "payload": {"type": "message", "role": "assistant", "content": [{"type": "output_text", "text": "Providers upgraded."}]}},
    ])
    (home / ".codex" / "AGENTS.md").write_text("Prefer small commits.\n")

    # pi
    pi_dir = home / ".pi" / "agent" / "sessions" / ("--" + str(webapp).lstrip("/").replace("/", "-") + "--")
    _jsonl(pi_dir / f"2026-11-03T08-00-00-000Z_{PI_ID}.jsonl", [
        {"type": "session", "version": 3, "id": PI_ID, "timestamp": "2026-11-03T08:00:00Z", "cwd": str(webapp)},
        {"type": "message", "id": "m1", "message": {"role": "user", "content": [{"type": "text", "text": "Fix the CSS"}]}},
        {"type": "message", "id": "m2", "message": {"role": "assistant", "content": [
            {"type": "toolCall", "id": "x", "name": "bash", "arguments": {"command": "npx prettier --write ."}}]}},
    ])

    # opencode 1.x (SQLite)
    oc = home / ".local" / "share" / "opencode"
    oc.mkdir(parents=True)
    con = sqlite3.connect(oc / "opencode.db")
    con.executescript("""
        create table session (id text primary key, project_id text, parent_id text, directory text, title text,
                              time_created integer, time_updated integer);
        create table message (id text primary key, session_id text, time_created integer, data text);
        create table part (id text primary key, message_id text, session_id text, data text);
    """)
    con.execute("insert into session values (?,?,?,?,?,?,?)", (OPENCODE_ID, "p1", None, str(infra), "Rotate certs", 1762160000000, 1762160100000))
    con.execute("insert into message values (?,?,?,?)", ("m1", OPENCODE_ID, 1, json.dumps({"role": "user"})))
    con.execute("insert into part values (?,?,?,?)", ("p1", "m1", OPENCODE_ID, json.dumps({"type": "text", "text": "Rotate the certs"})))
    con.execute("insert into message values (?,?,?,?)", ("m2", OPENCODE_ID, 2, json.dumps({"role": "assistant"})))
    con.execute("insert into part values (?,?,?,?)", ("p2", "m2", OPENCODE_ID, json.dumps(
        {"type": "tool", "tool": "bash", "state": {"status": "completed", "input": {"command": "openssl req -new"}}})))
    con.commit()
    con.close()

    # Hermes (SQLite)
    hm = home / ".hermes"
    hm.mkdir()
    con = sqlite3.connect(hm / "state.db")
    con.executescript("""
        create table sessions (id text primary key, source text, parent_session_id text, started_at real, ended_at real,
                               title text, cwd text, git_branch text);
        create table messages (id integer primary key, session_id text, role text, content text, tool_calls text);
    """)
    con.execute("insert into sessions values (?,?,?,?,?,?,?,?)", (HERMES_ID, "cli", None, 1762170000.0, 1762170500.0, "Write docs", str(webapp), "main"))
    con.execute("insert into messages (session_id, role, content) values (?,?,?)", (HERMES_ID, "user", "Write the docs"))
    con.execute("insert into messages (session_id, role, content, tool_calls) values (?,?,?,?)", (HERMES_ID, "assistant", "", json.dumps(
        [{"id": "1", "type": "function", "function": {"name": "terminal", "arguments": json.dumps({"command": "mkdocs build"})}}])))
    con.commit()
    con.close()

    # Credentials, each holding the fake secret.
    (home / ".config" / "gh").mkdir(parents=True)
    (home / ".config" / "gh" / "hosts.yml").write_text(f"github.com:\n  oauth_token: {FAKE_SECRET}\n")
    (home / ".aws").mkdir()
    (home / ".aws" / "credentials").write_text(f"[default]\naws_secret_access_key = {FAKE_SECRET}\n")

    return {
        "home": home,
        "cwd": home,
        "webapp": webapp,
        "infra": infra,
        "claude": f"claude:{CLAUDE_ID}",
        "codex": f"codex:{CODEX_ID}",
        "pi": f"pi:{PI_ID}",
        "opencode": f"opencode:{OPENCODE_ID}",
        "hermes": f"hermes:{HERMES_ID}",
        "secret": FAKE_SECRET,
    }
