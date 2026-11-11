# Packaging

The skills in `plugins/union/` are the single source of truth. `build.py`
vendors them into a PyPI source tree whose console script, `union-flyte-skills`,
copies them into an agent harness's skills directory.

```
packaging/
├── build.py            # writes ./build/pypi/union-flyte-skills/
├── set_version.py      # writes one version into every manifest
├── verify.py           # builds the wheel, installs it, exercises every target
└── templates/
    ├── cli.py          # the installer CLI
    └── README.md.tmpl  # the README shown on PyPI
```

```bash
python packaging/build.py     # build
python packaging/verify.py    # what CI runs
```

**Not published.** Nothing in this repo publishes to PyPI or creates a release
yet. When that is decided, the publish workflow from
flyteorg/flyte-agent-plugins (trusted publishing on a `v*` tag, gated on
`verify.py`) is the model to copy.
