"""Build the Union agent home for a handoff in a local staging directory.

Nothing here touches a Volume: ``stage`` writes ``<out>/home``, a directory
laid out exactly as the agent's home will be, and ``seal.py`` copies it into
the Volume. Staging first means the user can look at every file before it
leaves the machine.

Layout written under ``<out>/home`` (paths are relative to the agent's home):

  src/<repo>/                    each repo, cloned from a git bundle with the
                                 uncommitted work reapplied (or copied as a tree)
  .claude/projects/<slug>/       Claude transcripts, re-keyed to the remote cwd
  .agent-sessions/codex/sessions/<thread>/sessions/...
                                 Codex rollouts, re-keyed to the remote cwd
  .agent-sessions/finished.json  one row per resumable session, so it is listed
                                 as finished in the agent and Restart resumes it
  handoff/<session>/             sessions that cannot resume on the remote
                                 harness: the transcript plus HANDOFF.md
  .handoff/setup.sh              installs the tools the sessions used
  .handoff/manifest.json         what was staged, from where
"""

from __future__ import annotations

import copy
import json
import os
import re
import shutil
import stat
import subprocess
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Optional

import harnesses as hz
import inventory as inv

# Only the metadata fields that record a session's working directory are
# rewritten (see rewrite_cwds). Message text, tool input and output, and
# thinking blocks are left byte for byte: some are signed, and a changed byte
# fails the resume.


# --------------------------------------------------------------------------- #
# Path mapping
# --------------------------------------------------------------------------- #


@dataclass
class PathMap:
    """Local path prefix -> remote path prefix, longest prefix first."""

    pairs: list[tuple[str, str]] = field(default_factory=list)

    def add(self, local: str, remote: str) -> None:
        local = local.rstrip("/") or "/"
        self.pairs.append((local, remote.rstrip("/") or "/"))
        self.pairs.sort(key=lambda p: len(p[0]), reverse=True)

    def map(self, path: str) -> str:
        for local, remote in self.pairs:
            if path == local:
                return remote
            if path.startswith(local.rstrip("/") + "/"):
                return remote.rstrip("/") + path[len(local.rstrip("/")):]
        return path

    def to_dict(self) -> dict:
        return dict(self.pairs)


def rewrite_cwds(rec: dict, pm: PathMap) -> dict:
    """Rewrite the working-directory metadata of one transcript record.

    Claude Code: the top-level ``cwd`` every record carries.
    Codex: ``payload.cwd`` and ``payload.workspace_roots`` (session_meta,
    turn_context), ``payload.thread_settings.cwd``, and the per-environment
    ``cwd`` in a world_state snapshot. Nothing nested inside messages or tool
    calls is touched, even when it has a key named cwd.
    """
    out = copy.deepcopy(rec)
    if isinstance(out.get("cwd"), str):
        out["cwd"] = pm.map(out["cwd"])
    payload = out.get("payload")
    if not isinstance(payload, dict) or out.get("type") not in ("session_meta", "turn_context", "world_state", "event_msg"):
        return out
    if out["type"] in ("session_meta", "turn_context"):
        if isinstance(payload.get("cwd"), str):
            payload["cwd"] = pm.map(payload["cwd"])
        if isinstance(payload.get("workspace_roots"), list):
            payload["workspace_roots"] = [pm.map(x) if isinstance(x, str) else x for x in payload["workspace_roots"]]
    settings = payload.get("thread_settings")
    if isinstance(settings, dict) and isinstance(settings.get("cwd"), str):
        settings["cwd"] = pm.map(settings["cwd"])
    envs = ((payload.get("state") or {}).get("environments") or {}).get("environments") if out["type"] == "world_state" else None
    if isinstance(envs, dict):
        for env in envs.values():
            if isinstance(env, dict) and isinstance(env.get("cwd"), str):
                env["cwd"] = pm.map(env["cwd"])
    return out


def rewrite_jsonl(src: Path, dest: Path, pm: PathMap) -> int:
    """Copy a JSONL transcript, streamed, re-keying its cwd metadata. Lines
    that are not JSON objects, or need no change, are copied byte for byte.
    Returns the number of lines changed."""
    changed = 0
    dest.parent.mkdir(parents=True, exist_ok=True)
    with dest.open("w") as out:
        for line in hz.iter_text_lines(src):
            stripped = line.strip()
            try:
                rec = json.loads(stripped) if stripped else None
            except ValueError:
                rec = None
            if not isinstance(rec, dict):
                out.write(line)
                continue
            new = rewrite_cwds(rec, pm)
            if new != rec:
                changed += 1
                out.write(json.dumps(new, ensure_ascii=False, separators=(",", ":")) + "\n")
            else:
                out.write(line)
    return changed


def slugify(text: str, limit: int = 40) -> str:
    s = re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")
    return (s[:limit].rstrip("-") or "session")


# --------------------------------------------------------------------------- #
# Repos
# --------------------------------------------------------------------------- #


def _run(cmd: list[str], **kw) -> subprocess.CompletedProcess:
    proc = subprocess.run(cmd, text=True, capture_output=True, **kw)
    if proc.returncode != 0:
        raise RuntimeError(f"{' '.join(cmd)}: {proc.stderr.strip() or proc.stdout.strip()}")
    return proc


def stage_git_repo(src: Path, dest: Path, work: Path, picked_files: set[str]) -> dict:
    """Clone ``src`` into ``dest`` through a bundle of its local branches,
    tags and remote-tracking branches (so upstreams and ahead/behind survive),
    then reapply its staged and unstaged changes and copy its untracked
    files (secret-looking ones only when picked, by absolute path)."""
    name = dest.name
    bundle = work / f"{name}.bundle"
    bundle.parent.mkdir(parents=True, exist_ok=True)
    _run(["git", "-C", str(src), "bundle", "create", str(bundle), "--branches", "--tags", "--remotes", "HEAD"])
    _run(["git", "init", "--quiet", str(dest)])
    # Point HEAD at a branch the bundle does not have, so fetching into
    # refs/heads/* cannot collide with the checked-out initial branch.
    heads = set(_run(["git", "bundle", "list-heads", str(bundle)]).stdout.split())
    unborn, n = "refs/heads/union-handoff-unborn", 0
    while unborn in heads:
        n += 1
        unborn = f"refs/heads/union-handoff-unborn-{n}"
    _run(["git", "-C", str(dest), "symbolic-ref", "HEAD", unborn])
    _run(["git", "-C", str(dest), "fetch", "--quiet", str(bundle), "+refs/heads/*:refs/heads/*", "+refs/tags/*:refs/tags/*", "+refs/remotes/*:refs/remotes/*"])

    branch = inv._git(src, "symbolic-ref", "--quiet", "--short", "HEAD")
    head = inv._git(src, "rev-parse", "HEAD", check=True)
    if branch:
        _run(["git", "-C", str(dest), "checkout", "--quiet", branch])
    else:
        _run(["git", "-C", str(dest), "checkout", "--quiet", "--detach", head])

    for remote in inv._git(src, "remote").splitlines():
        # Credentials embedded in a URL (https://user:token@host) stay behind.
        url = inv.redact_url(inv._git(src, "remote", "get-url", remote))
        if url:
            _run(["git", "-C", str(dest), "remote", "add", remote, url])
    for b in inv._git(src, "for-each-ref", "--format=%(refname:short)", "refs/heads").splitlines():
        for key in ("remote", "merge"):
            val = inv._git(src, "config", f"branch.{b}.{key}")
            if val:
                _run(["git", "-C", str(dest), "config", f"branch.{b}.{key}", val])

    staged = subprocess.run(["git", "-C", str(src), "diff", "--cached", "--binary"], capture_output=True).stdout
    unstaged = subprocess.run(["git", "-C", str(src), "diff", "--binary"], capture_output=True).stdout
    for patch, args in ((staged, ["--index"]), (unstaged, [])):
        if patch.strip():
            subprocess.run(["git", "-C", str(dest), "apply", "--binary", "--whitespace=nowarn", *args],
                           input=patch, check=True, capture_output=True)

    copied, skipped = [], []
    for rel in inv.untracked(src):
        s, d = src / rel, dest / rel
        if inv.is_secret_path(rel) and str(s) not in picked_files:
            skipped.append(rel)
            continue
        d.parent.mkdir(parents=True, exist_ok=True)
        if s.is_symlink():
            os.symlink(os.readlink(s), d)
        elif s.is_file():
            shutil.copy2(s, d)
        else:
            continue
        copied.append(rel)
    bundle_bytes = bundle.stat().st_size
    bundle.unlink()
    return {
        "mode": "bundle",
        "branch": branch or None,
        "head": head,
        "bundle_bytes": bundle_bytes,
        "patch_bytes": len(staged) + len(unstaged),
        "untracked_copied": len(copied),
        "secret_files_left_behind": skipped,
    }


def stage_git_reference(src: Path, rel_dest: str, meta: Path, picked_files: set[str],
                        worktree_of: Optional[str]) -> dict:
    """Record a repo to be re-cloned from its remote on first boot, and ship
    only what the remote lacks: the commits after the remote base (a small
    bundle), the staged and unstaged patches, and the untracked files.
    ~/.handoff/restore_repos.py puts it back together."""
    upstream, url, base = inv.remote_base(src)
    branch = inv._git(src, "symbolic-ref", "--quiet", "--short", "HEAD") or None
    head = inv._git(src, "rev-parse", "HEAD", check=True)
    meta.mkdir(parents=True)
    bundle_bytes = 0
    if base != head:
        bundle = meta / "unpushed.bundle"
        _run(["git", "-C", str(src), "bundle", "create", str(bundle), "HEAD", f"^{base}"])
        bundle_bytes = bundle.stat().st_size
    staged = subprocess.run(["git", "-C", str(src), "diff", "--cached", "--binary"], capture_output=True).stdout
    unstaged = subprocess.run(["git", "-C", str(src), "diff", "--binary"], capture_output=True).stdout
    for name, patch in (("staged.patch", staged), ("unstaged.patch", unstaged)):
        if patch.strip():
            (meta / name).write_bytes(patch)
    copied, skipped = [], []
    for rel in inv.untracked(src):
        s, d = src / rel, meta / "untracked" / rel
        if inv.is_secret_path(rel) and str(s) not in picked_files:
            skipped.append(rel)
            continue
        d.parent.mkdir(parents=True, exist_ok=True)
        if s.is_symlink():
            os.symlink(os.readlink(s), d)
        elif s.is_file():
            shutil.copy2(s, d)
        else:
            continue
        copied.append(rel)
    spec = {"path": rel_dest, "url": url, "branch": branch, "head": head, "base": base,
            "upstream": upstream, "worktree_of": worktree_of}
    (meta / "repo.json").write_text(json.dumps(spec, indent=1) + "\n")
    return {
        "mode": "reference",
        "clone_url": url,
        "branch": branch,
        "head": head,
        "worktree_of": worktree_of,
        "unpushed_commits": int(inv._git(src, "rev-list", "--count", f"{base}..HEAD") or 0),
        "bundle_bytes": bundle_bytes,
        "patch_bytes": len(staged) + len(unstaged),
        "untracked_copied": len(copied),
        "secret_files_left_behind": skipped,
    }


_CONFIG_USERINFO_RE = re.compile(r"(url\s*=\s*[a-z][a-z0-9+.-]*://)[^/@\s]+@", re.I)


def stage_tree(src: Path, dest: Path, picked_files: set[str]) -> dict:
    """Copy a directory as-is, without rebuildable dirs and without
    secret-looking files that were not picked."""
    skipped = []

    def ignore(d, names):
        out = []
        for n in names:
            full = os.path.join(d, n)
            rel = os.path.relpath(full, src)
            if n in inv.SKIP_DIRS and n != ".git":
                out.append(n)
            elif n == ".git" and os.path.isfile(full):
                # A nested worktree or submodule: its .git points at the old
                # machine, so the copy is a plain directory instead.
                out.append(n)
                skipped.append(rel)
            elif inv.is_secret_path(rel) and full not in picked_files and not (n in inv.SECRET_DIRS and os.path.isdir(full) and any(
                    p.startswith(full + os.sep) for p in picked_files)):
                out.append(n)
                skipped.append(rel)
        return out

    shutil.copytree(src, dest, symlinks=True, ignore=ignore)
    for cfg in [dest / ".git" / "config"] + list(dest.glob("**/.git/config")):
        if cfg.is_file():  # remote URLs may embed user:token@
            text = cfg.read_text(errors="surrogateescape")
            cfg.write_text(_CONFIG_USERINFO_RE.sub(r"\1", text), errors="surrogateescape")
    return {"mode": "tree", "bytes": inv._dir_bytes(dest, limit_files=None), "secret_files_left_behind": sorted(set(skipped))}


# --------------------------------------------------------------------------- #
# Sessions
# --------------------------------------------------------------------------- #


@dataclass
class StagedSession:
    name: str
    source: hz.Session
    remote_cwd: str
    resumable: bool
    resume_id: Optional[str]
    files: list[str] = field(default_factory=list)


def _place_claude(s: hz.Session, home: Path, remote_cwd: str, pm: PathMap) -> list[str]:
    proj = home / ".claude" / "projects" / hz.claude_slug(remote_cwd)
    dest = proj / f"{s.id}.jsonl"
    rewrite_jsonl(s.path, dest, pm)
    files = [str(dest.relative_to(home))]
    for sidecar in s.extra:  # sub-agent transcripts and saved tool output
        target = proj / sidecar.name
        for f in sidecar.rglob("*"):
            if f.is_file():
                out = target / f.relative_to(sidecar)
                if f.suffix == ".jsonl":
                    rewrite_jsonl(f, out, pm)
                else:
                    out.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copy2(f, out)
        files.append(str(target.relative_to(home)) + "/")
    return files


def _place_codex(s: hz.Session, home: Path, pm: PathMap) -> list[str]:
    """A Union agent gives each Codex session its own CODEX_HOME, named by the
    session's id, and resumes the oldest rollout found under it."""
    name = s.path.name[: -len(".zst")] if s.path.name.endswith(".zst") else s.path.name
    m = re.match(r"rollout-(\d{4})-(\d{2})-(\d{2})T", name)
    y, mo, d = m.groups() if m else time.strftime("%Y-%m-%d", time.gmtime(s.started_at or time.time())).split("-")
    dest = home / ".agent-sessions" / "codex" / "sessions" / s.id / "sessions" / y / mo / d / name
    rewrite_jsonl(s.path, dest, pm)
    return [str(dest.relative_to(home))]


def handoff_note(st: StagedSession, parsed: hz.Parsed, pm: PathMap, repos: dict, tools: list[inv.Tool]) -> str:
    s = st.source
    lines = [
        f"# Handoff: {s.title or st.name}",
        "",
        f"Moved from a local `{s.harness}` session (`{s.id}`). It could not arrive",
        "resumable on this harness, so this note and the original transcript are",
        "what a new session starts from.",
        "",
        "## Where things are now",
        "",
        f"- Working directory: `{st.remote_cwd}` (was `{s.cwd}`)",
    ]
    for local, info in repos.items():
        lines.append(f"- Repo `{pm.map(local)}` (was `{local}`), branch `{info.get('branch') or 'detached'}`")
    lines += [
        f"- Transcript: `{st.files[0] if st.files else ''}` (paths inside it are the old local ones)",
        "",
        "## Summary",
        "",
        "<!-- The agent running the handoff replaces this comment with a short",
        "summary: the goal, what is done, what is in progress, and the next step. -->",
        "",
        "## First request",
        "",
        _quote(parsed.first_user),
        "",
        "## Last request",
        "",
        _quote(parsed.last_user),
        "",
        "## Last reply",
        "",
        _quote(parsed.last_assistant),
    ]
    if tools:
        lines += ["", "## Tools it used", "", ", ".join(f"`{t.name}` ({t.count})" for t in tools[:30])]
    return "\n".join(lines) + "\n"


def _quote(text: str, limit: int = 4000) -> str:
    text = (text or "").strip()
    if len(text) > limit:
        text = text[:limit] + "\n[...]"
    return "\n".join("> " + line for line in text.splitlines()) if text else "> (none)"


def finished_row(st: StagedSession, harness: str, now: float, memory: str, cpu: str) -> dict:
    """A finished-session record a Union agent lists, and can Restart."""
    cwd = st.remote_cwd
    return {
        "name": st.name,
        "agent": harness,
        "state": "ended",
        "url": None,
        "is_lead": False,
        "started_at": st.source.started_at or now,
        "ended_at": now,
        "memory": memory,
        "cpu": cpu,
        "exit_note": f"handed off from a local {st.source.harness} session",
        "spec": {
            "name": st.name,
            "prompt": None,
            "agent": harness,
            "memory": memory,
            "cpu": cpu,
            "cwd": cwd,
            "resume_session_id": st.resume_id,
            "browser": False,
            "browser_inherit_profile": True,
            "shell": False,
        },
        "run": "handoff",
        # Keyed by where the session came from, so two sessions with the same
        # title never collide and re-importing one is recognisable.
        "key": f"handoff:{st.source.harness}:{st.source.id}",
    }


def merge_finished(existing: dict, rows: list[dict]) -> tuple[dict, list[str]]:
    """Add ``rows`` to an existing finished list without replacing anything.

    A row whose key is already there (the same session handed off before) is
    skipped and its key returned. A row whose name is taken by a different
    session gets a suffix, since names are what the agent lists."""
    current = list(existing.get("sessions", []))
    keys = {r.get("key") for r in current}
    names = {r.get("name") for r in current}
    skipped = []
    for row in rows:
        if row["key"] in keys:
            skipped.append(row["key"])
            continue
        name, n = row["name"], 2
        while name in names:
            name, n = f"{row['name']}-{n}", n + 1
        if name != row["name"]:
            row = {**row, "name": name, "spec": {**row["spec"], "name": name}}
        current.append(row)
        keys.add(row["key"])
        names.add(name)
    return {**existing, "sessions": current}, skipped


# --------------------------------------------------------------------------- #
# Setup script
# --------------------------------------------------------------------------- #

SETUP_HEADER = """#!/usr/bin/env bash
# Installs the tools the handed-off sessions used that this image may lack.
# Generated by the union-handoff skill; review it, then run it once:
#   bash ~/.handoff/setup.sh
# Binaries were never copied: a binary built for the old machine (say macOS)
# does not run here. Each line below installs a Linux build instead.
set -u
cd "$HOME"

have() { command -v "$1" >/dev/null 2>&1; }
apt_install() {
  if have apt-get; then
    if [ "$(id -u)" = 0 ]; then apt-get install -y "$@"; else sudo apt-get install -y "$@"; fi
  else
    echo "no apt-get here; install $* by hand" >&2
  fi
}
brew_install() {
  if have brew; then brew install "$@"; else echo "no Homebrew here; install $* by hand" >&2; fi
}
FAILED=""
need() {  # need <binary> <install command...>
  local bin="$1"; shift
  if have "$bin"; then echo "ok    $bin"; return; fi
  echo "setup $bin: $*"
  eval "$@"
  hash -r
  if ! have "$bin"; then echo "FAILED $bin" >&2; FAILED="$FAILED $bin"; fi
}

# Files copied from another machine keep that machine's user id, and git
# refuses to work in a repo owned by someone else ("dubious ownership").
for repo in "$HOME"/src/*/; do
  [ -d "$repo" ] || continue
  if [ "$(stat -c %u "$repo")" != "$(id -u)" ]; then
    chown -R "$(id -u):$(id -g)" "$repo" 2>/dev/null \
      || git config --global --add safe.directory "${repo%/}"
  fi
done

if have apt-get && [ "$(id -u)" = 0 ]; then apt-get update -qq || true; fi
"""


def setup_script(tools: list[inv.Tool], restore_repos: bool = False) -> str:
    lines = [SETUP_HEADER]
    if restore_repos:
        lines += [
            "# Re-clone the repos that travelled by reference and reapply their",
            "# local work. Needs git access to their hosts (sign in first).",
            'python3 "$HOME/.handoff/restore_repos.py" || FAILED="$FAILED repos"',
            "",
        ]
    todo = []
    for t in tools:
        line = t.linux_install
        if not line:
            continue
        if line.startswith("#"):
            # Still checked: setup fails until this line is filled in or removed.
            todo += [f"{line}   (used {t.count}x)", f"need {t.name} ':'"]
        else:
            lines.append(f"need {t.name} {_shq(line)}   # used {t.count}x, locally from {t.installed_by}")
    if todo:
        lines += ["", "# Could not work these out; replace each ':' with an install command, or delete the pair:"] + todo
    lines += [
        "",
        'if [ -n "$FAILED" ]; then',
        '  echo "setup incomplete, still missing:$FAILED (fix the lines above and run it again)" >&2',
        "  exit 1",
        "fi",
        'touch "$HOME/.handoff/setup.done"',
        'echo "setup finished"',
        "",
    ]
    return "\n".join(lines)


def _shq(s: str) -> str:
    return "'" + s.replace("'", "'\\''") + "'"


# --------------------------------------------------------------------------- #
# Credentials
# --------------------------------------------------------------------------- #


def transcript_secrets(s: hz.Session) -> dict[str, int]:
    """Kinds of credential-looking values in everything copied for a session."""
    files = [s.path] if s.path.is_file() and s.path.suffix != ".db" else []
    for extra in s.extra:
        files += [f for f in extra.rglob("*") if f.is_file()] if extra.is_dir() else [extra]
    if s.path.is_dir():
        files += [f for f in s.path.rglob("*") if f.is_file()]
    found: dict[str, int] = {}
    for f in files:
        for kind, n in inv.scan_secret_values(hz.iter_text_lines(f)).items():
            found[kind] = found.get(kind, 0) + n
    if s.path.suffix == ".db":
        text = "\n".join(t for _, t in hz.parse(s).turns)
        for kind, n in inv.scan_secret_values(text.splitlines()).items():
            found[kind] = found.get(kind, 0) + n
    return found


def validate_credentials(ids: set[str], repo_paths: list[Path], local_home: Path) -> set[str]:
    """Check every picked credential id before anything is written, and
    return the absolute paths of picked files inside repos."""
    creds, _ = inv.credentials(local_home)
    by_id = {c.id: c for c in creds}
    picked: set[str] = set()
    for cid in ids:
        if cid.startswith("file:"):
            p = Path(cid[len("file:"):])
            top = next((t for t in repo_paths if t in p.parents), None)
            if not (p.is_file() and top is not None and inv.is_secret_path(str(p.relative_to(top)))):
                raise SystemExit(f"credential {cid!r} is not a secret-looking file inside a repo being moved")
            picked.add(str(p))
            continue
        c = by_id.get(cid)
        if c is None or not c.copyable or not c.rel:
            raise SystemExit(f"credential {cid!r} is unknown or cannot be copied")
    return picked


# --------------------------------------------------------------------------- #
# Orchestration
# --------------------------------------------------------------------------- #


@dataclass
class StageOptions:
    out: Path
    remote_harness: str
    remote_home: str = "/root"
    credentials: set[str] = field(default_factory=set)
    tree_repos: set[str] = field(default_factory=set)
    ship_repos: set[str] = field(default_factory=set)  # bundle the whole history instead of re-cloning
    exclude_repos: set[str] = field(default_factory=set)
    include_dirs: set[str] = field(default_factory=set)
    allow_transcript_secrets: bool = False
    memory: str = "2Gi"
    cpu: str = "2"
    log: Callable[[str], None] = print


def stage(sessions: list[hz.Session], opts: StageOptions) -> dict:
    local_home = hz.home()
    out = opts.out.resolve()
    if out.exists() and any(out.iterdir()):
        raise SystemExit(f"{out} is not empty; stage into a new directory")
    rh = opts.remote_home.rstrip("/")

    parsed = {s.key: hz.parse(s) for s in sessions}

    # Refuse before writing anything: credential-looking values in what would
    # be copied, unless the user has seen the report and said yes.
    found = {s.key: transcript_secrets(s) for s in sessions}
    found = {k: v for k, v in found.items() if v}
    if found and not opts.allow_transcript_secrets:
        lines = [f"  {k}: " + ", ".join(f"{n} {kind}" for kind, n in v.items()) for k, v in found.items()]
        raise SystemExit(
            "These transcripts contain values that look like credentials, and a transcript is copied whole:\n"
            + "\n".join(lines)
            + "\nRevoke or rotate them, leave those sessions out, or pass --allow-transcript-secrets."
        )

    # 1. Repos, and where each lands.
    repo_paths: list[Path] = []
    for s in sessions:
        for top in inv.repos_for(parsed[s.key].cwds or [s.cwd], parsed[s.key].paths, local_home):
            if top not in repo_paths and str(top) not in opts.exclude_repos:
                repo_paths.append(top)
    for d in sorted(opts.include_dirs):
        top = inv.git_toplevel(Path(d)) or Path(d).resolve()
        if top not in repo_paths:
            repo_paths.append(top)
    harness_dirs = [hz.claude_root(), hz.codex_root(), local_home / ".agents", hz.pi_root(), hz.hermes_root(),
                    local_home / ".config" / "opencode"]
    for top in repo_paths + harness_dirs:
        if out == top or top in out.parents:
            raise SystemExit(f"--out {out} is inside {top}, which is copied from; stage somewhere else")

    # The uncommitted changes travel as patches; the same rule as transcripts.
    for top in repo_paths:
        if inv.is_git(top) and inv.has_commits(top):
            patches = b"".join(subprocess.run(["git", "-C", str(top), "diff", *a, "--binary"], capture_output=True).stdout
                               for a in (["--cached"], []))
            kinds = inv.scan_secret_values(patches.decode(errors="replace").splitlines())
            if kinds:
                found[f"uncommitted changes in {top}"] = kinds
    if found and not opts.allow_transcript_secrets:
        lines = [f"  {k}: " + ", ".join(f"{n} {kind}" for kind, n in v.items()) for k, v in found.items()]
        raise SystemExit(
            "Uncommitted changes contain values that look like credentials, and they are copied as they are:\n"
            + "\n".join(lines)
            + "\nRevoke or rotate them, leave that repo out (--exclude), or pass --allow-transcript-secrets."
        )

    picked_files = validate_credentials(opts.credentials, repo_paths, local_home)
    home = out / "home"
    work = out / "work"
    home.mkdir(parents=True)
    work.mkdir()
    pm = PathMap()
    names: set[str] = set()
    remote_of: dict[str, str] = {}
    for top in repo_paths:
        base = slugify(top.name, 60) if not re.match(r"^[\w.-]+$", top.name) else top.name
        name, n = base, 2
        while name in names:
            name, n = f"{base}-{n}", n + 1
        names.add(name)
        remote_of[str(top)] = f"{rh}/src/{name}"
        pm.add(str(top), f"{rh}/src/{name}")
    pm.add(str(local_home), rh)

    # Worktrees of one repo: the main checkout is cloned, the others are added
    # to it as worktrees, as they were here.
    primary_of: dict[str, Optional[str]] = {}
    groups: dict[str, list[Path]] = {}
    for top in repo_paths:
        if inv.is_git(top):
            common = inv._git(top, "rev-parse", "--path-format=absolute", "--git-common-dir")
            groups.setdefault(common or str(top), []).append(top)
    for members in groups.values():
        main = next((m for m in members if (m / ".git").is_dir()), members[0])
        for m in members:
            primary_of[str(m)] = None if m == main else remote_of[str(main)][len(rh) + 1:]

    by_reference = {
        str(top): (inv.is_git(top) and inv.has_commits(top) and str(top) not in opts.ship_repos
                   and str(top) not in opts.tree_repos and all(inv.remote_base(top)[1:]))
        for top in repo_paths
    }
    main_path = {remote_of[str(t)][len(rh) + 1:]: str(t) for t in repo_paths}

    repos: dict[str, dict] = {}
    for top in repo_paths:
        rel_dest = remote_of[str(top)][len(rh) + 1:]
        dest = home / rel_dest
        is_git = inv.is_git(top)
        if by_reference[str(top)]:
            primary = primary_of.get(str(top))
            if primary and not by_reference[main_path[primary]]:
                primary = None  # its main checkout travels whole, so clone this one on its own
            opts.log(f"repo  {top} -> {remote_of[str(top)]} (re-cloned on first boot)")
            info = stage_git_reference(top, rel_dest, home / ".handoff" / "repos" / dest.name, picked_files, primary)
            info["remote"] = remote_of[str(top)]
            repos[str(top)] = info
            continue
        dest.parent.mkdir(parents=True, exist_ok=True)
        as_tree = str(top) in opts.tree_repos
        if as_tree and (top / ".git").is_file():
            # A linked worktree's .git points at the old machine; a copied tree
            # would carry a broken repo. Rebuild it from a bundle instead.
            opts.log(f"note  {top} is a git worktree, so it travels as a bundle, not a tree")
            as_tree = False
        if is_git and inv.has_commits(top) and not as_tree:
            opts.log(f"repo  {top} -> {remote_of[str(top)]} (bundle)")
            info = stage_git_repo(top, dest, work, picked_files)
        else:
            opts.log(f"repo  {top} -> {remote_of[str(top)]} (tree)")
            info = stage_tree(top, dest, picked_files)
        info["remote"] = remote_of[str(top)]
        repos[str(top)] = info

    # Sessions whose cwd is not inside a staged repo still need somewhere to
    # land: their cwd under home if it was under home, else home itself.
    def remote_cwd(local: Optional[str]) -> str:
        if not local:
            return rh
        mapped = pm.map(local)
        if mapped == local:  # outside home and every repo
            return rh
        return mapped

    # 2. Sessions.
    staged: list[StagedSession] = []
    used_names: set[str] = set()
    tools_all = inv.tools_from([c for p in parsed.values() for c in p.commands], local_home)
    for s in sessions:
        p = parsed[s.key]
        base = slugify(s.title or f"{s.harness}-{s.id[:8]}")
        name, n = base, 2
        while name in used_names:
            name, n = f"{base}-{n}", n + 1
        used_names.add(name)
        rcwd = remote_cwd(s.cwd)
        resumable = s.harness == opts.remote_harness and s.harness in hz.REMOTE_HARNESSES
        st = StagedSession(name, s, rcwd, resumable, s.id if resumable else None)
        if resumable and s.harness == "claude":
            st.files = _place_claude(s, home, rcwd, pm)
        elif resumable and s.harness == "codex":
            st.files = _place_codex(s, home, pm)
        else:
            d = home / "handoff" / name
            d.mkdir(parents=True)
            if s.path.is_dir():
                shutil.copytree(s.path, d / "transcript", symlinks=True)
                st.files = [str((d / "transcript").relative_to(home)) + "/"]
            elif s.path.suffix == ".db":
                (d / "transcript.md").write_text(_db_transcript(s, p))
                st.files = [str((d / "transcript.md").relative_to(home))]
            else:
                tname = "transcript" + (".jsonl" if s.path.name.endswith((".jsonl", ".jsonl.zst")) else s.path.suffix)
                with (d / tname).open("w") as fh:
                    fh.writelines(hz.iter_text_lines(s.path))
                st.files = [str((d / tname).relative_to(home))]
            for extra in s.extra:  # sub-agent transcripts, message and part files
                target = d / "extra" / extra.name
                if extra.is_dir():
                    shutil.copytree(extra, target, symlinks=True)
                else:
                    target.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copy2(extra, target)
            if s.extra:
                st.files.append(str((d / "extra").relative_to(home)) + "/")
            session_repos = {k: v for k, v in repos.items() if any(
                c == k or c.startswith(k + "/") for c in (p.cwds or [s.cwd or ""]))}
            tools = inv.tools_from(p.commands, local_home)
            (d / "HANDOFF.md").write_text(handoff_note(st, p, pm, session_repos, tools))
            st.files.append(str((d / "HANDOFF.md").relative_to(home)))
        opts.log(f"session {s.key} -> {name} ({'resumable' if resumable else 'handoff note'})")
        staged.append(st)

    # 3. Memory, instructions and skills from the local harness(es).
    extras = _stage_harness_files(sessions, home, opts.remote_harness, rh, pm, staged)

    # 4. Credentials the user picked.
    creds, _ = inv.credentials(local_home)
    by_id = {c.id: c for c in creds}
    copied_creds = []
    for cid in sorted(opts.credentials):
        if cid.startswith("file:"):
            src = Path(cid[len("file:"):])
            top = next(t for t in repo_paths if t in src.parents)
            if (home / remote_of[str(top)][len(rh) + 1:] / src.relative_to(top)).exists():
                copied_creds.append(cid)  # copied with its repo above
            continue
        c = by_id[cid]
        src, dest = Path(c.path), home / c.rel
        dest.parent.mkdir(parents=True, exist_ok=True)
        if src.is_dir():
            shutil.copytree(src, dest, symlinks=True)
        else:
            shutil.copy2(src, dest)
            dest.chmod(stat.S_IRUSR | stat.S_IWUSR)
        copied_creds.append(cid)
        opts.log(f"credential {cid} -> ~/{c.rel}")

    # 5. Setup script, finished records, notes.
    hdir = home / ".handoff"
    hdir.mkdir(exist_ok=True)
    restores = any(r.get("mode") == "reference" for r in repos.values())
    if restores:
        shutil.copy2(Path(__file__).with_name("restore_repos.py"), hdir / "restore_repos.py")
    (hdir / "setup.sh").write_text(setup_script(tools_all, restore_repos=restores))
    (hdir / "setup.sh").chmod(0o755)

    now = time.time()
    rows = [finished_row(st, opts.remote_harness, now, opts.memory, opts.cpu)
            for st in staged if st.resumable]
    for row in rows:
        row["spec"]["cwd"] = _rel(row["spec"]["cwd"], rh)
    if rows:
        f = home / ".agent-sessions" / "finished.json"
        f.parent.mkdir(parents=True, exist_ok=True)
        f.write_text(json.dumps(merge_finished({}, rows)[0], indent=1) + "\n")

    readme = _readme(staged, repos, pm, copied_creds, opts.remote_harness)
    (hdir / "README.md").write_text(readme, errors="backslashreplace")  # filenames need not be UTF-8
    _append_section(home / ("CLAUDE.md" if opts.remote_harness == "claude" else "AGENTS.md"), HANDOFF_POINTER)

    manifest = {
        "created_at": now,
        "remote_harness": opts.remote_harness,
        "remote_home": rh,
        "path_map": pm.to_dict(),
        "repos": repos,
        "sessions": [
            {"name": st.name, "source": st.source.key, "title": st.source.title, "remote_cwd": st.remote_cwd,
             "resumable": st.resumable, "resume_id": st.resume_id, "files": st.files}
            for st in staged
        ],
        "credentials_copied": copied_creds,
        "transcript_secrets": found,
        "extras": extras,
        "tools": [t.to_dict() for t in tools_all],
    }
    (hdir / "manifest.json").write_text(json.dumps(manifest, indent=1) + "\n", errors="backslashreplace")
    shutil.rmtree(work, ignore_errors=True)
    manifest["home"] = str(home)
    manifest["bytes"] = inv._dir_bytes(home, skip=set(), limit_files=None)
    return manifest


def _rel(path: str, rh: str) -> Optional[str]:
    if path == rh:
        return None
    return path[len(rh) + 1:] if path.startswith(rh + "/") else None


HANDOFF_POINTER = """## Handed-off sessions

Some sessions in this home were moved here from a local machine. Before
running anything they need, check `~/.handoff/README.md`; if
`~/.handoff/setup.done` does not exist, run `bash ~/.handoff/setup.sh` once to
install the tools they used.
"""


def _append_section(path: Path, text: str) -> None:
    existing = path.read_text() if path.exists() else ""
    if text.strip() in existing:
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text((existing.rstrip() + "\n\n" if existing.strip() else "") + text)


def _readme(staged: list[StagedSession], repos: dict, pm: PathMap, creds: list[str], harness: str) -> str:
    lines = ["# Handoff", "", "Moved here from a local machine by the union-handoff skill.", "", "## Sessions", ""]
    for st in staged:
        how = "resume it with Restart" if st.resumable else f"start a new session from `~/{st.files[-1]}`"
        lines.append(f"- `{st.name}`: {st.source.title or st.source.id} ({how}; cwd `{st.remote_cwd}`)")
    lines += ["", "## Repos", ""]
    for local, info in repos.items():
        lines.append(f"- `{info['remote']}` from a {info['mode']}, branch `{info.get('branch') or '-'}`")
        if info.get("secret_files_left_behind"):
            lines.append(f"  - left behind (secret-looking, not picked): {', '.join(info['secret_files_left_behind'])}")
    lines += ["", "## Credentials", ""]
    lines.append("Copied: " + (", ".join(f"`{c}`" for c in creds) if creds else "none. Sign in again where a tool needs it."))
    lines += ["", "## Tools", "", "Run `bash ~/.handoff/setup.sh` once. Review it first."]
    return "\n".join(lines) + "\n"


def _db_transcript(s: hz.Session, p: hz.Parsed) -> str:
    """The whole conversation, in order, from a harness that keeps it in a
    database: every message and every shell command."""
    out = [f"# {s.harness} session {s.id}", "", "Exported in order from the harness's database.", ""]
    for role, text in p.turns:
        out += [f"## {role}", "", text, ""]
    return "\n".join(out)


def _copy_missing(src: Path, dest: Path, skipped: Optional[list] = None) -> int:
    """Copy a tree without overwriting anything already staged, and without
    secret-looking files or symlinks that point outside it."""
    n = 0
    root = src.resolve()
    for f in src.rglob("*"):
        rel = f.relative_to(src)
        if any((src / parent).is_symlink() for parent in rel.parents if str(parent) != "."):
            continue  # under a symlinked directory, which is recreated as a link below
        if f.is_symlink():
            target = f.resolve()
            if target != root and root not in target.parents:
                if skipped is not None:
                    skipped.append(str(f))  # points outside: may be a login file
                continue
            out = dest / rel
            if not os.path.lexists(out):
                out.parent.mkdir(parents=True, exist_ok=True)
                os.symlink(os.readlink(f), out)
                n += 1
            continue
        if not f.is_file():
            continue
        if inv.is_secret_path(str(rel)):
            if skipped is not None:
                skipped.append(str(f))
            continue
        out = dest / rel
        if out.exists():
            continue
        out.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(f, out)
        n += 1
    return n


def _stage_harness_files(sessions, home: Path, remote: str, rh: str, pm: PathMap, staged) -> dict:
    """Memory, user-level instructions and user-level skills."""
    local_home = hz.home()
    done: dict[str, list[str]] = {"memory": [], "instructions": [], "skills": [], "left_behind": []}
    harnesses = {s.harness for s in sessions}

    # Claude keeps per-project memory beside the transcripts.
    for st in staged:
        s = st.source
        if s.harness != "claude" or not s.cwd:
            continue
        mem = s.path.parent / "memory"
        if not mem.is_dir():
            continue
        if remote == "claude":
            dest = home / ".claude" / "projects" / hz.claude_slug(st.remote_cwd) / "memory"
        else:
            dest = home / ".handoff" / "memory" / "claude" / hz.claude_slug(s.cwd)
        if _copy_missing(mem, dest, done["left_behind"]):
            done["memory"].append(str(dest.relative_to(home)))

    instructions = {
        "claude": [hz.claude_root() / "CLAUDE.md"],
        "codex": [hz.codex_root() / "AGENTS.md"],
        "pi": [hz.pi_root() / "AGENTS.md"],
        "opencode": [local_home / ".config" / "opencode" / "AGENTS.md"],
        "hermes": [hz.hermes_root() / "SOUL.md"],
    }
    for h in sorted(harnesses):
        for f in instructions.get(h, []):
            if not f.is_file():
                continue
            if h == "claude" and remote == "claude":
                dest = home / ".claude" / "CLAUDE.md"
            else:
                dest = home / ".handoff" / "instructions" / h / f.name
            if not dest.exists():
                dest.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(f, dest)
                done["instructions"].append(str(dest.relative_to(home)))

    skill_dirs = {
        "claude": [hz.claude_root() / "skills"],
        "codex": [local_home / ".agents" / "skills", hz.codex_root() / "skills"],
        "pi": [hz.pi_root() / "skills"],
        "opencode": [local_home / ".config" / "opencode" / "skills"],
        "hermes": [hz.hermes_root() / "skills"],
    }
    target = home / (".claude/skills" if remote == "claude" else ".agents/skills")
    for h in sorted(harnesses):
        for d in skill_dirs.get(h, []):
            if not d.is_dir():
                continue
            for skill in sorted(d.iterdir()):
                if not (skill / "SKILL.md").is_file() or skill.name.startswith("."):
                    continue
                dest = target / skill.name
                if dest.exists():
                    continue
                if _copy_missing(skill, dest, done["left_behind"]):
                    done["skills"].append(skill.name)
    return done
