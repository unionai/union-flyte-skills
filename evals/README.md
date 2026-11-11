# Skill evals

A scaled-down version of the harness in
[flyteorg/flyte-agent-plugins](https://github.com/flyteorg/flyte-agent-plugins/tree/main/evals).
Everything here is deterministic: no LLM, no agent, no credentials.

## Tiers

- **static**: lints a skill's `SKILL.md` and every file shipped with it.
  Frontmatter, `name` matching the directory, description length, python and
  JSON fences that parse, every `scripts/...` path the body mentions exists,
  no secret-like values, nothing internal (tenant hostnames, workspace links),
  no em dashes. Every skill must have one (a unit test enforces it).
- **script**: builds a fixture world in a temp directory (`evals/fixtures/`),
  runs one of the skill's bundled scripts with `HOME` pointed at it, and checks
  the exit code, stdout, the JSON it printed and the files it wrote. This is
  how the deterministic half of a skill, the part a script does rather than
  the model, is held to a spec.

## Run

```bash
pip install pyyaml pytest
export PYTHONPATH=$(git rev-parse --show-toplevel)

python -m evals.harness.run -v                 # every scenario
python -m evals.harness.run --tier static      # lint only
python -m evals.harness.run --skill union-handoff
python -m evals.harness.public_lint            # whole-repo public-content scan
python -m pytest -q                            # harness + skill unit tests
```

## Layout

```
manifest.yaml            where skills and scenarios live
scenarios/<skill>/*.yaml one scenario per file
fixtures/<name>.py       build(root) -> {placeholders} for script scenarios
harness/                 spec, checks, static_lint, public_lint, run
tests/                   unit tests for the harness
```

## Not here (yet)

The upstream harness also runs a real agent against each skill (trajectory
tier) with and without the skill installed, and judges the output with an LLM.
That needs a model endpoint and credentials in CI, so it is left out of this
repo for now.
