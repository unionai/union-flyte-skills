import pathlib

import pytest

from evals.harness import checks
from evals.harness.checks import Outcome, json_path, run_checks
from evals.harness.public_lint import lint
from evals.harness.spec import REPO_ROOT, Manifest, Scenario, load_scenarios
from evals.harness.static_lint import lint_skill


def test_every_scenario_loads_and_names_a_real_skill():
    manifest = Manifest.load()
    for sc in load_scenarios():
        assert (manifest.skill_dir(sc.skill) / "SKILL.md").is_file(), sc.id


def test_every_skill_has_a_static_scenario():
    skills = {p.parent.name for p in (REPO_ROOT / "plugins/union/skills").glob("*/SKILL.md")}
    static = {sc.skill for sc in load_scenarios() if sc.tier == "static"}
    assert skills == static


def test_every_check_kind_used_exists():
    known = set(checks.kinds())
    for sc in load_scenarios():
        for c in sc.checks:
            assert c["kind"] in known, (sc.id, c["kind"])


def test_script_scenario_requires_fixture():
    with pytest.raises(ValueError):
        Scenario.from_dict({"id": "x", "skill": "y", "tier": "script", "script": "s.py", "checks": [{}]})


def test_json_path():
    data = {"a": [{"b": 1}, {"b": 2}], "c": {"d": "x"}}
    assert json_path(data, "a[1].b") == 2
    assert json_path(data, "a[*].b") == [1, 2]
    assert json_path(data, "c.d") == "x"


def test_checks_against_an_outcome(tmp_path: pathlib.Path):
    (tmp_path / "f.txt").write_text("hello world")
    o = Outcome(0, '{"items": ["a", "b"], "n": 3}', "", tmp_path, {"word": "hello"})
    results = run_checks(o, [
        {"kind": "exit_code"},
        {"kind": "json_contains", "path": "items", "item": "a"},
        {"kind": "json_min", "path": "n", "min": 3},
        {"kind": "not_regex", "pattern": "secret"},
        {"kind": "file_regex", "glob": "*.txt", "pattern": "{word}"},
    ])
    assert all(results), results
    bad = run_checks(o, [{"kind": "not_regex", "pattern": "items"}, {"kind": "nope"}])
    assert not any(bad)


def test_static_lint_flags_internal_and_secret(tmp_path: pathlib.Path):
    skill = tmp_path / "demo"
    skill.mkdir()
    (skill / "SKILL.md").write_text(
        "---\nname: demo\ndescription: A demo skill used only by this unit test.\n---\n"
        "See https://acme." + "hosted.unionai.cloud and token ghp_" + "a" * 36 + "\n"
        "Run scripts/missing.py \u2014 now\n"
    )
    by_kind = {c.kind: c.passed for c in lint_skill(skill)}
    assert by_kind["nothing_internal"] is False
    assert by_kind["no_secrets"] is False
    assert by_kind["script_refs_exist"] is False
    assert by_kind["no_em_dashes"] is False


def test_repo_has_no_public_leaks():
    assert lint() == []
