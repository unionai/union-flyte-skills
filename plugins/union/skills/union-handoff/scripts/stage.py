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
  .handoff/xdg-config/           what belongs in ~/.config (see RESERVED_ROOT_NAMES)
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
        # As configured, not as this machine's insteadOf rules rewrite it.
        url = inv.redact_url(inv._git(src, "config", "--get", f"remote.{remote}.url"))
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
        if inv.is_rebuilt_or_junk(rel, keep_git=True):
            continue  # a virtualenv, node_modules or build output that is not ignored
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
                        worktree_of: Optional[str], plan: dict, all_branches: bool = False) -> dict:
    """Record a repo to be re-cloned from its remote on first boot, and ship
    only what the remote lacks, in one bundle: the commits of HEAD and of the
    branches checked out in its worktrees (with ``all_branches``, of every
    local branch and tag) not reachable from the remote's current tips. Plus
    the staged and unstaged patches and the untracked files.
    ~/.handoff/restore_repos.py puts it back together. ``plan`` is
    inventory.reference_plan(src)."""
    branch = inv._git(src, "symbolic-ref", "--quiet", "--short", "HEAD") or None
    head = inv._git(src, "rev-parse", "HEAD", check=True)
    meta.mkdir(parents=True)
    has = plan["has"]
    refs = inv.shipped_refs(src, all_branches)
    unpushed = int(inv._git(src, "rev-list", "--count", *refs, "--not", *has) or 0)
    bundle_bytes = 0
    if unpushed:
        bundle = meta / "unpushed.bundle"
        _run(["git", "-C", str(src), "bundle", "create", str(bundle), *refs, "--not", *has])
        bundle_bytes = bundle.stat().st_size
    left_behind = inv.branches_ahead(src, has, exclude=refs)
    branches = {}
    for line in inv._git(src, "for-each-ref", "--format=%(refname:short) %(objectname) %(upstream:short)",
                         "refs/heads").splitlines():
        name, sha, *up = line.split()
        if name in left_behind:
            continue  # its commits are not shipped, so the agent could not recreate it
        branches[name] = {"head": sha, "upstream": up[0] if up else None}
    staged = subprocess.run(["git", "-C", str(src), "diff", "--cached", "--binary"], capture_output=True).stdout
    unstaged = subprocess.run(["git", "-C", str(src), "diff", "--binary"], capture_output=True).stdout
    for name, patch in (("staged.patch", staged), ("unstaged.patch", unstaged)):
        if patch.strip():
            (meta / name).write_bytes(patch)
    copied, skipped = [], []
    for rel in inv.untracked(src):
        s, d = src / rel, meta / "untracked" / rel
        if inv.is_rebuilt_or_junk(rel, keep_git=True):
            continue
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
    spec = {"path": rel_dest, "url": plan["url"], "remote": plan["remote"], "branch": branch, "head": head,
            "upstream": plan["upstream"], "worktree_of": worktree_of, "branches": branches}
    (meta / "repo.json").write_text(json.dumps(spec, indent=1) + "\n")
    return {
        "mode": "reference",
        "clone_url": plan["url"],
        "branch": branch,
        "head": head,
        "worktree_of": worktree_of,
        "unpushed_commits": unpushed,
        "branches_left_behind": left_behind,
        "bundle_bytes": bundle_bytes,
        "patch_bytes": len(staged) + len(unstaged),
        "untracked_bytes": inv._dir_bytes(meta / "untracked", skip=set(), limit_files=None),
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
            if (n in inv.SKIP_DIRS and n != ".git") or inv.JUNK_FILE_RE.match(n):
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
copy_missing() {  # copy_missing <from> <to>: copies what <to> lacks, never overwrites
  (cd "$1" && find . \\( -type f -o -type l \\) -print) | while IFS= read -r f; do
    if [ ! -e "$2/$f" ] && [ ! -L "$2/$f" ]; then
      mkdir -p "$2/$(dirname "$f")" && cp -pP "$1/$f" "$2/$f" || exit 1
    fi
  done
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


XDG_CONFIG_BLOCK = """# ~/.config travelled as ~/.handoff/xdg-config: a Union Volume reserves the
# name .config at its root. Put it where this machine's tools look for it.
if [ -d "$HOME/.handoff/xdg-config" ]; then
  cfg="${XDG_CONFIG_HOME:-$HOME/.config}"
  if [ -e "$cfg" ] && [ ! -d "$cfg" ]; then
    cfg="$HOME/.xdg-config"
    grep -qs 'XDG_CONFIG_HOME=' "$HOME/.bashrc" || echo 'export XDG_CONFIG_HOME="$HOME/.xdg-config"' >> "$HOME/.bashrc"
    echo "~/.config is not a directory here; using $cfg (XDG_CONFIG_HOME, exported in ~/.bashrc)"
  fi
  export XDG_CONFIG_HOME="$cfg"  # on every run, so a rerun's gh finds it too
  if [ ! -e "$HOME/.handoff/xdg-config.done" ]; then
    if mkdir -p "$cfg" && copy_missing "$HOME/.handoff/xdg-config" "$cfg"; then
      touch "$HOME/.handoff/xdg-config.done"
    else
      FAILED="$FAILED xdg-config"
    fi
  fi
fi
"""


def _github_block(ssh: bool) -> list[str]:
    """Sign in to GitHub before the repos are re-cloned, and, when their remotes
    use SSH but no SSH key was copied, reach them over HTTPS instead."""
    lines = ["# Sign in to GitHub with the GitHub CLI before re-cloning. With no login yet it",
             "# prints a one-time code and a link, then waits until the code is entered there."]
    if ssh:
        lines += ["# These repos' remotes use SSH and no SSH key was copied, so they are reached",
                  "# over HTTPS instead."]
    lines += [
        "need gh 'apt_install gh'",
        "if have gh && ! gh auth status >/dev/null 2>&1; then",
        '  echo "Signing in to GitHub: open the link below and enter the one-time code." >&2',
        "  gh auth login --hostname github.com --git-protocol https --web --skip-ssh-key </dev/null || true",
        "fi",
    ]
    if ssh:
        lines.append("gh config set git_protocol https 2>/dev/null || true")
    lines += [
        "if have gh && gh auth status >/dev/null 2>&1; then",
        "  gh auth setup-git",
        "else",
        '  echo "gh is not signed in: run gh auth login, then run this script again" >&2',
        '  FAILED="$FAILED gh-login"',
        "fi",
    ]
    if ssh:
        lines += [
            "for from in git@github.com: ssh://git@github.com/; do",
            '  git config --global --get-all url.https://github.com/.insteadOf | grep -qxF "$from" \\',
            '    || git config --global --add url.https://github.com/.insteadOf "$from"',
            "done",
        ]
    return lines + [""]


def _ssh_hosts_block(hosts: list[str]) -> list[str]:
    """SSH remotes on hosts other than GitHub, for an agent that has no SSH key."""
    return [f'echo "{h}: its repos use SSH and no SSH key was copied; sign in to {h} over HTTPS or add a key" >&2'
            for h in sorted(set(hosts)) if h != "github.com"]


def setup_script(tools: list[inv.Tool], restore_repos: bool = False, xdg_config: bool = False,
                 ssh_hosts: tuple = (), github: bool = False) -> str:
    lines = [SETUP_HEADER]
    if xdg_config:
        lines.append(XDG_CONFIG_BLOCK)
    github_ssh = any(h == "github.com" for h in ssh_hosts)
    if github or github_ssh:
        lines += _github_block(ssh=github_ssh)
    lines += _ssh_hosts_block(list(ssh_hosts))
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
    allow_flagged_transcripts: bool = False
    all_branches: bool = False  # ship every local branch's unpushed commits, not only HEAD's and worktrees'
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
    if found and not opts.allow_flagged_transcripts:
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
    if found and not opts.allow_flagged_transcripts:
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

    plans: dict[str, dict] = {}
    for top in repo_paths:
        if inv.is_git(top) and inv.has_commits(top) and str(top) not in opts.ship_repos | opts.tree_repos:
            plans[str(top)] = inv.reference_plan(top)
            if not plans[str(top)]["has"]:
                opts.log(f"note  {top} travels whole: {plans[str(top)]['note']}")
    by_reference = {str(top): bool(plans.get(str(top), {}).get("has")) for top in repo_paths}
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
            info = stage_git_reference(top, rel_dest, home / ".handoff" / "repos" / dest.name, picked_files, primary,
                                       plans[str(top)], opts.all_branches)
            if info["branches_left_behind"]:
                n = sum(info["branches_left_behind"].values())
                opts.log(f"note  {top}: {n} unpushed commit(s) on {len(info['branches_left_behind'])} other branch(es)"
                         " left behind (--all-branches ships them)")
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
    not_found = set().union(*(p.not_found for p in parsed.values()))
    tools_all = inv.tools_from([c for p in parsed.values() for c in p.commands], local_home, not_found)
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
            tools = inv.tools_from(p.commands, local_home, p.not_found)
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
            rel_dest = remote_of[str(top)][len(rh) + 1:]
            rel = src.relative_to(top)
            if ((home / rel_dest / rel).exists()
                    or (home / ".handoff" / "repos" / Path(rel_dest).name / "untracked" / rel).exists()):
                copied_creds.append(cid)  # copied with its repo above
            continue
        c = by_id[cid]
        src, dest = Path(c.path), home / c.rel
        dest.parent.mkdir(parents=True, exist_ok=True)
        if src.is_dir():
            # gcloud keeps its own virtualenv and logs beside the login.
            shutil.copytree(src, dest, symlinks=True, ignore=lambda d, names: [
                n for n in names if n in inv.SKIP_DIRS or n in ("virtenv", "logs") or inv.JUNK_FILE_RE.match(n)])
        else:
            shutil.copy2(src, dest)
            dest.chmod(stat.S_IRUSR | stat.S_IWUSR)
        copied_creds.append(cid)
        opts.log(f"credential {cid} -> ~/{c.rel}")

    # 5. Setup script, finished records, notes.
    hdir = home / ".handoff"
    hdir.mkdir(exist_ok=True)
    relocated = relocate_reserved(home)
    for name, where in relocated.items():
        opts.log(f"note  ~/{name} is staged as ~/{where}: a Union Volume reserves {name} at its root;"
                 " setup.sh puts it back")
    restores = any(r.get("mode") == "reference" for r in repos.values())
    if restores:
        shutil.copy2(Path(__file__).with_name("restore_repos.py"), hdir / "restore_repos.py")
    ssh_key_copied = any(c.startswith("ssh:") for c in copied_creds)
    ssh_hosts = sorted({h for r in repos.values() if r.get("mode") == "reference"
                        for h in [inv.ssh_host(r.get("clone_url"))] if h}) if not ssh_key_copied else []
    # Sign in to GitHub when the sessions used gh, or a re-cloned repo needs it
    # (HTTPS, or SSH with no key copied).
    github = any(t.name == "gh" for t in tools_all) or any(
        inv.remote_host(r.get("clone_url")) == "github.com" and not (ssh_key_copied and inv.ssh_host(r.get("clone_url")))
        for r in repos.values() if r.get("mode") == "reference")
    (hdir / "setup.sh").write_text(setup_script(tools_all, restore_repos=restores, xdg_config=".config" in relocated,
                                                ssh_hosts=tuple(ssh_hosts), github=github))
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
             "resumable": st.resumable, "resume_id": st.resume_id, "files": st.files,
             # What refresh_transcripts needs to copy the transcript again.
             "harness": st.source.harness, "id": st.source.id, "source_path": str(st.source.path),
             "extra": [str(e) for e in st.source.extra]}
            for st in staged
        ],
        "allow_flagged_transcripts": opts.allow_flagged_transcripts,
        "relocated": relocated,
        "credentials_copied": copied_creds,
        "transcript_secrets": found,
        "extras": extras,
        "tools": [t.to_dict() for t in tools_all],
    }
    (hdir / "manifest.json").write_text(json.dumps(manifest, indent=1) + "\n", errors="backslashreplace")
    shutil.rmtree(work, ignore_errors=True)
    manifest["home"] = str(home)
    manifest["bytes"] = inv._dir_bytes(home, skip=set(), limit_files=None)
    # What the report estimated: transcripts plus what each repo ships.
    manifest["estimate_bytes"] = sum(s.size_bytes for s in sessions) + sum(
        r.get("bundle_bytes", 0) + r.get("patch_bytes", 0) + r.get("untracked_bytes", 0) + r.get("bytes", 0)
        for r in repos.values())
    if manifest["bytes"] > 3 * manifest["estimate_bytes"] + 20 * 1024 * 1024:
        manifest["largest"] = largest_dirs(home)
        opts.log(f"WARNING: staged {manifest['bytes'] // 1024 // 1024} MB, far above the"
                 f" {manifest['estimate_bytes'] // 1024 // 1024} MB estimated. Largest directories:")
        for rel, n in manifest["largest"]:
            opts.log(f"  {n // 1024 // 1024:>6} MB  {rel}")
        opts.log("Look for something that should not travel (a cache, a virtualenv, build output) before sealing.")
    return manifest


def largest_dirs(home: Path, depth: int = 3, top: int = 5) -> list[tuple[str, int]]:
    """The biggest directories in a staged home, ``depth`` levels down."""
    sizes: dict[str, int] = {}
    for dirpath, _, filenames in os.walk(home):
        rel = Path(dirpath).relative_to(home).parts[:depth]
        if not rel:
            continue
        key = "/".join(rel)
        for f in filenames:
            try:
                sizes[key] = sizes.get(key, 0) + os.lstat(os.path.join(dirpath, f)).st_size
            except OSError:
                pass
    return sorted(sizes.items(), key=lambda kv: kv[1], reverse=True)[:top]


# A Union Volume is a JuiceFS file system, which exposes these names at its
# root as virtual, read-only entries: nothing can be written under them. A
# home that has one (most have ~/.config) is staged under another name, and
# setup.sh puts it where the tools look.
RESERVED_ROOT_NAMES = {".config", ".stats", ".accesslog", ".trash", ".control"}
RELOCATED_TO = {".config": ".handoff/xdg-config"}


def reserved_in(home: Path) -> list[str]:
    """Top-level names in a staged home that a Union Volume reserves."""
    return sorted(n for n in RESERVED_ROOT_NAMES if os.path.lexists(home / n))


def relocate_reserved(home: Path) -> dict[str, str]:
    """Move each reserved top-level name out of the way. Returns name -> new path."""
    moved = {}
    for name in reserved_in(home):
        where = RELOCATED_TO.get(name, f".handoff/reserved/{name.lstrip('.')}")
        dest = home / where
        dest.parent.mkdir(parents=True, exist_ok=True)
        if os.path.lexists(dest):
            # Never reached by a fresh stage (its --out is empty); refuse
            # rather than mix two copies or drop a picked credential.
            raise SystemExit(f"cannot move ~/{name} aside: {dest} already exists")
        (home / name).rename(dest)
        moved[name] = where
    return moved


def refresh_transcripts(home: Path, log: Callable[[str], None] = print) -> list[str]:
    """Copy each staged session's transcript again from its source, with the
    same cwd rewrite, so a session that kept going after ``stage`` arrives
    with its whole conversation. Refuses, writing nothing, if a transcript
    now holds credential-looking values and staging did not allow that."""
    mf = home / ".handoff" / "manifest.json"
    manifest = json.loads(mf.read_text())
    pm = PathMap()
    for local, remote in manifest["path_map"].items():
        pm.add(local, remote)
    todo = []
    for entry in manifest["sessions"]:
        src = Path(entry.get("source_path") or "")
        if not src.is_file() or src.suffix == ".db" or not entry.get("files"):
            continue  # gone, a database, or staged before this was recorded
        extra = [Path(e) for e in entry.get("extra", [])]
        sidecar = src.with_suffix("")  # a Claude sidecar may have appeared since staging
        if entry["harness"] == "claude" and sidecar.is_dir() and sidecar not in extra:
            extra.append(sidecar)
        s = hz.Session(entry["harness"], entry["id"], src, None, extra=extra)
        todo.append((entry, s))
    if not manifest.get("allow_flagged_transcripts"):
        found = {s.key: transcript_secrets(s) for _, s in todo}
        found = {k: v for k, v in found.items() if v}
        if found:
            raise SystemExit("Since staging, these transcripts gained values that look like credentials:\n"
                             + "\n".join(f"  {k}: " + ", ".join(f"{n} {kind}" for kind, n in v.items())
                                         for k, v in found.items())
                             + "\nNothing was refreshed. Rotate them, or re-stage with --allow-transcript-secrets.")
    done = []
    for entry, s in todo:
        first = home / entry["files"][0]
        if entry["resumable"] and s.harness == "claude":
            tmp = first.with_name(first.name + ".refresh")
            rewrite_jsonl(s.path, tmp, pm)
            os.replace(tmp, first)
            for sidecar in s.extra:
                for f in sidecar.rglob("*"):
                    if f.is_file():
                        out = first.parent / sidecar.name / f.relative_to(sidecar)
                        if f.suffix == ".jsonl":
                            rewrite_jsonl(f, out, pm)
                        else:
                            out.parent.mkdir(parents=True, exist_ok=True)
                            shutil.copy2(f, out)
        elif entry["resumable"] and s.harness == "codex":
            tmp = first.with_name(first.name + ".refresh")
            rewrite_jsonl(s.path, tmp, pm)
            os.replace(tmp, first)
        elif first.name.startswith("transcript"):
            tmp = first.with_name(first.name + ".refresh")
            with tmp.open("w") as fh:
                fh.writelines(hz.iter_text_lines(s.path))
            os.replace(tmp, first)
            for extra in s.extra:  # sub-agent transcripts, as stage copied them
                target = first.parent / "extra" / extra.name
                if extra.is_dir():
                    shutil.copytree(extra, target, symlinks=True, dirs_exist_ok=True)
                elif extra.is_file():
                    target.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copy2(extra, target)
        else:
            continue
        done.append(entry["name"])
        log(f"refreshed {entry['source']} -> ~/{entry['files'][0]}")
    manifest["refreshed_at"] = time.time()
    mf.write_text(json.dumps(manifest, indent=1) + "\n", errors="backslashreplace")
    return done



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
    lines += ["", "## Tools", "", "Run `bash ~/.handoff/setup.sh` once. Review it first. It also puts back what",
              "could not travel under its own name (`~/.handoff/xdg-config` is `~/.config`)."]
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
    secret-looking files, symlinks that point outside it, or what is rebuilt
    rather than moved (virtualenvs, node_modules, build output, OS junk)."""
    n = 0
    root = src.resolve()
    for dirpath, dirnames, filenames in os.walk(src):
        d = Path(dirpath)
        keep = []
        for name in dirnames:
            if (d / name).is_symlink():
                filenames.append(name)  # recreated as a link below, never followed
            elif not inv.is_rebuilt_or_junk(str((d / name).relative_to(src))):
                keep.append(name)
        dirnames[:] = keep
        for name in filenames:
            f = d / name
            rel = f.relative_to(src)
            if inv.is_rebuilt_or_junk(str(rel)):
                continue
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
