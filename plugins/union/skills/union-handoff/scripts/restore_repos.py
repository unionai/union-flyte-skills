#!/usr/bin/env python3
"""Recreate the handed-off repos in this home, on first boot.

Copied into ~/.handoff/ by the union-handoff skill and run by
~/.handoff/setup.sh. For each ~/.handoff/repos/<name>/repo.json, main
checkouts first:

  1. clone the repo from its remote, keeping the remote's name (or, for a
     worktree, add a worktree to the clone it belongs to, fetching its own
     remote first if that differs);
  2. fetch every branch and tag the remote did not have, from unpushed.bundle;
  3. recreate the local branches with their upstreams, and check out the
     branch (or detached commit) the session was on;
  4. reapply the staged and unstaged changes and copy the untracked files;
  5. initialise submodules, best effort.

All of it happens in a temporary directory beside the destination, which is
moved into place only when every step succeeded, and a repo is marked done
only then. So a failure leaves nothing half-restored, and running this again
retries exactly the repos that are not done. A destination that already
holds something else is refused, not overwritten. Standard library only.
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


def git(*args: str, cwd: Path | None = None, input: bytes | None = None) -> str:
    proc = subprocess.run(["git", *args], cwd=cwd, input=input, capture_output=True)
    if proc.returncode != 0:
        raise RuntimeError(f"git {' '.join(args)}: {proc.stderr.decode(errors='replace').strip()}")
    return proc.stdout.decode(errors="replace")


def _ensure_remote(repo: Path, name: str, url: str) -> str:
    """The name of a remote in ``repo`` pointing at ``url``, adding one if needed."""
    existing = {}
    for line in git("remote", "-v", cwd=repo).splitlines():
        parts = line.split()
        if len(parts) >= 2:
            existing[parts[0]] = parts[1]
    for rname, rurl in existing.items():
        if rurl == url:
            return rname
    if name in existing:
        name = f"{name}-handoff"
    git("remote", "add", name, url, cwd=repo)
    return name


def restore(spec: dict, meta: Path) -> str:
    dest = HOME / spec["path"]
    if (meta / "restored").exists():
        return "already restored"
    if dest.exists() and (not dest.is_dir() or any(dest.iterdir())):
        raise RuntimeError(f"{dest} already holds something; move it aside and run this again")
    tmp = dest.parent / f".{dest.name}.handoff-restoring"
    primary = HOME / spec["worktree_of"] if spec.get("worktree_of") else None
    _discard(tmp, primary)
    dest.parent.mkdir(parents=True, exist_ok=True)
    try:
        remote = spec.get("remote") or "origin"
        if primary:
            if not (primary / ".git").exists():
                raise RuntimeError(f"its main checkout {primary} was not restored")
            remote = _ensure_remote(primary, remote, spec["url"])
            git("fetch", "--quiet", remote, cwd=primary)
            git("worktree", "add", "--detach", str(tmp), cwd=primary)
        else:
            git("clone", "--quiet", "--origin", remote, spec["url"], str(tmp))
        # Detach first, so that no branch being recreated is the checked-out one.
        git("checkout", "--quiet", "--detach", cwd=tmp)
        bundle = meta / "unpushed.bundle"
        if bundle.is_file():
            # Fetch only what the bundle lists: git leaves out a ref (HEAD
            # included) whose commit the remote already has.
            listed = {line.split()[1] for line in git("bundle", "list-heads", str(bundle), cwd=tmp).splitlines()
                      if len(line.split()) == 2}
            specs = ([] if not any(r.startswith("refs/heads/") for r in listed) else ["+refs/heads/*:refs/handoff/heads/*"])
            specs += ["+refs/tags/*:refs/tags/*"] if any(r.startswith("refs/tags/") for r in listed) else []
            specs += ["HEAD"] if "HEAD" in listed else []
            if specs:
                git("fetch", "--quiet", str(bundle), *specs, cwd=tmp)
        for name, b in sorted((spec.get("branches") or {}).items()):
            if name == spec.get("branch"):
                continue
            try:
                git("branch", "--force", name, b["head"], cwd=tmp)
            except RuntimeError:
                continue  # checked out in another worktree of this repo
            if b.get("upstream"):
                subprocess.run(["git", "branch", f"--set-upstream-to={b['upstream']}", name], cwd=tmp, capture_output=True)
        if spec.get("branch"):
            git("checkout", "--quiet", "-B", spec["branch"], spec["head"], cwd=tmp)
            if spec.get("upstream"):
                # The upstream branch may not exist on the remote yet; that is fine.
                subprocess.run(["git", "branch", f"--set-upstream-to={spec['upstream']}"], cwd=tmp, capture_output=True)
        else:
            git("checkout", "--quiet", "--detach", spec["head"], cwd=tmp)
        refs = git("for-each-ref", "--format=%(refname)", "refs/handoff", cwd=tmp).split()
        for ref in refs:
            git("update-ref", "-d", ref, cwd=tmp)
        for name, args in (("staged.patch", ["--index"]), ("unstaged.patch", [])):
            patch = meta / name
            if patch.is_file() and patch.stat().st_size:
                git("apply", "--binary", "--whitespace=nowarn", *args, input=patch.read_bytes(), cwd=tmp)
        untracked = meta / "untracked"
        if untracked.is_dir():
            for f in untracked.rglob("*"):
                target = tmp / f.relative_to(untracked)
                if f.is_symlink():
                    target.parent.mkdir(parents=True, exist_ok=True)
                    if not os.path.lexists(target):
                        os.symlink(os.readlink(f), target)
                elif f.is_file():
                    target.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copy2(f, target)
        note = ""
        if (tmp / ".gitmodules").is_file():
            sub = subprocess.run(["git", "submodule", "update", "--init", "--recursive"], cwd=tmp, capture_output=True)
            note = "" if sub.returncode == 0 else " (submodules not initialised; run git submodule update --init)"
        if dest.exists():
            dest.rmdir()  # empty, checked above
        if primary:
            git("worktree", "move", str(tmp), str(dest), cwd=primary)
        else:
            tmp.rename(dest)
    except BaseException:
        _discard(tmp, primary)
        raise
    (meta / "restored").write_text("done\n")
    return "restored" + note


def _discard(tmp: Path, primary: Path | None) -> None:
    if not os.path.lexists(tmp):
        return
    if primary and (primary / ".git").exists():
        subprocess.run(["git", "worktree", "remove", "--force", str(tmp)], cwd=primary, capture_output=True)
        subprocess.run(["git", "worktree", "prune"], cwd=primary, capture_output=True)
    if os.path.lexists(tmp):
        shutil.rmtree(tmp, ignore_errors=True)


def main() -> int:
    if not REPOS.is_dir():
        return 0
    specs = []
    for meta in sorted(REPOS.iterdir()):
        f = meta / "repo.json"
        if f.is_file():
            specs.append((json.loads(f.read_text()), meta))
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
            "in to the git host yet (for GitHub: gh auth login). Fix that and run this again; repos already\n"
            "restored are skipped.",
            file=sys.stderr,
        )
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
