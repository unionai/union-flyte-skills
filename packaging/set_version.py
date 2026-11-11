#!/usr/bin/env python3
"""Write one version into every manifest that carries it.

    python packaging/set_version.py 0.2.0
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent

MANIFESTS = [
    REPO / "plugins/union/.claude-plugin/plugin.json",
    REPO / "plugins/union/.codex-plugin/plugin.json",
    REPO / "package.json",
]


def main() -> int:
    if len(sys.argv) != 2:
        print(__doc__.strip(), file=sys.stderr)
        return 2
    version = sys.argv[1].lstrip("v")
    for path in MANIFESTS:
        data = json.loads(path.read_text())
        data["version"] = version
        path.write_text(json.dumps(data, indent=2) + "\n")
        print(f"{path.relative_to(REPO)}: {version}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
