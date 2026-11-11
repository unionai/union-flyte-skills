"""Repo-wide public-content lint: every tracked file, not just skills.

  python -m evals.harness.public_lint

Fails on secret-like values and internal references (see static_lint), and on
em/en dashes in prose. The pattern definitions themselves are exempt.
"""

from __future__ import annotations

import subprocess
import sys

from .spec import REPO_ROOT
from .static_lint import DASH_RE, scan_text

EXEMPT = {"evals/harness/static_lint.py", "LICENSE"}


def tracked_files() -> list[str]:
    out = subprocess.run(
        ["git", "ls-files", "--cached", "--others", "--exclude-standard"],
        cwd=REPO_ROOT, text=True, capture_output=True, check=True,
    ).stdout
    return [f for f in out.splitlines() if f and f not in EXEMPT]


def lint() -> list[str]:
    problems = []
    for rel in tracked_files():
        path = REPO_ROOT / rel
        if not path.is_file():
            continue
        try:
            text = path.read_text()
        except UnicodeDecodeError:
            continue
        secrets, internal = scan_text(text)
        problems += [f"{rel}: {label}" for label in secrets + internal]
        if DASH_RE.search(text):
            problems.append(f"{rel}: em/en dash")
    return problems


def main() -> int:
    problems = lint()
    for p in problems:
        print(f"FAIL {p}")
    print(f"{len(problems)} problem(s)")
    return 1 if problems else 0


if __name__ == "__main__":
    raise SystemExit(main())
