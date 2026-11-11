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


FAILED_COPY = """The copy into the volume failed: {error}

The mount is still held, unpublished, so nothing half-copied was sealed:
  pid {pid}, mounted at {mnt}, local name {name}

Either finish it: fix the cause, copy the rest of {home} into {mnt}, then
  kill -INT {pid}            # publishes, like Ctrl-C
or throw it away:
  kill -KILL {pid}
  flyte forget volume {name} --discard
(forget refuses while the mount's client process still holds it, and names
that process; stop it and run forget again.)
"""


def copy_into(src: Path, dest: Path, merge: bool) -> dict:
    """Copy the staged home into the mounted volume."""
    copied, skipped, merged = 0, [], []
    for f in sorted(src.rglob("*")):
        rel = f.relative_to(src).as_posix()
        out = dest / rel
        if f.is_dir() and not f.is_symlink():
            out.mkdir(parents=True, exist_ok=True)
            continue
        out.parent.mkdir(parents=True, exist_ok=True)
        if merge and out.exists():
            if rel in MERGED_FILES:
                existing = json.loads(out.read_text() or "{}")
                ours = json.loads(f.read_text())
                out.write_text(json.dumps(stg.merge_finished(existing, ours.get("sessions", [])), indent=1) + "\n")
                merged.append(rel)
            elif rel in APPENDED_FILES:
                stg._append_section(out, stg.HANDOFF_POINTER)
                merged.append(rel)
            else:
                skipped.append(rel)
            continue
        if f.is_symlink():
            if out.is_symlink() or out.exists():
                out.unlink()
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
    proc = subprocess.Popen(cmd, stdout=log, stderr=subprocess.STDOUT, text=True)
    result: dict = {"command": shlex.join(cmd), "mountpoint": str(mnt), "log": log.name}
    try:
        _wait_mounted(proc, mnt, opts.mount_timeout)
    except RuntimeError as e:
        log.seek(0)
        tail = "\n".join(log.read().splitlines()[-25:])
        raise SystemExit(f"{e}. The end of its output ({log.name}):\n{tail}")
    try:
        result.update(copy_into(home, mnt, merge=bool(opts.merge_artifact)))
    except Exception as e:
        raise SystemExit(FAILED_COPY.format(error=e, pid=proc.pid, mnt=mnt, name=local_name(opts), home=home))
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
