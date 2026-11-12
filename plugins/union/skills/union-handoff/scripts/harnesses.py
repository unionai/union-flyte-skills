"""Where each coding-agent harness keeps its sessions, and how to read them.

Every reader returns ``Session`` objects and can ``parse`` one into the facts
the handoff needs: the commands its shell tool ran, the paths it touched, and
its first and last messages. Readers only ever read; nothing here writes.

Locations (all relative to the user's home unless overridden):

  claude    $CLAUDE_CONFIG_DIR or ~/.claude, projects/<cwd-slug>/<id>.jsonl
  codex     $CODEX_HOME or ~/.codex, sessions/YYYY/MM/DD/rollout-*.jsonl[.zst]
  pi        $PI_CODING_AGENT_DIR or ~/.pi/agent, sessions/--<cwd-slug>--/*.jsonl
  opencode  $OPENCODE_DB or ~/.local/share/opencode/opencode.db (SQLite), or
            the older storage/{session,message,part}/*.json layout
  hermes    $HERMES_HOME or ~/.hermes, state.db (SQLite)

See references/harnesses.md for how each was verified.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import sqlite3
import subprocess
import sys
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Iterable, Iterator, Optional


# How much of a transcript listing reads. Transcripts reach hundreds of MB;
# the facts a list needs are at the top.
HEAD_BYTES = 512 * 1024


@dataclass
class Session:
    harness: str
    id: str
    path: Path  # the transcript file (or, for a database, the database)
    cwd: Optional[str]
    title: str = ""
    started_at: Optional[float] = None
    updated_at: Optional[float] = None
    size_bytes: int = 0
    # Extra files that belong to the session (sidecar dirs, part files).
    extra: list[Path] = field(default_factory=list)
    raw_id: Optional[str] = None  # the id as the harness stores it, when safe_id changed it

    @property
    def key(self) -> str:
        return f"{self.harness}:{self.id}"

    def to_dict(self) -> dict:
        return {
            "key": self.key,
            "harness": self.harness,
            "id": self.id,
            "path": str(self.path),
            "cwd": self.cwd,
            "title": self.title,
            "started_at": self.started_at,
            "updated_at": self.updated_at,
            "size_bytes": self.size_bytes,
        }


@dataclass
class Parsed:
    commands: list[str] = field(default_factory=list)
    paths: list[str] = field(default_factory=list)  # files/dirs the tools named
    cwds: list[str] = field(default_factory=list)  # every cwd the session recorded
    first_user: str = ""
    last_user: str = ""
    last_assistant: str = ""
    branch: Optional[str] = None
    # Every turn in order as (role, text), for harnesses whose transcript lives
    # in a database, so a written handoff carries the whole conversation.
    turns: list = field(default_factory=list)
    # Programs a shell reported not found in some tool output: a session that
    # only checked for a tool did not use it.
    not_found: set = field(default_factory=set)


_SAFE_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")


def safe_id(candidate, fallback: str) -> str:
    """A session id that is safe as one path component. Ids come from inside
    transcripts, which are data, so never trust one as a path."""
    c = str(candidate or "")
    return c if _SAFE_ID_RE.match(c) and c not in (".", "..") else fallback


def _digest(text: str) -> str:
    return hashlib.sha1(text.encode(errors="replace")).hexdigest()[:12]


def home() -> Path:
    return Path(os.path.expanduser("~"))


def _iso(ts) -> Optional[float]:
    if ts is None:
        return None
    if isinstance(ts, (int, float)):
        return float(ts) / (1000.0 if ts > 1e12 else 1.0)
    try:
        return datetime.fromisoformat(str(ts).replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None


class MissingDecompressor(RuntimeError):
    pass


def _read_lines(path: Path) -> Iterator[str]:
    """A transcript's lines, streamed. Codex rewrites rollouts older than a
    week as zstd: Python 3.14 reads that natively, older Pythons need the zstd
    CLI. A missing or failing decompressor raises rather than reading as empty."""
    if path.suffix == ".zst":
        try:
            from compression import zstd  # type: ignore[import-not-found]

            with zstd.open(path, "rt", errors="replace") as fh:
                yield from fh
            return
        except ImportError:
            pass
        try:
            proc = subprocess.Popen(["zstd", "-dc", str(path)], stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        except FileNotFoundError:
            raise MissingDecompressor(f"{path} is zstd-compressed; install zstd (or use Python 3.14+) to read it")
        assert proc.stdout is not None
        try:
            for raw in proc.stdout:
                yield raw.decode(errors="replace")
        finally:
            proc.stdout.close()
            err = proc.stderr.read().decode(errors="replace") if proc.stderr else ""
            code = proc.wait()
        if code != 0:
            raise RuntimeError(f"zstd could not read {path}: {err.strip()}")
        return
    with path.open(errors="replace") as fh:
        yield from fh


def iter_text_lines(path: Path) -> Iterator[str]:
    """Lines of a transcript, each ending in a newline, streamed."""
    for line in _read_lines(path):
        yield line if line.endswith("\n") else line + "\n"


def read_text(path: Path) -> str:
    """A transcript's full text, decompressed if need be."""
    return "".join(iter_text_lines(path))


def _jsonl(path: Path, max_bytes: Optional[int] = None) -> Iterator[dict]:
    """JSON objects, one per line. ``max_bytes`` stops early: listing reads
    only a transcript's head, which is where its cwd and first turns are."""
    seen = 0
    try:
        for line in _read_lines(path):
            seen += len(line)
            if max_bytes is not None and seen > max_bytes:
                return  # closing the generator stops a zstd child early
            line = line.strip()
            if not line:
                continue
            try:
                rec = json.loads(line)
            except ValueError:
                continue
            if isinstance(rec, dict):
                yield rec
    except OSError:
        return
    except MissingDecompressor as e:
        if max_bytes is None:
            raise
        print(f"warning: {e}", file=sys.stderr)


def _text(content) -> str:
    """Flatten a message's content (string, or a list of typed blocks)."""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for block in content:
            if isinstance(block, str):
                parts.append(block)
            elif isinstance(block, dict) and block.get("type") in ("text", "input_text", "output_text"):
                parts.append(str(block.get("text", "")))
        return "\n".join(p for p in parts if p)
    return ""


def _tool_text(content) -> str:
    """The text of a tool result's content blocks."""
    if isinstance(content, list):
        return "\n".join(str(b.get("text", "")) for b in content if isinstance(b, dict))
    return str(content or "")


def inventory_not_found(text: str) -> set:
    import inventory  # local import: inventory has no harness knowledge

    return inventory.not_found_names(text)


def _short(text: str, n: int = 160) -> str:
    text = " ".join(text.split())
    return text if len(text) <= n else text[: n - 3] + "..."


def _is_noise(text: str) -> bool:
    """Harness-injected user turns (command echoes, reminders), not the human."""
    t = text.lstrip()
    return not t or t.startswith(("<command-", "<local-command", "<system-reminder", "Caveat:", "<environment_context", "<user_instructions", "# AGENTS.md"))


def _file_size(paths: Iterable[Path]) -> int:
    total = 0
    for p in paths:
        try:
            if p.is_dir():
                for f in p.rglob("*"):
                    if f.is_file():
                        total += f.stat().st_size
            elif p.is_file():
                total += p.stat().st_size
        except OSError:
            pass
    return total


# --------------------------------------------------------------------------- #
# Claude Code
# --------------------------------------------------------------------------- #


def claude_root() -> Path:
    return Path(os.environ.get("CLAUDE_CONFIG_DIR") or home() / ".claude")


def claude_slug(path: str) -> str:
    """Claude Code names a project directory after its cwd with every
    character that is not a letter or digit replaced by '-'."""
    return re.sub(r"[^A-Za-z0-9]", "-", path)


def claude_sessions() -> list[Session]:
    out = []
    projects = claude_root() / "projects"
    if not projects.is_dir():
        return out
    for f in projects.glob("*/*.jsonl"):
        cwd = title = None
        first_ts = None
        for rec in _jsonl(f, HEAD_BYTES):
            if cwd is None and rec.get("cwd"):
                cwd = rec["cwd"]
            if first_ts is None and rec.get("timestamp"):
                first_ts = _iso(rec["timestamp"])
            if rec.get("type") in ("ai-title", "summary") and (rec.get("aiTitle") or rec.get("summary")):
                title = rec.get("aiTitle") or rec.get("summary")
            if title is None and rec.get("type") == "user" and not rec.get("isMeta"):
                t = _text((rec.get("message") or {}).get("content"))
                if not _is_noise(t):
                    title = _short(t, 80)
        if cwd is None:
            continue  # a stub with no conversation in it
        sidecar = f.with_suffix("")
        extra = [sidecar] if sidecar.is_dir() else []
        st = f.stat()
        out.append(Session("claude", f.stem, f, cwd, title or "", first_ts, st.st_mtime, st.st_size + _file_size(extra), extra))
    return out


def claude_parse(s: Session) -> Parsed:
    p = Parsed()
    users = []
    # Sub-agent transcripts ride in the sidecar directory; their tool calls
    # count toward the repos and tools, their messages do not.
    sidecars = [f for d in s.extra for f in sorted(d.rglob("*.jsonl"))]
    for path in [s.path] + sidecars:
        main = path == s.path
        for rec in _jsonl(path):
            if rec.get("cwd") and rec["cwd"] not in p.cwds:
                if main:
                    p.cwds.append(rec["cwd"])
                elif rec["cwd"] not in p.paths:
                    p.paths.append(rec["cwd"])  # a sub-agent working elsewhere
            if main and rec.get("gitBranch"):
                p.branch = rec["gitBranch"]
            msg = rec.get("message") or {}
            content = msg.get("content")
            if main and rec.get("type") == "user" and not rec.get("isMeta"):
                t = _text(content)
                if not _is_noise(t):
                    users.append(t)
            if rec.get("type") == "user" and isinstance(content, list):
                for block in content:
                    if isinstance(block, dict) and block.get("type") == "tool_result":
                        out = block.get("content")
                        p.not_found |= inventory_not_found(out if isinstance(out, str) else _tool_text(out))
            if rec.get("type") == "assistant":
                t = _text(content)
                if t and main:
                    p.last_assistant = t
                for block in content if isinstance(content, list) else []:
                    if not isinstance(block, dict) or block.get("type") != "tool_use":
                        continue
                    inp = block.get("input") or {}
                    if block.get("name") == "Bash" and inp.get("command"):
                        p.commands.append(str(inp["command"]))
                    for k in ("file_path", "notebook_path", "path"):
                        if isinstance(inp.get(k), str):
                            p.paths.append(inp[k])
    if users:
        p.first_user, p.last_user = users[0], users[-1]
    return p


# --------------------------------------------------------------------------- #
# Codex CLI
# --------------------------------------------------------------------------- #


def codex_root() -> Path:
    return Path(os.environ.get("CODEX_HOME") or home() / ".codex")


_ROLLOUT_ID_RE = re.compile(r"([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})(?:_[^.]*)?\.jsonl(?:\.zst)?$")
_JS_CMD_RE = re.compile(r'\bcmd\s*:\s*("(?:[^"\\]|\\.)*")')
_JS_WORKDIR_RE = re.compile(r'\bworkdir\s*:\s*("(?:[^"\\]|\\.)*")')


def codex_sessions() -> list[Session]:
    out = []
    files = []
    for sub in ("sessions", "archived_sessions"):
        base = codex_root() / sub
        if base.is_dir():
            files += [f for f in base.rglob("rollout-*.jsonl*") if f.name.endswith((".jsonl", ".jsonl.zst"))]
    for f in files:
        meta = None
        title = ""
        for rec in _jsonl(f, HEAD_BYTES):
            if rec.get("type") == "session_meta" and meta is None:
                meta = rec.get("payload") or {}
            if not title:
                t = _codex_user_text(rec)
                if t and not _is_noise(t):
                    title = _short(t, 80)
            if meta is not None and title:
                break
        m = _ROLLOUT_ID_RE.search(f.name)
        sid = safe_id((meta or {}).get("id"), m.group(1) if m else safe_id(f.stem, "codex-session"))
        st = f.stat()
        out.append(Session("codex", sid, f, (meta or {}).get("cwd"), title, _iso((meta or {}).get("timestamp")), st.st_mtime, st.st_size))
    return out


def _codex_user_text(rec: dict) -> str:
    payload = rec.get("payload") or {}
    if rec.get("type") == "response_item" and payload.get("type") == "message" and payload.get("role") == "user":
        return _text(payload.get("content"))
    if rec.get("type") == "event_msg" and payload.get("type") == "user_message":
        return str(payload.get("message", ""))
    return ""


def codex_commands(payload: dict) -> list[str]:
    """Shell commands in one response_item payload, across the shapes Codex
    has used: a ``shell``/``exec_command`` function call whose JSON arguments
    carry ``command`` (argv list or string) or ``cmd``, a ``local_shell_call``
    action, and a custom ``exec`` tool whose JS input calls exec_command."""
    kind = payload.get("type")
    if kind == "function_call":
        try:
            args = json.loads(payload.get("arguments") or "{}")
        except ValueError:
            return []
        cmd = args.get("command") or args.get("cmd")
        return [_argv_to_command(cmd)] if cmd else []
    if kind == "local_shell_call":
        cmd = (payload.get("action") or {}).get("command")
        return [_argv_to_command(cmd)] if cmd else []
    if kind == "custom_tool_call":
        found = []
        for lit in _JS_CMD_RE.findall(str(payload.get("input") or "")):
            try:
                found.append(json.loads(lit))
            except ValueError:
                pass
        return found
    return []


def _argv_to_command(cmd) -> str:
    if isinstance(cmd, list):
        # ["bash", "-lc", "<script>"] is how Codex wraps most commands.
        if len(cmd) >= 3 and cmd[0] in ("bash", "sh", "zsh", "/bin/bash", "/bin/sh") and cmd[1] in ("-lc", "-c"):
            return str(cmd[2])
        return " ".join(str(c) for c in cmd)
    return str(cmd)


def codex_parse(s: Session) -> Parsed:
    p = Parsed()
    users = []
    for rec in _jsonl(s.path):
        payload = rec.get("payload") or {}
        cwd = payload.get("cwd") if rec.get("type") in ("session_meta", "turn_context") else None
        if cwd and cwd not in p.cwds:
            p.cwds.append(cwd)
        if rec.get("type") == "session_meta":
            branch = (payload.get("git") or {}).get("branch")
            p.branch = branch or p.branch
        t = _codex_user_text(rec)
        if t and not _is_noise(t) and rec.get("type") == "response_item":
            users.append(t)
        if rec.get("type") == "response_item":
            if payload.get("type") == "message" and payload.get("role") == "assistant":
                p.last_assistant = _text(payload.get("content")) or p.last_assistant
            p.commands.extend(codex_commands(payload))
            if payload.get("type") in ("function_call_output", "custom_tool_call_output", "local_shell_call_output"):
                out = payload.get("output")
                p.not_found |= inventory_not_found(out if isinstance(out, str) else json.dumps(out))
            if payload.get("type") == "custom_tool_call":
                for lit in _JS_WORKDIR_RE.findall(str(payload.get("input") or "")):
                    try:
                        p.paths.append(json.loads(lit))
                    except ValueError:
                        pass
            if payload.get("type") == "function_call":
                try:
                    wd = json.loads(payload.get("arguments") or "{}").get("workdir")
                except ValueError:
                    wd = None
                if wd:
                    p.paths.append(wd)
    if users:
        p.first_user, p.last_user = users[0], users[-1]
    return p


# --------------------------------------------------------------------------- #
# pi
# --------------------------------------------------------------------------- #


def pi_root() -> Path:
    return Path(os.environ.get("PI_CODING_AGENT_DIR") or home() / ".pi" / "agent")


def pi_sessions() -> list[Session]:
    out = []
    base = pi_root() / "sessions"
    if not base.is_dir():
        return out
    for f in base.glob("*/*.jsonl"):
        header = None
        title = ""
        for rec in _jsonl(f, HEAD_BYTES):
            if header is None and rec.get("type") == "session":
                header = rec
                continue
            if rec.get("type") == "session_info" and rec.get("name"):
                title = str(rec["name"])
            if not title and rec.get("type") == "message":
                msg = rec.get("message") or {}
                if msg.get("role") == "user":
                    title = _short(_text(msg.get("content")), 80)
        if header is None:
            continue
        st = f.stat()
        out.append(Session("pi", safe_id(header.get("id"), safe_id(f.stem, "pi-session")), f, header.get("cwd"), title, _iso(header.get("timestamp")), st.st_mtime, st.st_size))
    return out


def pi_parse(s: Session) -> Parsed:
    p = Parsed(cwds=[s.cwd] if s.cwd else [])
    users = []
    for rec in _jsonl(s.path):
        if rec.get("type") != "message":
            continue
        msg = rec.get("message") or {}
        role = msg.get("role")
        content = msg.get("content")
        if role == "user":
            t = _text(content)
            if t:
                users.append(t)
        elif role == "assistant":
            t = _text(content)
            if t:
                p.last_assistant = t
            for block in content if isinstance(content, list) else []:
                if isinstance(block, dict) and block.get("type") == "toolCall":
                    args = block.get("arguments") or {}
                    if block.get("name") == "bash" and args.get("command"):
                        p.commands.append(str(args["command"]))
                    if isinstance(args.get("path"), str):
                        p.paths.append(args["path"])
    if users:
        p.first_user, p.last_user = users[0], users[-1]
    return p


# --------------------------------------------------------------------------- #
# opencode
# --------------------------------------------------------------------------- #


def opencode_root() -> Path:
    data = os.environ.get("XDG_DATA_HOME") or str(home() / ".local" / "share")
    return Path(data) / "opencode"


def opencode_sessions() -> list[Session]:
    out = []
    root = opencode_root()
    base = root / "storage" / "session"
    if base.is_dir():
        for f in base.glob("*/*.json"):
            try:
                info = json.loads(f.read_text())
            except (OSError, ValueError):
                continue
            sid = safe_id(info.get("id"), safe_id(f.stem, "opencode-session"))
            if info.get("parentID"):
                continue  # a sub-agent's session; it travels with its parent
            msg_dir = root / "storage" / "message" / sid
            msgs = list(msg_dir.glob("*.json")) if msg_dir.is_dir() else []
            parts = []
            for m in msgs:
                pd = root / "storage" / "part" / m.stem
                if pd.is_dir():
                    parts.append(pd)
            extra = ([msg_dir] if msg_dir.is_dir() else []) + parts
            times = info.get("time") or {}
            out.append(Session(
                "opencode", sid, f, info.get("directory"), str(info.get("title") or ""),
                _iso(times.get("created")), _iso(times.get("updated")) or f.stat().st_mtime,
                f.stat().st_size + _file_size(extra), extra,
            ))
    db = opencode_db()
    if db is not None:
        seen = {x.id for x in out}
        out.extend(x for x in _opencode_db_sessions(db) if x.id not in seen)
    return out


def opencode_db() -> Optional[Path]:
    if os.environ.get("OPENCODE_DB"):
        p = Path(os.environ["OPENCODE_DB"])
        return p if p.is_file() else None
    p = opencode_root() / "opencode.db"
    return p if p.is_file() else None


def _ro(db: Path) -> sqlite3.Connection:
    con = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    con.row_factory = sqlite3.Row
    return con


def _tables(con: sqlite3.Connection) -> set[str]:
    return {r[0] for r in con.execute("select name from sqlite_master where type = 'table'")}


def _opencode_db_sessions(db: Path) -> list[Session]:
    """opencode 1.x keeps sessions in SQLite: table ``session`` (newer builds:
    ``session_v2``) with ``directory``, ``title`` and ``time_*`` columns.
    Read-only; an unexpected schema yields nothing rather than an error."""
    out = []
    try:
        con = _ro(db)
        table = "session" if "session" in _tables(con) else "session_v2"
        rows = con.execute(f"select * from {table}").fetchall()
        con.close()
    except sqlite3.Error:
        return out
    size = db.stat().st_size
    for r in rows:
        get = lambda k: r[k] if k in r.keys() else None  # noqa: E731
        if get("parent_id"):
            continue
        raw = str(get("id"))
        out.append(Session("opencode", safe_id(raw, "opencode-" + _digest(raw)), db, get("directory"), str(get("title") or ""),
                           _iso(get("time_created")), _iso(get("time_updated")), size, raw_id=raw))
    return out


def _opencode_db_parts(db: Path, sid: str) -> list[tuple[str, dict]]:
    """(role, part data) for one session, oldest first."""
    try:
        con = _ro(db)
        tables = _tables(con)
        if "part" in tables:
            rows = con.execute(
                "select m.data as m, p.data as p from part p join message m on m.id = p.message_id "
                "where p.session_id = ? order by m.time_created, p.id", (sid,)).fetchall()
        else:
            rows = con.execute(
                "select m.type as role, p.data as p from session_part p join session_message m on m.id = p.message_id "
                "where p.session_id = ? order by m.seq, p.ordinal", (sid,)).fetchall()
        con.close()
    except sqlite3.Error:
        return []
    out = []
    for r in rows:
        try:
            part = json.loads(r["p"])
            role = json.loads(r["m"]).get("role") if "m" in r.keys() else r["role"]
        except (TypeError, ValueError):
            continue
        out.append((str(role or ""), part))
    return out


def _opencode_part_text(part: dict) -> str:
    """One part of an opencode message, as text for a written handoff: text
    as-is, a tool call with its input and output, anything else as JSON."""
    kind = part.get("type")
    if kind == "text":
        return str(part.get("text", ""))
    if kind == "tool":
        state = part.get("state") or {}
        out = [f"[tool {part.get('tool')}] input: {json.dumps(state.get('input'), ensure_ascii=False)}"]
        if state.get("output") is not None:
            out.append(f"output:\n{state.get('output')}")
        if state.get("error") is not None:
            out.append(f"error: {state.get('error')}")
        return "\n".join(out)
    if kind == "reasoning":
        return f"[reasoning]\n{part.get('text', '')}"
    if kind in ("step-start", "step-finish"):
        return ""
    return f"[{kind}] {json.dumps(part, ensure_ascii=False)}"


def opencode_parse(s: Session) -> Parsed:
    p = Parsed(cwds=[s.cwd] if s.cwd else [])
    if s.path.suffix == ".db":
        messages = []  # [(role, [parts])], grouped by consecutive role
        for role, part in _opencode_db_parts(s.path, s.raw_id or s.id):
            if not messages or messages[-1][0] != role:
                messages.append((role, []))
            messages[-1][1].append(part)
    else:
        root = opencode_root() / "storage"
        msgs = []
        for f in sorted((root / "message" / s.id).glob("*.json")):
            try:
                msgs.append(json.loads(f.read_text()))
            except (OSError, ValueError):
                pass
        msgs.sort(key=lambda m: (m.get("time") or {}).get("created") or 0)
        messages = []
        for m in msgs:
            parts = []
            for pf in sorted((root / "part" / m.get("id", "")).glob("*.json")):
                try:
                    parts.append(json.loads(pf.read_text()))
                except (OSError, ValueError):
                    pass
            messages.append((m.get("role", ""), parts))
    users = []
    for role, parts in messages:
        texts = []
        for part in parts:
            if part.get("type") == "text":
                texts.append(str(part.get("text", "")))
            elif part.get("type") == "tool":
                inp = (part.get("state") or {}).get("input") or {}
                if part.get("tool") == "bash" and inp.get("command"):
                    p.commands.append(str(inp["command"]))
                for k in ("filePath", "path", "workdir"):
                    if isinstance(inp.get(k), str):
                        p.paths.append(inp[k])
        text = "\n".join(texts)
        rendered = [_opencode_part_text(part) for part in parts]
        rendered = [r for r in rendered if r]
        if rendered:
            p.turns.append((role, "\n\n".join(rendered)))
        if role == "user" and text:
            users.append(text)
        elif role == "assistant" and text:
            p.last_assistant = text
    if users:
        p.first_user, p.last_user = users[0], users[-1]
    return p


# --------------------------------------------------------------------------- #
# Hermes
# --------------------------------------------------------------------------- #


def hermes_root() -> Path:
    return Path(os.environ.get("HERMES_HOME") or home() / ".hermes")


def hermes_sessions() -> list[Session]:
    db = hermes_root() / "state.db"
    if not db.is_file():
        return []
    try:
        con = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
        con.row_factory = sqlite3.Row
        rows = con.execute("select * from sessions").fetchall()
        con.close()
    except sqlite3.Error:
        return []
    out = []
    size = db.stat().st_size
    for r in rows:
        keys = r.keys()
        get = lambda k: r[k] if k in keys else None  # noqa: E731
        if get("parent_session_id"):
            continue
        raw = str(get("id"))
        out.append(Session(
            "hermes", safe_id(raw, "hermes-" + _digest(raw)), db, get("cwd"), str(get("title") or ""),
            _iso(get("started_at")), _iso(get("ended_at")) or _iso(get("started_at")), size, raw_id=raw,
        ))
    return out


def hermes_parse(s: Session) -> Parsed:
    p = Parsed(cwds=[s.cwd] if s.cwd else [])
    try:
        con = sqlite3.connect(f"file:{s.path}?mode=ro", uri=True)
        con.row_factory = sqlite3.Row
        rows = con.execute("select * from messages where session_id = ? order by id", (s.raw_id or s.id,)).fetchall()
        con.close()
    except sqlite3.Error:
        return p
    users = []
    for r in rows:
        keys = r.keys()
        role = r["role"] if "role" in keys else None
        content = r["content"] if "content" in keys else None
        if content:
            tool = r["tool_name"] if "tool_name" in keys and r["tool_name"] else None
            p.turns.append((f"{role} ({tool})" if tool else str(role), str(content)))
        if role == "user" and content:
            users.append(str(content))
        elif role == "assistant" and content:
            p.last_assistant = str(content)
        calls = r["tool_calls"] if "tool_calls" in keys else None
        if calls:
            try:
                calls = json.loads(calls)
            except (TypeError, ValueError):
                calls = []
            for c in calls if isinstance(calls, list) else []:
                fn = (c or {}).get("function") or {}
                try:
                    args = json.loads(fn.get("arguments") or "{}")
                except (TypeError, ValueError):
                    args = {}
                p.turns.append((str(role), f"[tool {fn.get('name')}] {json.dumps(args, ensure_ascii=False)}"))
                if fn.get("name") == "terminal" and args.get("command"):
                    p.commands.append(str(args["command"]))
                if isinstance(args.get("path"), str):
                    p.paths.append(args["path"])
    if users:
        p.first_user, p.last_user = users[0], users[-1]
    return p


# --------------------------------------------------------------------------- #
# Any other harness: a transcript file the user (or the agent) points at
# --------------------------------------------------------------------------- #


def other_session(spec: str) -> Session:
    """``file:<transcript path>@<working directory>``, for a harness this
    script does not know. It always arrives as a written handoff."""
    body = spec[len("file:"):]
    path, _, cwd = body.rpartition("@") if "@" in body else (body, "", "")
    f = Path(os.path.expanduser(path)).resolve()
    if not f.exists():
        raise LookupError(f"no transcript at {f}")
    files = [x for x in f.rglob("*") if x.is_file()] if f.is_dir() else [f]
    st = f.stat()
    return Session("other", f.name, f, os.path.expanduser(cwd) if cwd else None, f.name,
                   st.st_mtime, st.st_mtime, _file_size(files))


def other_parse(s: Session) -> Parsed:
    """Best effort: any ``command``/``cmd`` string in any JSON line."""
    p = Parsed(cwds=[s.cwd] if s.cwd else [])

    def walk(v):
        if isinstance(v, dict):
            for k, x in v.items():
                if k in ("command", "cmd") and isinstance(x, str):
                    p.commands.append(x)
                elif k in ("command", "cmd") and isinstance(x, list) and x:
                    p.commands.append(_argv_to_command(x))
                else:
                    walk(x)
        elif isinstance(v, list):
            for x in v:
                walk(x)

    if s.path.is_file():
        for rec in _jsonl(s.path):
            walk(rec)
    return p


READERS: dict[str, tuple[Callable[[], list[Session]], Callable[[Session], Parsed]]] = {
    "claude": (claude_sessions, claude_parse),
    "codex": (codex_sessions, codex_parse),
    "pi": (pi_sessions, pi_parse),
    "opencode": (opencode_sessions, opencode_parse),
    "hermes": (hermes_sessions, hermes_parse),
}

# Harnesses a Union agent can run, so their sessions can arrive resumable.
REMOTE_HARNESSES = ("claude", "codex")


def discover(harnesses: Optional[Iterable[str]] = None) -> list[Session]:
    sessions = []
    for name in harnesses or READERS:
        sessions.extend(READERS[name][0]())
    sessions.sort(key=lambda s: s.updated_at or 0, reverse=True)
    return sessions


def parse(s: Session) -> Parsed:
    import inventory  # local import: inventory has no harness knowledge

    p = other_parse(s) if s.harness == "other" else READERS[s.harness][1](s)
    cwd = p.cwds[0] if p.cwds else s.cwd
    for c in p.commands:
        p.paths.extend(inventory.command_dirs(c, cwd))
    return p


def find(key: str, sessions: Optional[list[Session]] = None) -> Session:
    """Resolve ``harness:id`` (or a unique id prefix) to a session, or a
    ``file:<path>@<cwd>`` transcript from a harness this script does not know."""
    if key.startswith("file:"):
        return other_session(key)
    harness, _, sid = key.partition(":") if ":" in key else ("", "", key)
    pool = sessions if sessions is not None else discover([harness] if harness else None)
    matches = [s for s in pool if (not harness or s.harness == harness) and s.id.startswith(sid)]
    if len(matches) != 1:
        raise LookupError(f"{key!r} matches {len(matches)} sessions; give harness:full-id")
    return matches[0]


def fmt_time(ts: Optional[float]) -> str:
    if not ts:
        return "-"
    return datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%Y-%m-%d %H:%MZ")
