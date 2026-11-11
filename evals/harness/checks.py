"""Deterministic, LLM-free checks referenced by scenario specs.

Each check kind is a function ``(outcome: Outcome, params: dict) -> CheckResult``.
An ``Outcome`` is what a script-tier run produced: exit code, stdout, stderr and
the directory it ran against. No network, no LLM, so every check is
deterministic and unit-testable.

Add a kind by decorating a function with ``@check("my_kind")``.
"""

from __future__ import annotations

import glob as _glob
import json
import pathlib
import re
from dataclasses import dataclass, field
from typing import Any, Callable


@dataclass(frozen=True)
class CheckResult:
    kind: str
    passed: bool
    detail: str

    def __bool__(self) -> bool:
        return self.passed


@dataclass
class Outcome:
    returncode: int
    stdout: str
    stderr: str
    workdir: pathlib.Path
    vars: dict[str, str] = field(default_factory=dict)

    def json(self) -> Any:
        return json.loads(self.stdout)


CheckFn = Callable[[Outcome, dict], CheckResult]
_REGISTRY: dict[str, CheckFn] = {}


def check(kind: str) -> Callable[[CheckFn], CheckFn]:
    def deco(fn: CheckFn) -> CheckFn:
        _REGISTRY[kind] = fn
        return fn

    return deco


def kinds() -> list[str]:
    return sorted(_REGISTRY)


def run_checks(outcome: Outcome, specs: list[dict[str, Any]]) -> list[CheckResult]:
    results: list[CheckResult] = []
    for spec in specs:
        kind = spec.get("kind")
        fn = _REGISTRY.get(kind)
        if fn is None:
            results.append(CheckResult(str(kind), False, f"unknown check kind {kind!r}"))
            continue
        try:
            results.append(fn(outcome, spec))
        except Exception as exc:  # a check crash is a failure, never a harness crash
            results.append(CheckResult(str(kind), False, f"check raised: {exc!r}"))
    return results


def substitute(value: str, variables: dict[str, str]) -> str:
    """Replace ``{name}`` placeholders a fixture defined; leave others alone."""
    for k, v in variables.items():
        value = value.replace("{" + k + "}", v)
    return value


def _label(p: dict, default: str) -> str:
    return p.get("name") or default


def json_path(data: Any, path: str) -> Any:
    """Resolve ``a.b[0].c`` against parsed JSON. ``[*]`` maps over a list."""
    tokens = re.findall(r"[^.\[\]]+|\[\*\]|\[\d+\]", path)
    cur: Any = [data]
    for tok in tokens:
        nxt: list[Any] = []
        for item in cur:
            if tok == "[*]":
                nxt.extend(item)
            elif tok.startswith("["):
                nxt.append(item[int(tok[1:-1])])
            else:
                nxt.append(item[tok])
        cur = nxt
    return cur if "[*]" in path else cur[0]


# ---------------------------------------------------------------------------- #
# Check kinds
# ---------------------------------------------------------------------------- #


@check("exit_code")
def _exit_code(o: Outcome, p: dict) -> CheckResult:
    want = int(p.get("equals", 0))
    return CheckResult(_label(p, "exit_code"), o.returncode == want, f"exit {o.returncode} (want {want}); stderr: {o.stderr[-300:]!r}")


@check("stdout_regex")
def _stdout_regex(o: Outcome, p: dict) -> CheckResult:
    pat = substitute(p["pattern"], o.vars)
    ok = re.search(pat, o.stdout, re.MULTILINE) is not None
    return CheckResult(_label(p, "stdout_regex"), ok, f"/{pat}/ {'found' if ok else 'missing'} in stdout")


@check("not_regex")
def _not_regex(o: Outcome, p: dict) -> CheckResult:
    """Pattern appears in neither stdout nor stderr (e.g. a secret value)."""
    pat = substitute(p["pattern"], o.vars)
    hit = re.search(pat, o.stdout + "\n" + o.stderr, re.MULTILINE)
    return CheckResult(_label(p, "not_regex"), hit is None, f"/{pat}/ {'LEAKED: ' + hit.group(0) if hit else 'absent'}")


@check("json_equals")
def _json_equals(o: Outcome, p: dict) -> CheckResult:
    got = json_path(o.json(), p["path"])
    want = p["equals"]
    if isinstance(want, str):
        want = substitute(want, o.vars)
    return CheckResult(_label(p, f"json {p['path']}"), got == want, f"{p['path']} = {got!r} (want {want!r})")


@check("json_contains")
def _json_contains(o: Outcome, p: dict) -> CheckResult:
    """The value at ``path`` (a list, or a string) contains ``item``."""
    got = json_path(o.json(), p["path"])
    item = p["item"]
    if isinstance(item, str):
        item = substitute(item, o.vars)
    return CheckResult(_label(p, f"json {p['path']} contains"), item in got, f"{item!r} in {p['path']} = {got!r}")


@check("json_min")
def _json_min(o: Outcome, p: dict) -> CheckResult:
    got = json_path(o.json(), p["path"])
    n = len(got) if isinstance(got, (list, dict, str)) else got
    return CheckResult(_label(p, f"json {p['path']} >= {p['min']}"), n >= p["min"], f"{p['path']} -> {n}")


@check("file_exists")
def _file_exists(o: Outcome, p: dict) -> CheckResult:
    pattern = substitute(p["glob"], o.vars)
    matches = _glob.glob(str(o.workdir / pattern), recursive=True)
    return CheckResult(_label(p, "file_exists"), bool(matches), f"{len(matches)} match(es) for {pattern!r}")


@check("file_regex")
def _file_regex(o: Outcome, p: dict) -> CheckResult:
    pattern = substitute(p["glob"], o.vars)
    pat = substitute(p["pattern"], o.vars)
    files = [pathlib.Path(f) for f in _glob.glob(str(o.workdir / pattern), recursive=True)]
    hit = any(re.search(pat, f.read_text(errors="replace"), re.MULTILINE) for f in files if f.is_file())
    return CheckResult(_label(p, "file_regex"), hit, f"/{pat}/ in {pattern!r} ({len(files)} file(s))")
