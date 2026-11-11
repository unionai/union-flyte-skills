#!/usr/bin/env python3
"""Generate the PyPI source tree for the ``union-flyte-skills`` distribution.

One source of truth, ``plugins/union/``, is vendored into an installable
package whose console script copies the skills into an agent harness.

    python packaging/build.py                 # build into ./build/pypi/union-flyte-skills
    python packaging/build.py --print-version # version from the plugin manifest

The version comes from ``plugins/union/.claude-plugin/plugin.json``; use
``packaging/set_version.py`` to change it.
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
PLUGIN_SRC = REPO / "plugins" / "union"
TEMPLATES = REPO / "packaging" / "templates"

REPO_URL = (
    f"{os.environ['GITHUB_SERVER_URL']}/{os.environ['GITHUB_REPOSITORY']}"
    if os.environ.get("GITHUB_SERVER_URL") and os.environ.get("GITHUB_REPOSITORY")
    else "https://github.com/unionai/union-flyte-skills"
)

DIST = "union-flyte-skills"
MODULE = DIST.replace("-", "_")

KEYWORDS = [
    "union",
    "flyte",
    "agent-skills",
    "claude-code",
    "claude-code-plugin",
    "codex",
]


def manifest() -> dict:
    return json.loads((PLUGIN_SRC / ".claude-plugin" / "plugin.json").read_text())


def version() -> str:
    return manifest()["version"]


def description() -> str:
    return manifest()["description"]


def skill_names() -> list[str]:
    return sorted(
        p.name for p in (PLUGIN_SRC / "skills").iterdir() if (p / "SKILL.md").is_file()
    )


def render_readme(ver: str) -> str:
    tmpl = (TEMPLATES / "README.md.tmpl").read_text()
    skills = "\n".join(f"- `{name}`" for name in skill_names())
    return (
        tmpl.replace("{{DIST}}", DIST)
        .replace("{{VERSION}}", ver)
        .replace("{{REPO_URL}}", REPO_URL)
        .replace("{{DESCRIPTION}}", description())
        .replace("{{SKILLS}}", skills)
    )


def copy_plugin(dest: Path) -> None:
    """Copy the plugin payload (manifests + skills) into dest."""
    dest.mkdir(parents=True, exist_ok=True)
    for name in (".claude-plugin", ".codex-plugin", "skills"):
        shutil.copytree(
            PLUGIN_SRC / name,
            dest / name,
            ignore=shutil.ignore_patterns("__pycache__", "*.pyc", "tests"),
        )


def build_pypi(ver: str, outdir: Path) -> Path:
    pkg = outdir / "pypi" / DIST
    if pkg.exists():
        shutil.rmtree(pkg)
    src = pkg / "src" / MODULE
    src.mkdir(parents=True)

    copy_plugin(src / "plugin")
    shutil.copy2(TEMPLATES / "cli.py", src / "cli.py")
    (src / "__init__.py").write_text(
        '"""Union agent skills, installable into any agent harness."""\n\n'
        f'__version__ = "{ver}"\n\n'
        "from .cli import TARGETS, main, plugin_root\n\n"
        '__all__ = ["TARGETS", "main", "plugin_root", "__version__"]\n'
    )

    shutil.copy2(REPO / "LICENSE", pkg / "LICENSE")
    (pkg / "README.md").write_text(render_readme(ver))

    pyproject = f"""\
[build-system]
requires = ["hatchling>=1.24"]
build-backend = "hatchling.build"

[project]
name = "{DIST}"
version = "{ver}"
description = "{description()}"
readme = "README.md"
requires-python = ">=3.9"
license = "Apache-2.0"
license-files = ["LICENSE"]
authors = [{{ name = "unionai" }}]
keywords = {json.dumps(KEYWORDS)}
classifiers = [
    "Development Status :: 3 - Alpha",
    "Intended Audience :: Developers",
    "Programming Language :: Python :: 3",
]
dependencies = []

[project.urls]
Homepage = "{REPO_URL}"
Repository = "{REPO_URL}"
Issues = "{REPO_URL}/issues"

[project.scripts]
{DIST} = "{MODULE}.cli:main"

# The payload lives under src/{MODULE}/plugin/, including dot-directories.
# ignore-vcs keeps a build/ directory listed in .gitignore from being pruned.
[tool.hatch.build]
ignore-vcs = true

[tool.hatch.build.targets.wheel]
packages = ["src/{MODULE}"]
artifacts = ["src/{MODULE}/plugin/**"]

[tool.hatch.build.targets.sdist]
include = ["src", "README.md", "LICENSE", "pyproject.toml"]
artifacts = ["src/{MODULE}/plugin/**"]
"""
    (pkg / "pyproject.toml").write_text(pyproject)
    return pkg


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--outdir", default=str(REPO / "build"))
    ap.add_argument(
        "--print-version",
        action="store_true",
        help="Print the version from the plugin manifest and exit.",
    )
    args = ap.parse_args()

    ver = version()
    if args.print_version:
        print(ver)
        return 0

    skills = skill_names()
    if not skills:
        raise SystemExit(f"No skills found under {PLUGIN_SRC / 'skills'}")

    outdir = Path(args.outdir).resolve()
    print(f"version {ver}, {len(skills)} skills, out={outdir}")
    print(f"  pypi {build_pypi(ver, outdir)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
