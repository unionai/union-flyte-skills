"""Run eval scenarios locally or in CI.

  python -m evals.harness.run                      # everything
  python -m evals.harness.run --tier static        # lint every skill
  python -m evals.harness.run --skill union-handoff
  python -m evals.harness.run --scenario <id> -v   # show every check
"""

from __future__ import annotations

import argparse
import importlib
import json
import os
import subprocess
import sys
import tempfile
from dataclasses import asdict, dataclass, field
from pathlib import Path

from .checks import CheckResult, Outcome, run_checks, substitute
from .spec import REPO_ROOT, Manifest, Scenario, load_scenarios
from .static_lint import lint_skill


@dataclass
class ScenarioResult:
    scenario_id: str
    skill: str
    tier: str
    checks: list[CheckResult] = field(default_factory=list)

    @property
    def passed(self) -> bool:
        return bool(self.checks) and all(self.checks)

    def to_dict(self) -> dict:
        return {
            "scenario_id": self.scenario_id,
            "skill": self.skill,
            "tier": self.tier,
            "passed": self.passed,
            "checks": [asdict(c) for c in self.checks],
        }


def evaluate_static(sc: Scenario, manifest: Manifest) -> ScenarioResult:
    return ScenarioResult(sc.id, sc.skill, sc.tier, lint_skill(manifest.skill_dir(sc.skill)))


def evaluate_script(sc: Scenario, manifest: Manifest) -> ScenarioResult:
    """Build the fixture world in a temp dir, run the skill's script there with
    HOME pointed at it, and check what came out."""
    fixture = importlib.import_module(f"evals.fixtures.{sc.fixture}")
    script = manifest.skill_dir(sc.skill) / sc.script
    with tempfile.TemporaryDirectory(prefix=f"eval-{sc.id}-") as tmp:
        root = Path(tmp)
        variables = {k: str(v) for k, v in fixture.build(root).items()}
        variables.setdefault("root", str(root))
        home = variables.get("home", str(root))
        args = [substitute(a, variables) for a in sc.args]
        env = {
            k: v for k, v in os.environ.items()
            # The script must see only the fixture's world, not the runner's.
            if not k.endswith(("_TOKEN", "_API_KEY", "_SECRET")) and k not in ("CODEX_HOME", "CLAUDE_CONFIG_DIR")
        }
        env.update({"HOME": home, "NO_COLOR": "1", "GIT_CONFIG_NOSYSTEM": "1"})
        env.update(getattr(fixture, "ENV", {}))
        proc = subprocess.run(
            [sys.executable, str(script), *args],
            cwd=variables.get("cwd", home),
            env=env,
            text=True,
            capture_output=True,
            timeout=300,
        )
        outcome = Outcome(proc.returncode, proc.stdout, proc.stderr, root, variables)
        return ScenarioResult(sc.id, sc.skill, sc.tier, run_checks(outcome, list(sc.checks)))


def select(args) -> list[Scenario]:
    return [
        sc for sc in load_scenarios()
        if (not args.scenario or sc.id == args.scenario)
        and (not args.skill or sc.skill == args.skill)
        and (not args.tier or sc.tier == args.tier)
    ]


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="union-skill-evals", description=__doc__)
    ap.add_argument("--scenario", help="scenario id")
    ap.add_argument("--skill", help="filter by skill name")
    ap.add_argument("--tier", choices=["static", "script"], help="filter by tier")
    ap.add_argument("--json", dest="json_out", help="write full results JSON here")
    ap.add_argument("-v", "--verbose", action="store_true", help="print every check")
    args = ap.parse_args(argv)

    manifest = Manifest.load()
    scenarios = select(args)
    if not scenarios:
        print("no scenarios matched the filter", file=sys.stderr)
        return 2

    results = []
    for sc in scenarios:
        results.append(evaluate_static(sc, manifest) if sc.tier == "static" else evaluate_script(sc, manifest))

    failures = 0
    for r in results:
        failures += 0 if r.passed else 1
        print(f"{'ok  ' if r.passed else 'FAIL'} {r.scenario_id} ({r.tier})")
        for c in r.checks:
            if args.verbose or not c.passed:
                print(f"       {'ok  ' if c.passed else 'FAIL'} {c.kind}: {c.detail}")
    print(f"{len(results)} scenario(s), {failures} failing")
    if args.json_out:
        Path(args.json_out).write_text(json.dumps([r.to_dict() for r in results], indent=2))
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
