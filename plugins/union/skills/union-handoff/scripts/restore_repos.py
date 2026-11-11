#!/usr/bin/env python3
"""Recreate the handed-off repos in this home, on first boot.

Copied into ~/.handoff/ by the union-handoff skill and run by
~/.handoff/setup.sh. For each ~/.handoff/repos/<name>/repo.json, in order:

  1. clone the repo from its remote (or, for a worktree, add a worktree to
     the clone it belongs to);
  2. fetch the commits the remote did not have, from unpushed.bundle;
  3. check out the branch (or detached commit) the session was on;
  4. reapply the staged and unstaged changes and copy the untracked files.

A repo already restored (its directory is non-empty) is skipped, so running
this twice is harmless. A clone that fails (usually: the agent is not signed
in to the git host yet) is reported and the rest carry on; sign in and run it
again. Standard library only.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

HOME = Path(os.path.expanduser("~"))
REPOS = HOME / ".handoff" / "repos"


def git(*args: str, cwd: Path | None = None, input: bytes | None = None) -> None:
    proc = subprocess.run(["git", *args], cwd=cwd, input=input, capture_output=True)
    if proc.returncode != 0:
        raise RuntimeError(f"git {' '.join(args)}: {proc.stderr.decode(errors='replace').strip()}")


def restore(spec: dict, meta: Path) -> str:
    dest = HOME / spec["path"]
    if dest.exists() and any(dest.iterdir()):
        return "already there"
    dest.parent.mkdir(parents=True, exist_ok=True)
    if spec.get("worktree_of"):
        primary = HOME / spec["worktree_of"]
        if not (primary / ".git").exists():
            raise RuntimeError(f"its main checkout {primary} was not restored")
        git("worktree", "add", "--detach", str(dest), cwd=primary)
    else:
        git("clone", "--quiet", spec["url"], str(dest))
    bundle = meta / "unpushed.bundle"
    if bundle.is_file():
        git("fetch", "--quiet", str(bundle), "HEAD", cwd=dest)  # into FETCH_HEAD only
    if spec.get("branch"):
        git("checkout", "--quiet", "-B", spec["branch"], spec["head"], cwd=dest)
        if spec.get("upstream"):
            try:
                git("branch", f"--set-upstream-to={spec['upstream']}", cwd=dest)
            except RuntimeError:
                pass  # the upstream branch may not exist on the remote yet
    else:
        git("checkout", "--quiet", "--detach", spec["head"], cwd=dest)
    for name, args in (("staged.patch", ["--index"]), ("unstaged.patch", [])):
        patch = meta / name
        if patch.is_file() and patch.stat().st_size:
            git("apply", "--binary", "--whitespace=nowarn", *args, input=patch.read_bytes(), cwd=dest)
    untracked = meta / "untracked"
    if untracked.is_dir():
        for f in untracked.rglob("*"):
            target = dest / f.relative_to(untracked)
            if f.is_symlink():
                target.parent.mkdir(parents=True, exist_ok=True)
                if not os.path.lexists(target):
                    os.symlink(os.readlink(f), target)
            elif f.is_file():
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(f, target)
    return "restored"


def main() -> int:
    if not REPOS.is_dir():
        return 0
    specs = []
    for meta in sorted(REPOS.iterdir()):
        f = meta / "repo.json"
        if f.is_file():
            specs.append((json.loads(f.read_text()), meta))
    # Main checkouts before the worktrees that hang off them.
    specs.sort(key=lambda sm: (bool(sm[0].get("worktree_of")), sm[0]["path"]))
    failed = []
    for spec, meta in specs:
        try:
            print(f"{spec['path']}: {restore(spec, meta)}")
        except (RuntimeError, OSError) as e:
            failed.append(spec["path"])
            print(f"{spec['path']}: FAILED: {e}", file=sys.stderr)
    if failed:
        print(
            "Could not restore: " + ", ".join(failed) + ". A failed clone usually means this agent is not signed\n"
            "in to the git host yet (for GitHub: gh auth login). Fix that and run this again.",
            file=sys.stderr,
        )
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
