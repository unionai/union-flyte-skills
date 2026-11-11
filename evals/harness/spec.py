"""Scenario and manifest schema + loaders. Pure data, no subprocess.

A scenario is one YAML mapping under ``evals/scenarios/<skill>/``:

  id: unique id
  skill: the skill under test (a directory under plugins/union/skills/)
  tier: static | script
  # tier: script only
  fixture: a module in evals/fixtures/ whose build(root) makes the world
  script: path inside the skill, e.g. scripts/handoff.py
  args: [argv passed to the script; {placeholders} come from the fixture]
  checks: [{kind: ..., ...}]   see harness/checks.py
"""

from __future__ import annotations

import pathlib
from dataclasses import dataclass, field
from typing import Any

import yaml

REPO_ROOT = pathlib.Path(__file__).resolve().parents[2]
EVALS_ROOT = REPO_ROOT / "evals"

VALID_TIERS = ("static", "script")


@dataclass(frozen=True)
class Scenario:
    id: str
    skill: str
    tier: str
    fixture: str | None = None
    script: str | None = None
    args: tuple[str, ...] = ()
    checks: tuple[dict[str, Any], ...] = ()
    source_path: pathlib.Path | None = field(default=None, compare=False)

    @staticmethod
    def from_dict(d: dict[str, Any], source_path: pathlib.Path | None = None) -> "Scenario":
        loc = str(source_path) if source_path else "<scenario>"
        for key in ("id", "skill"):
            if key not in d:
                raise ValueError(f"{loc}: scenario missing required field {key!r}")
        tier = d.get("tier", "static")
        if tier not in VALID_TIERS:
            raise ValueError(f"{loc}: invalid tier {tier!r}, expected one of {VALID_TIERS}")
        if tier == "script":
            for key in ("fixture", "script", "checks"):
                if not d.get(key):
                    raise ValueError(f"{loc}: a script scenario needs {key!r}")
        return Scenario(
            id=str(d["id"]),
            skill=str(d["skill"]),
            tier=tier,
            fixture=d.get("fixture"),
            script=d.get("script"),
            args=tuple(str(a) for a in d.get("args") or ()),
            checks=tuple(d.get("checks") or ()),
            source_path=source_path,
        )


@dataclass(frozen=True)
class Manifest:
    skill_dir_template: str
    scenarios_dir: str

    @staticmethod
    def load(path: pathlib.Path | None = None) -> "Manifest":
        d = yaml.safe_load((path or EVALS_ROOT / "manifest.yaml").read_text())
        return Manifest(
            skill_dir_template=d.get("skill_dir_template", "plugins/union/skills/{skill}"),
            scenarios_dir=d.get("scenarios_dir", "evals/scenarios"),
        )

    def skill_dir(self, skill: str, repo_root: pathlib.Path = REPO_ROOT) -> pathlib.Path:
        return repo_root / self.skill_dir_template.format(skill=skill)


def load_scenarios(scenarios_dir: pathlib.Path | None = None) -> list[Scenario]:
    scenarios_dir = scenarios_dir or (EVALS_ROOT / "scenarios")
    out: list[Scenario] = []
    seen: dict[str, pathlib.Path] = {}
    for p in sorted(scenarios_dir.rglob("*.yaml")):
        doc = yaml.safe_load(p.read_text())
        if not isinstance(doc, dict):
            raise ValueError(f"{p}: scenario file must be a single YAML mapping")
        sc = Scenario.from_dict(doc, source_path=p)
        if sc.id in seen:
            raise ValueError(f"duplicate scenario id {sc.id!r} in {p} and {seen[sc.id]}")
        seen[sc.id] = p
        out.append(sc)
    return out
