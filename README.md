# Union Flyte Skills

Agent skills for [Union](https://www.union.ai), the enterprise platform built
on [Flyte](https://flyte.org), for
[Claude Code](https://docs.claude.com/en/docs/claude-code),
[OpenAI Codex](https://developers.openai.com/codex), and any agent harness that
supports [Agent Skills](https://agentskills.io).

The open-source Flyte skills live in
[flyteorg/flyte-agent-plugins](https://github.com/flyteorg/flyte-agent-plugins).
This repo holds the skills that need Union itself.

## Skills

| Skill | What it does |
|---|---|
| [`union-docs`](plugins/union/skills/union-docs/SKILL.md) | Finds the right Union docs page before answering, instead of guessing flags. |

## Install

### As a plugin (Claude Code, Codex)

```bash
# Claude Code
/plugin marketplace add unionai/union-flyte-skills
/plugin install union@union-flyte-skills
```

For Codex, add this repository as a plugin marketplace; it reads
`.agents/plugins/marketplace.json`.

### With the installer, into any harness

The installer is not on PyPI yet. Run it from a checkout:

```bash
git clone https://github.com/unionai/union-flyte-skills
cd union-flyte-skills
python packaging/build.py
uvx --from ./build/pypi/union-flyte-skills union-flyte-skills install
```

It copies every skill into each harness it finds on this machine. Pick
explicitly with `--target` (repeatable):

| `--target` | User-level directory | Project-level (`--project`) | Read by |
|---|---|---|---|
| `agents` | `~/.agents/skills/` | `.agents/skills/` | Codex, Hermes (project), any harness following the convention |
| `claude` | `~/.claude/skills/` | `.claude/skills/` | Claude Code |
| `hermes` | `~/.hermes/skills/` | `.hermes/skills/` | Hermes |
| `opencode` | `~/.config/opencode/skills/` | `.opencode/skills/` | opencode |
| `pi` | `~/.pi/agent/skills/` | none | pi |

```bash
union-flyte-skills install --target claude   # one harness
union-flyte-skills install --project         # this repo only
union-flyte-skills install --dir ./skills    # any directory
union-flyte-skills install --dry-run         # preview, change nothing
union-flyte-skills install --force           # overwrite existing copies
union-flyte-skills uninstall                 # remove them again
union-flyte-skills list                      # list the bundled skills
```

`--target codex` is an alias for `agents`.

## Repository layout

```
.claude-plugin/marketplace.json     Claude Code marketplace
.agents/plugins/marketplace.json    Codex marketplace
plugins/union/                      the plugin: manifests + skills/<name>/SKILL.md
packaging/                          the installer CLI and its build/verify scripts
evals/                              static lint + script-tier scenarios
tests/                              unit tests for scripts bundled with skills
```

## Development

```bash
pip install pyyaml pytest build
python packaging/verify.py            # build the wheel, install it, exercise every target
python -m pytest -q                   # unit tests
python -m evals.harness.run -v        # skill lint + scenarios
python -m evals.harness.public_lint   # nothing secret or internal in a public repo
```

Contributions are welcome. Skills follow the
[Agent Skills](https://agentskills.io) format: a directory with a `SKILL.md`
whose frontmatter `name` matches the directory, plus any scripts or reference
files it needs.

## License

Apache-2.0. The installer and eval harness are adapted from
[flyteorg/flyte-agent-plugins](https://github.com/flyteorg/flyte-agent-plugins).
