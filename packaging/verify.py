#!/usr/bin/env python3
"""Build the distribution and prove it installs before anyone publishes it.

Run locally exactly as CI runs it:

    python packaging/verify.py

Checks:
  * every manifest agrees on one version;
  * every skill has frontmatter with a name matching its directory;
  * the wheel carries the skills (and their scripts) as package data;
  * the CLI installs the full skill set into every harness target, under a
    throwaway HOME, and removes it again; --dry-run writes nothing.

Requires `uv`, or `python -m build` plus `venv`.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import zipfile
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "packaging"))

import build as builder  # noqa: E402

FAILURES: list[str] = []
FRONTMATTER_RE = re.compile(r"^---\s*\n(.*?)\n---\s*\n", re.DOTALL)

# Harness target -> user-level skills directory, relative to HOME. Kept apart
# from the CLI on purpose: the point is to catch the CLI drifting from this.
EXPECTED_TARGETS = {
    "agents": ".agents/skills",
    "claude": ".claude/skills",
    "hermes": ".hermes/skills",
    "opencode": ".config/opencode/skills",
    "pi": ".pi/agent/skills",
}


def check(condition: bool, message: str) -> None:
    print(f"  {'ok  ' if condition else 'FAIL'} {message}")
    if not condition:
        FAILURES.append(message)


def run(cmd: list[str], check: bool = True, **kw) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, check=check, text=True, capture_output=True, **kw)


def check_versions() -> None:
    print("versions")
    versions = {}
    for rel in (
        "plugins/union/.claude-plugin/plugin.json",
        "plugins/union/.codex-plugin/plugin.json",
        "package.json",
    ):
        versions[rel] = json.loads((REPO / rel).read_text())["version"]
    check(len(set(versions.values())) == 1, f"one version across manifests: {versions}")


def check_skills() -> list[str]:
    print("skills")
    names = builder.skill_names()
    check(bool(names), f"skills found: {names}")
    for name in names:
        text = (builder.PLUGIN_SRC / "skills" / name / "SKILL.md").read_text()
        m = FRONTMATTER_RE.match(text)
        fm_name = None
        if m:
            for line in m.group(1).splitlines():
                if line.startswith("name:"):
                    fm_name = line.split(":", 1)[1].strip()
        check(fm_name == name, f"{name}: frontmatter name matches directory ({fm_name!r})")
    return names


def build_wheel(pkg: Path, out: Path) -> Path:
    if shutil.which("uv"):
        run(["uv", "build", "--wheel", "--out-dir", str(out), str(pkg)])
    else:
        run([sys.executable, "-m", "build", "--wheel", "--outdir", str(out), str(pkg)])
    wheels = sorted(out.glob("*.whl"))
    if not wheels:
        raise SystemExit("no wheel was built")
    return wheels[0]


def make_venv(path: Path, wheel: Path) -> Path:
    if shutil.which("uv"):
        run(["uv", "venv", "--quiet", str(path)], env={**os.environ, "VIRTUAL_ENV": ""})
        run(["uv", "pip", "install", "--quiet", "--python", str(path / "bin" / "python"), str(wheel)])
    else:
        run([sys.executable, "-m", "venv", str(path)])
        run([str(path / "bin" / "python"), "-m", "pip", "install", "--quiet", str(wheel)])
    return path / "bin" / builder.DIST


def check_wheel(wheel: Path, names: list[str]) -> None:
    print("wheel")
    with zipfile.ZipFile(wheel) as zf:
        members = set(zf.namelist())
    mod = builder.MODULE
    for name in names:
        check(f"{mod}/plugin/skills/{name}/SKILL.md" in members, f"wheel carries {name}/SKILL.md")
        for f in sorted((builder.PLUGIN_SRC / "skills" / name).rglob("*")):
            rel = f.relative_to(builder.PLUGIN_SRC).as_posix()
            if f.is_file() and "__pycache__" not in rel and "/tests/" not in rel:
                if f"{mod}/plugin/{rel}" not in members:
                    check(False, f"wheel carries {rel}")
    check(f"{mod}/plugin/.claude-plugin/plugin.json" in members, "wheel carries the plugin manifest")


def check_cli(exe: Path, names: list[str]) -> None:
    print("cli")
    listed = run([str(exe), "list"]).stdout.split()
    check(listed == names, f"`list` prints the bundled skills: {listed}")
    ver = run([str(exe), "version"]).stdout.strip()
    check(ver == builder.version(), f"`version` prints {ver}")
    plugin = Path(run([str(exe), "emit-plugin"]).stdout.strip())
    check((plugin / "skills").is_dir(), "`emit-plugin` prints the bundled plugin directory")

    with tempfile.TemporaryDirectory() as tmp:
        home = Path(tmp) / "home"
        home.mkdir()
        env = {**os.environ, "HOME": str(home)}

        dry = run([str(exe), "install", "--dry-run", "--target", "claude"], env=env)
        check(not (home / ".claude").exists(), "--dry-run writes nothing")
        check("Dry run" in dry.stdout, "--dry-run says so")

        for target, rel in EXPECTED_TARGETS.items():
            run([str(exe), "install", "--target", target], env=env)
            dest = home / rel
            got = sorted(p.name for p in dest.iterdir()) if dest.is_dir() else []
            check(got == names, f"install --target {target} -> ~/{rel}: {got}")

        again = run([str(exe), "install", "--target", "claude"], env=env)
        check("skip" in again.stdout, "a second install skips existing skills")
        forced = run([str(exe), "install", "--target", "claude", "--force"], env=env)
        check("skip" not in forced.stdout, "--force overwrites")

        alias = run([str(exe), "install", "--target", "codex", "--dry-run"], env=env)
        check(str(home / ".agents/skills") in alias.stdout, "--target codex is the agents directory")

        project = Path(tmp) / "proj"
        project.mkdir()
        run([str(exe), "install", "--project", "--target", "claude"], env=env, cwd=project)
        check((project / ".claude/skills" / names[0]).is_dir(), "--project installs under the current directory")

        for target, rel in EXPECTED_TARGETS.items():
            run([str(exe), "uninstall", "--target", target], env=env)
            left = [n for n in names if (home / rel / n).exists()]
            check(not left, f"uninstall --target {target} removes them: left {left}")

        bad = run([str(exe), "install", "--target", "nope"], env=env, check=False)
        check(bad.returncode != 0, "an unknown target is rejected")


def main() -> int:
    check_versions()
    names = check_skills()
    with tempfile.TemporaryDirectory() as tmp:
        tmp = Path(tmp)
        pkg = builder.build_pypi(builder.version(), tmp / "build")
        wheel = build_wheel(pkg, tmp / "dist")
        check_wheel(wheel, names)
        exe = make_venv(tmp / "venv", wheel)
        check_cli(exe, names)
    if FAILURES:
        print(f"\n{len(FAILURES)} check(s) failed.")
        return 1
    print("\nAll checks passed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
