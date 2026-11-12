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

Before mounting, the transcripts are copied again from their sources (see
stage.refresh_transcripts), so a session that kept going since ``stage``
arrives whole. Files are copied without extended attributes, so macOS writes
no AppleDouble ``._*`` files into the mount, and any that appear are deleted
before publishing.

The upload runs as the user: ``flyte`` with their config (``--config``), and
the object store with their own cloud credentials (``--aws-profile`` sets
AWS_PROFILE), which need write access to the project's bucket.
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
    config: Optional[str] = None  # flyte config file, passed as `flyte --config`
    aws_profile: Optional[str] = None  # AWS_PROFILE for the upload
    refresh: bool = True  # copy the transcripts again just before sealing
    extra_args: list[str] = field(default_factory=list)


def command(opts: SealOptions, mnt: Path) -> list[str]:
    base = [opts.flyte] + (["--config", opts.config] if opts.config else [])
    if opts.merge_artifact:
        cmd = base + ["mount", "volume", str(mnt), "--artifact", opts.merge_artifact,
                      "--fork-name", local_name(opts)]
    else:
        if not opts.volume:
            raise SystemExit("a new volume needs --volume <name>")
        cmd = base + ["create", "volume", opts.volume, "--mount", str(mnt)]
        if opts.artifact:
            cmd += ["--artifact", opts.artifact]
        if opts.bucket:
            cmd += ["--bucket", opts.bucket]
    if opts.project:
        cmd += ["--project", opts.project]
    if opts.domain:
        cmd += ["--domain", opts.domain]
    return cmd + opts.extra_args


def command_env(opts: SealOptions) -> dict:
    """The environment `flyte` runs in: the user's, plus their AWS profile,
    and COPYFILE_DISABLE so macOS tools write no ``._*`` files."""
    env = {**os.environ, "COPYFILE_DISABLE": "1"}
    if opts.aws_profile:
        env["AWS_PROFILE"] = opts.aws_profile
    return env


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
copy the rest of {home} into {mnt} (on macOS without extended attributes,
`COPYFILE_DISABLE=1 cp -R -X ...`, and then `find {mnt} -name '._*' -delete`), then
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
    # Repos that travel by reference land on the agent at first boot, but a
    # home that already has that path must still count as a collision.
    for spec in sorted((home / ".handoff" / "repos").glob("*/repo.json")):
        units.append(json.loads(spec.read_text())["path"])
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


def _copy_plain(f: Path, out: Path) -> None:
    """Copy a file's bytes, mode and times, and nothing else: copying
    extended attributes or flags into a FUSE mount makes macOS write an
    AppleDouble ``._name`` file beside each one."""
    shutil.copyfile(f, out)
    st = f.stat()
    os.chmod(out, st.st_mode & 0o7777)
    os.utime(out, ns=(st.st_atime_ns, st.st_mtime_ns))


def is_appledouble(name: str) -> bool:
    return name.startswith("._")


def sweep_appledouble(dest: Path, written: list[str]) -> list[str]:
    """Delete the ``._name`` files macOS left beside what was written."""
    gone = []
    for rel in written:
        p = dest / rel
        ad = p.parent / f"._{p.name}"
        if os.path.lexists(ad) and not os.path.isdir(ad):
            os.unlink(ad)
            gone.append(str(ad.relative_to(dest)))
    return gone


def copy_into(src: Path, dest: Path, merge: bool) -> dict:
    """Copy the staged home into the mounted volume. When merging, nothing in
    the target is overwritten and no write goes through a symlink."""
    copied, skipped, merged, written = 0, [], [], []
    for f in sorted(src.rglob("*")):
        rel = f.relative_to(src).as_posix()
        if is_appledouble(f.name):
            continue
        out = dest / rel
        _no_symlinks_below(dest, rel)
        if f.is_dir() and not f.is_symlink():
            if out.is_symlink():
                raise Collision(f"{out} is a symlink in the target home; refusing to write through it")
            out.mkdir(parents=True, exist_ok=True)
            written.append(rel)
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
            _copy_plain(f, out)
        written.append(rel)
        copied += 1
    return {"copied": copied, "merged": merged, "skipped_existing": skipped,
            "appledouble_deleted": sweep_appledouble(dest, written)}


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
    reserved = stg.reserved_in(home)
    if reserved:
        # Checked before anything mounts: writing one fails part way through.
        raise SystemExit(
            f"The staged home has {', '.join(reserved)} at its top level, and a Union Volume reserves those"
            " names (they are read-only virtual entries at its root). Re-stage it with this version of"
            " `handoff.py stage`, which moves them aside for setup.sh to put back, or move them yourself.")
    mnt = opts.mountpoint or Path(tempfile.mkdtemp(prefix="union-handoff-mnt-"))
    if opts.merge_artifact and not opts.fork_name:
        opts.fork_name = local_name(opts)
    cmd = command(opts, mnt)
    if opts.dry_run:
        prefix = f"AWS_PROFILE={shlex.quote(opts.aws_profile)} " if opts.aws_profile else ""
        return {"dry_run": True, "command": prefix + shlex.join(cmd), "mountpoint": str(mnt),
                "local_name": local_name(opts)}
    if shutil.which(opts.flyte) is None:
        raise SystemExit(f"`{opts.flyte}` is not on PATH; install flyteplugins-union (pip install flyteplugins-union)")
    if opts.refresh:
        stg.refresh_transcripts(home, log=lambda m: print(m, file=sys.stderr))

    mnt.mkdir(parents=True, exist_ok=True)
    print(f"$ {shlex.join(cmd)}", file=sys.stderr)
    log = tempfile.NamedTemporaryFile("w+", prefix="union-handoff-flyte-", suffix=".log", delete=False)
    # Its own session: a Ctrl-C in this terminal must not reach the mount,
    # because a mount that receives one publishes whatever it holds.
    proc = subprocess.Popen(cmd, stdout=log, stderr=subprocess.STDOUT, text=True, start_new_session=True,
                            env=command_env(opts))
    result: dict = {"command": shlex.join(cmd), "mountpoint": str(mnt), "log": log.name}
    try:
        _wait_mounted(proc, mnt, opts.mount_timeout)
    except RuntimeError as e:
        log.seek(0)
        tail = "\n".join(log.read().splitlines()[-25:])
        hint = ""
        if any(w in tail for w in ("AccessDenied", "Access Denied", "403", "Forbidden", "ExpiredToken", "NoCredentials")):
            hint = ("\nThe object store refused your cloud credentials. The upload uses your own, so they need"
                    " write access to the project's bucket: pick a profile that has it with --aws-profile"
                    " (and sign in to it, e.g. `aws sso login --profile <name>`).")
        raise SystemExit(f"{e}. The end of its output ({log.name}):\n{tail}{hint}")
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
