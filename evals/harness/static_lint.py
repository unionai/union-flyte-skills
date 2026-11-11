"""Static tier: skill-agnostic lint of a SKILL.md.

No LLM and no agent. It validates the skill file itself:
  - YAML frontmatter present, with ``name`` + ``description``
  - ``name`` matches the containing directory
  - description length is within agent-skill norms
  - python fences parse; JSON fences parse
  - every ``scripts/...`` path the body mentions exists in the skill
  - no secrets, and nothing environment-specific (this repo is public)

Adapted from flyteorg/flyte-agent-plugins (Apache-2.0).
"""

from __future__ import annotations

import ast
import json
import pathlib
import re

import yaml

from .checks import CheckResult

FRONTMATTER_RE = re.compile(r"^---\s*\n(.*?)\n---\s*\n", re.DOTALL)
FENCE_RE = re.compile(r"^```([\w-]*)\n(.*?)^```", re.DOTALL | re.MULTILINE)
SCRIPT_REF_RE = re.compile(r"(?<![\w/.-])(scripts/[\w./-]+\.(?:py|sh))")

SECRET_PATTERNS = [
    (re.compile(r"AKIA[0-9A-Z]{16}"), "AWS access key id"),
    (re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----"), "private key"),
    (re.compile(r"eyJ[A-Za-z0-9_-]{20,}\.[A-Za-z0-9_-]{20,}\.[A-Za-z0-9_-]{10,}"), "JWT"),
    (re.compile(r"xox[baprs]-[0-9A-Za-z-]{10,}"), "Slack token"),
    (re.compile(r"gh[pousr]_[0-9A-Za-z]{36}"), "GitHub token"),
    (re.compile(r"sk-[A-Za-z0-9_-]{32,}"), "API key"),
]

# A public repo must not name a tenant, a workspace link or an internal tracker.
# Placeholders like <your-org> are what docs should use instead.
INTERNAL_PATTERNS = [
    (re.compile(r"[\w-]+\.hosted\.unionai\.cloud"), "Union tenant hostname"),
    (re.compile(r"[\w-]+\.slack\.com/"), "Slack workspace link"),
    (re.compile(r"notion\.so/"), "Notion link"),
    (re.compile(r"linear\.app/"), "Linear link"),
]

# Em and en dashes read as machine prose; the repo style is a plain hyphen.
DASH_RE = re.compile("[\u2013\u2014]")


def scan_text(text: str) -> tuple[list[str], list[str]]:
    secrets = [label for rx, label in SECRET_PATTERNS if rx.search(text)]
    internal = [label for rx, label in INTERNAL_PATTERNS if rx.search(text)]
    return secrets, internal


def lint_skill(skill_dir: pathlib.Path) -> list[CheckResult]:
    results: list[CheckResult] = []
    md = skill_dir / "SKILL.md"
    if not md.exists():
        return [CheckResult("skill_exists", False, f"no SKILL.md in {skill_dir}")]
    results.append(CheckResult("skill_exists", True, str(md)))

    text = md.read_text()
    m = FRONTMATTER_RE.match(text)
    if not m:
        results.append(CheckResult("frontmatter", False, "missing/invalid YAML frontmatter"))
        return results
    try:
        fm = yaml.safe_load(m.group(1)) or {}
    except yaml.YAMLError as e:
        results.append(CheckResult("frontmatter", False, f"frontmatter not valid yaml: {e}"))
        return results
    results.append(CheckResult("frontmatter", True, "parsed"))

    name = fm.get("name")
    results.append(CheckResult(
        "frontmatter_name", bool(name) and name == skill_dir.name,
        f"name={name!r} dir={skill_dir.name!r}",
    ))
    desc = (fm.get("description") or "").strip()
    results.append(CheckResult(
        "frontmatter_description", 20 <= len(desc) <= 1024,
        f"description length {len(desc)} (want 20..1024)",
    ))

    body = text[m.end():]
    py_bad, json_bad, n_py, n_json = [], [], 0, 0
    for lang, code in FENCE_RE.findall(body):
        lang = lang.lower()
        if lang in ("python", "py"):
            n_py += 1
            try:
                ast.parse(code)
            except SyntaxError as e:
                py_bad.append(f"L{e.lineno}: {e.msg}")
        elif lang == "json":
            n_json += 1
            try:
                json.loads(code)
            except ValueError as e:
                json_bad.append(str(e))
    results.append(CheckResult("python_fences_parse", not py_bad, f"{n_py} python fence(s); errors: {py_bad or 'none'}"))
    results.append(CheckResult("json_fences_parse", not json_bad, f"{n_json} json fence(s); errors: {json_bad or 'none'}"))

    missing = sorted({ref for ref in SCRIPT_REF_RE.findall(body) if not (skill_dir / ref).exists()})
    results.append(CheckResult("script_refs_exist", not missing, f"missing: {missing or 'none'}"))

    # Everything shipped in the skill, not just SKILL.md, is public.
    secrets, internal, dashes = set(), set(), []
    for f in sorted(skill_dir.rglob("*")):
        if not f.is_file() or "__pycache__" in f.parts:
            continue
        content = f.read_text(errors="replace")
        s, i = scan_text(content)
        secrets.update(f"{label} in {f.name}" for label in s)
        internal.update(f"{label} in {f.name}" for label in i)
        if DASH_RE.search(content):
            dashes.append(f.relative_to(skill_dir).as_posix())
    results.append(CheckResult("no_secrets", not secrets, f"secret-like values: {sorted(secrets) or 'none'}"))
    results.append(CheckResult("nothing_internal", not internal, f"internal references: {sorted(internal) or 'none'}"))
    results.append(CheckResult("no_em_dashes", not dashes, f"files with em/en dashes: {dashes or 'none'}"))
    return results
