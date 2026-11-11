"""Copy a staged home into a Union Volume and seal it, with the flyte CLI.

Two shapes, both from flyteplugins-union's volume commands:

  new agent     flyte create volume <name> --mount <dir> [--artifact <name>] ...
  merge         flyte mount volume <dir> --artifact <existing home>

Either way the command mounts the volume and holds; this module copies the
staged files in, then sends SIGINT (the documented Ctrl-C), which drains,
unmounts and publishes a sealed version.

Whether a mount publishes or discards is fixed when it starts, and every way
of stopping it (Ctrl-C, kill, `flyte unmount volume`) publishes. So if the copy
fails part way, this module does NOT stop the mount: it leaves it held, says
so, and lets the user decide between finishing the copy and publishing, or
discarding (see ``FAILED_COPY``).

Merging never overwrites a file already in the target home: a clash is
skipped and listed, except ``.agent-sessions/finished.json`` (rows are merged
by key) and the CLAUDE.md / AGENTS.md handoff section (appended).
"""

from __future__ import annotations

import json
import os
import shlex
import shutil
import signal
import subprocess
import sys
import tempfile
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

import stage as stg

MERGED_FILES = {".agent-sessions/finished.json"}
APPENDED_FILES = {"CLAUDE.md", "AGENTS.md"}


@dataclass
class SealOptions:
    staged_home: Path
    volume: Optional[str] = None  # new volume name
    artifact: Optional[str] = None  # register the new volume under this artifact name
    merge_artifact: Optional[str] = None  # existing agent home to fork and merge into
    bucket: Optional[str] = None
    project: Optional[str] = None
    domain: Optional[str] = None
    flyte: str = "flyte"
    mountpoint: Optional[Path] = None
    fork_name: Optional[str] = None
    dry_run: bool = False
    mount_timeout: float = 300.0
    publish_timeout: float = 3600.0
    extra_args: list[str] = field(default_factory=list)


def command(opts: SealOptions, mnt: Path) -> list[str]:
    if opts.merge_artifact:
        cmd = [opts.flyte, "mount", "volume", str(mnt), "--artifact", opts.merge_artifact,
               "--fork-name", local_name(opts)]
    else:
        if not opts.volume:
            raise SystemExit("a new volume needs --volume <name>")
        cmd = [opts.flyte, "create", "volume", opts.volume, "--mount", str(mnt)]
        if opts.artifact:
            cmd += ["--artifact", opts.artifact]
        if opts.bucket:
            cmd += ["--bucket", opts.bucket]
    if opts.project:
        cmd += ["--project", opts.project]
    if opts.domain:
        cmd += ["--domain", opts.domain]
    return cmd + opts.extra_args


def local_name(opts: SealOptions) -> str:
    """The name the local mount is known by, for `flyte unmount/forget volume`."""
    if opts.merge_artifact:
        return opts.fork_name or f"{opts.merge_artifact}-handoff-{time.strftime('%Y%m%d%H%M%S')}"
    return opts.volume or ""


FAILED_COPY = """The copy into the volume did not finish: {error}

The mount is still up and nothing has been published yet:
  pid {pid}, mounted at {mnt}, local name {name}

Ctrl-C, `kill` and `flyte unmount volume` all PUBLISH whatever is in the
mount, so do not stop it that way until it is complete. Either finish it:
copy the rest of {home} into {mnt}, then
  kill -INT {pid}            # publishes, like Ctrl-C
or throw it away. SIGKILL is the one stop that does not publish:
  kill -KILL {pid}
  flyte forget volume {name} --discard
(forget refuses while the mount's client process still holds the volume, and
names that process; stop that one too, then run forget again.)
"""


class Collision(Exception):
    pass


def _units(home: Path) -> list[str]:
    """The pieces of a staged home that must land whole or not at all: each
    repo, each written handoff, each transcript and its sidecar, each Codex
    session home. A merge that met any of them half-present would build a
    hybrid (say, one repo's objects under another's HEAD)."""
    units = []
    for parent in ("src", "handoff", ".agent-sessions/codex/sessions"):
        d = home / parent
        if d.is_dir():
            units += [f"{parent}/{c.name}" for c in sorted(d.iterdir())]
    projects = home / ".claude" / "projects"
    if projects.is_dir():
        for proj in sorted(projects.iterdir()):
            for c in sorted(proj.iterdir()):
                if c.name != "memory":
                    units.append(f".claude/projects/{proj.name}/{c.name}")
    return units


def _no_symlinks_below(root: Path, rel: str) -> None:
    """Refuse a destination path that goes through a symlink: following one
    would write outside the volume."""
    cur = root
    for part in Path(rel).parts[:-1]:
        cur = cur / part
        if cur.is_symlink():
            raise Collision(f"{cur} is a symlink in the target home; refusing to write through it")


def collisions(home: Path, dest: Path) -> list[str]:
    hits = [u for u in _units(home) if os.path.lexists(dest / u)]
    for u in _units(home):
        try:
            _no_symlinks_below(dest, u + "/x")
        except Collision as e:
            hits.append(str(e))
    return hits


def copy_into(src: Path, dest: Path, merge: bool) -> dict:
    """Copy the staged home into the mounted volume. When merging, nothing in
    the target is overwritten and no write goes through a symlink."""
    copied, skipped, merged = 0, [], []
    for f in sorted(src.rglob("*")):
        rel = f.relative_to(src).as_posix()
        out = dest / rel
        _no_symlinks_below(dest, rel)
        if f.is_dir() and not f.is_symlink():
            if out.is_symlink():
                raise Collision(f"{out} is a symlink in the target home; refusing to write through it")
            out.mkdir(parents=True, exist_ok=True)
            continue
        if merge and os.path.lexists(out):
            if out.is_symlink():
                skipped.append(rel)
            elif rel in MERGED_FILES:
                existing = json.loads(out.read_text() or "{}")
                ours = json.loads(f.read_text())
                result, dup = stg.merge_finished(existing, ours.get("sessions", []))
                out.write_text(json.dumps(result, indent=1) + "\n")
                merged.append(rel)
                skipped += [f"{rel}#{k}" for k in dup]
            elif rel in APPENDED_FILES:
                stg._append_section(out, stg.HANDOFF_POINTER)
                merged.append(rel)
            else:
                skipped.append(rel)
            continue
        if f.is_symlink():
            os.symlink(os.readlink(f), out)
        else:
            shutil.copy2(f, out)
        copied += 1
    return {"copied": copied, "merged": merged, "skipped_existing": skipped}


def _wait_mounted(proc: subprocess.Popen, mnt: Path, timeout: float) -> None:
    deadline = time.time() + timeout
    while time.time() < deadline:
        if proc.poll() is not None:
            raise RuntimeError(f"`flyte` exited with {proc.returncode} before the volume mounted")
        if os.path.ismount(mnt):
            return
        time.sleep(1)
    raise RuntimeError(f"the volume did not mount at {mnt} within {timeout:.0f}s")


def seal(opts: SealOptions) -> dict:
    home = opts.staged_home
    if not (home / ".handoff" / "manifest.json").is_file():
        raise SystemExit(f"{home} is not a staged handoff home (no .handoff/manifest.json)")
    mnt = opts.mountpoint or Path(tempfile.mkdtemp(prefix="union-handoff-mnt-"))
    if opts.merge_artifact and not opts.fork_name:
        opts.fork_name = local_name(opts)
    cmd = command(opts, mnt)
    if opts.dry_run:
        return {"dry_run": True, "command": shlex.join(cmd), "mountpoint": str(mnt), "local_name": local_name(opts)}
    if shutil.which(opts.flyte) is None:
        raise SystemExit(f"`{opts.flyte}` is not on PATH; install flyteplugins-union (pip install flyteplugins-union)")

    mnt.mkdir(parents=True, exist_ok=True)
    print(f"$ {shlex.join(cmd)}", file=sys.stderr)
    log = tempfile.NamedTemporaryFile("w+", prefix="union-handoff-flyte-", suffix=".log", delete=False)
    # Its own session: a Ctrl-C in this terminal must not reach the mount,
    # because a mount that receives one publishes whatever it holds.
    proc = subprocess.Popen(cmd, stdout=log, stderr=subprocess.STDOUT, text=True, start_new_session=True)
    result: dict = {"command": shlex.join(cmd), "mountpoint": str(mnt), "log": log.name}
    try:
        _wait_mounted(proc, mnt, opts.mount_timeout)
    except RuntimeError as e:
        log.seek(0)
        tail = "\n".join(log.read().splitlines()[-25:])
        raise SystemExit(f"{e}. The end of its output ({log.name}):\n{tail}")
    if opts.merge_artifact:
        hits = collisions(home, mnt)
        if hits:
            # Nothing was written, so stopping now publishes an unchanged fork.
            proc.send_signal(signal.SIGINT)
            proc.wait(timeout=opts.publish_timeout)
            raise SystemExit(
                "The target home already has parts of this handoff, so merging would mix two copies:\n  "
                + "\n  ".join(hits)
                + "\nNothing was copied. Re-stage with --exclude for those repos, or hand off into a new agent."
            )
    try:
        result.update(copy_into(home, mnt, merge=bool(opts.merge_artifact)))
    except (Exception, KeyboardInterrupt) as e:
        raise SystemExit(FAILED_COPY.format(error=str(e) or type(e).__name__, pid=proc.pid, mnt=mnt,
                                            name=local_name(opts), home=home))
    proc.send_signal(signal.SIGINT)
    try:
        proc.wait(timeout=opts.publish_timeout)
    except subprocess.TimeoutExpired:
        raise RuntimeError(f"publishing did not finish within {opts.publish_timeout:.0f}s; see {log.name}")
    log.seek(0)
    output = log.read()
    result["returncode"] = proc.returncode
    result["output_tail"] = output[-4000:]
    locators = [w for w in output.split() if w.endswith("metadata.json") and "://" in w]
    result["locator"] = locators[-1] if locators else None
    if proc.returncode != 0:
        raise RuntimeError(f"`flyte` exited with {proc.returncode}; see {log.name}\n{output[-2000:]}")
    return result
