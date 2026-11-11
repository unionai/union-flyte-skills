#!/usr/bin/env python3
"""Move local coding-agent sessions into a Union agent.

  handoff.py discover [--harness H] [--limit N] [--json]
      List recent sessions of every harness found on this machine.

  handoff.py report --session H:ID [--session ...] [--remote-harness H] [--json]
      What moving them involves, before anything is copied: repos and how
      each would travel, sizes, the tools they ran, the credentials found.

  handoff.py stage --session H:ID [...] --out DIR --remote-harness H
                   [--credential ID ...] [--tree REPO ...] [--exclude REPO ...]
      Build the agent home in DIR/home, locally, for review.

  handoff.py seal --staged DIR (--volume NAME [--artifact NAME] | --merge-artifact NAME)
                  [--bucket URI] [--project P --domain D] [--dry-run]
      Copy the staged home into a new (or forked) Union Volume and seal it.

  handoff.py launch --locator LOC --identifier NAME [--project system --domain production]
                    [--launcher-task agent-workspace-launcher.launch_agent_workspace] [--dry-run]
      Start a Union agent with the sealed volume as its home (sessions not
      started), through `flyte run deployed-task`.

Standard library only (Python 3.9+). Reads never write; stage writes only
under --out; seal writes only into the volume it mounts.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import harnesses as hz  # noqa: E402
import inventory as inv  # noqa: E402


def human(n: int) -> str:
    size = float(n)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if size < 1024 or unit == "TB":
            return f"{size:.0f} {unit}" if unit == "B" else f"{size:.1f} {unit}"
        size /= 1024
    return f"{n} B"


def _sessions(keys: list[str]) -> list[hz.Session]:
    pool = hz.discover()
    try:
        return [hz.find(k, pool) for k in keys]
    except LookupError as e:
        raise SystemExit(str(e))


# --------------------------------------------------------------------------- #
# discover
# --------------------------------------------------------------------------- #


def cmd_discover(args) -> int:
    sessions = hz.discover([args.harness] if args.harness else None)
    if args.cwd:
        want = os.path.realpath(args.cwd)
        sessions = [s for s in sessions if s.cwd and os.path.realpath(s.cwd).startswith(want)]
    sessions = sessions[: args.limit]
    if args.json:
        rows = [s.to_dict() for s in sessions]
        for r in rows:
            r["title"] = inv.redact_values(r["title"])
        print(json.dumps(rows, indent=1))
        return 0
    if not sessions:
        found = [h for h, (lister, _) in hz.READERS.items()]
        print(f"No sessions found for: {', '.join(found)}.")
        print("If your harness keeps them somewhere else, find that directory and read")
        print("references/harnesses.md for what this script expects.")
        return 0
    print(f"{'SESSION':<48} {'UPDATED':<17} {'SIZE':>8}  CWD / TITLE")
    for s in sessions:
        print(f"{s.key[:48]:<48} {hz.fmt_time(s.updated_at):<17} {human(s.size_bytes):>8}  {s.cwd or '-'}")
        if s.title:
            print(f"{'':<76}{inv.redact_values(s.title)}")
    return 0


# --------------------------------------------------------------------------- #
# report
# --------------------------------------------------------------------------- #


def default_remote(local: str) -> str:
    """The same harness when a Union agent can run it, else Claude."""
    return local if local in hz.REMOTE_HARNESSES else "claude"


def build_report(sessions: list[hz.Session], remote_harness: str | None,
                 exclude: set[str] = frozenset(), include: set[str] = frozenset(),
                 ship: set[str] = frozenset()) -> dict:
    import stage as stg

    home = hz.home()
    big = sum(s.size_bytes for s in sessions)
    if big > 50 * 1024 * 1024:
        print(f"Reading and scanning {human(big)} of transcripts; this can take a few minutes.", file=sys.stderr)
    parsed = {s.key: hz.parse(s) for s in sessions}
    repo_map: dict[str, inv.Repo] = {}
    session_rows = []
    for d in sorted(include):
        top = inv.git_toplevel(Path(d)) or Path(d).resolve()
        repo_map[str(top)] = inv.describe_repo(top)
    for s in sessions:
        p = parsed[s.key]
        tops = [t for t in inv.repos_for(p.cwds or [s.cwd], p.paths, home) if str(t) not in exclude]
        for top in tops:
            r = repo_map.get(str(top)) or inv.describe_repo(top)
            r.sessions.append(s.key)
            repo_map[str(top)] = r
        target = remote_harness or default_remote(s.harness)
        resumable = s.harness == target
        row = s.to_dict()
        row["title"] = inv.redact_values(row["title"])
        session_rows.append({
            **row,
            "repos": [str(t) for t in tops],
            "commands": len(p.commands),
            "first_request": inv.redact_values(hz._short(p.first_user, 200)),
            "last_request": inv.redact_values(hz._short(p.last_user, 200)),
            "flagged_in_transcript": stg.transcript_secrets(s),
            "remote_harness": target,
            "arrives": "resumable" if resumable else "handoff note",
        })
    repos = list(repo_map.values())
    for r in repos:
        if r.path in ship and r.plan == "reference":
            r.plan, r.plan_note = "bundle", "--ship"
    tools = inv.tools_from([c for p in parsed.values() for c in p.commands], home)
    creds, env_names = inv.credentials(home, repos)

    transcripts = sum(s.size_bytes for s in sessions)
    repo_bytes = 0
    for r in repos:
        if r.plan == "reference":
            repo_bytes += r.unpushed_bytes + r.patch_bytes + r.untracked_bytes
        elif r.git and r.head:
            repo_bytes += r.history_bytes + r.patch_bytes + r.untracked_bytes
        else:
            repo_bytes += r.tree_bytes
    return {
        "sessions": session_rows,
        "repos": [r.to_dict() for r in repos],
        "tools": [t.to_dict() for t in tools],
        "credentials": [c.to_dict() for c in creds],
        "flagged_env_names": env_names,
        "totals": {
            "transcript_bytes": transcripts,
            "repo_bytes_estimate": repo_bytes,
            "total_bytes_estimate": transcripts + repo_bytes,
        },
    }


def print_report(rep: dict) -> None:
    print("SESSIONS")
    for s in rep["sessions"]:
        print(f"  {s['key']}  {human(s['size_bytes'])}  arrives as: {s['arrives']} on {s['remote_harness']}")
        print(f"    cwd: {s['cwd']}")
        if s["title"]:
            print(f"    title: {s['title']}")
        print(f"    shell commands run: {s['commands']}")
        if s["flagged_in_transcript"]:
            kinds = ", ".join(f"{n} {k}" for k, n in s["flagged_in_transcript"].items())
            print(f"    WARNING: the transcript holds credential-looking values ({kinds}).")
            print("      A transcript is copied whole; rotate them or leave this session out.")
    print("\nREPOS")
    if not rep["repos"]:
        print("  none found (the sessions' working directories are not git repos)")
    for r in rep["repos"]:
        if r["git"] and not r["head"]:
            print(f"  {r['path']}  (git, no commits yet)  travels as: full tree, {human(r['tree_bytes'])}")
        elif r["git"]:
            print(f"  {r['path']}  branch {r['branch']}  remote {r['remote'] or '(none)'}")
            if r["plan"] == "reference":
                print(f"    travels by reference: re-cloned from {r['clone_url']} when the agent first boots.")
                print(f"      Shipped: every local branch and tag commit the remote lacks ({human(r['unpushed_bytes'])}),")
                print("      the uncommitted changes and the untracked files. The agent needs read access to that")
                print("      remote; ship the whole history instead with --ship <repo>.")
            else:
                why = f" ({r['plan_note']})" if r.get("plan_note") else ""
                print(f"    travels as: git bundle of {len(r['branches'])} local branch(es), remote-tracking branches and tags,"
                      f" up to {human(r['history_bytes'])} of history{why}")
            if r["uses_lfs"]:
                print("    WARNING: uses Git LFS. LFS files that were never pushed are not carried; push them first.")
            print(f"    uncommitted: {r['modified_files']} modified file(s), patch {human(r['patch_bytes'])};"
                  f" {r['untracked_files']} untracked file(s), {human(r['untracked_bytes'])}")
            if r["unpushed_commits"]:
                print(f"    {r['unpushed_commits']} commit(s) on local branches or tags that are not on the remote:"
                      " push them first, or they travel in the volume")
            if r["secret_files"]:
                print(f"    secret-looking untracked files, left behind unless picked: {', '.join(r['secret_files'])}")
            if r["tracked_secret_files"]:
                print(f"    WARNING: committed files that look like secrets travel in the history: {', '.join(r['tracked_secret_files'])}")
            if r["flagged_in_patch"]:
                kinds = ", ".join(f"{n} {k}" for k, n in r["flagged_in_patch"].items())
                print(f"    WARNING: the uncommitted changes hold credential-looking values ({kinds})")
            if r["submodules"]:
                print(f"    {r['submodules']} submodule(s): set up again from their remotes on first boot; work not"
                      " committed and pushed inside a submodule is NOT carried")
            if r["worktree"]:
                print("    a linked git worktree: it is rebuilt as a standalone repo from the bundle")
        else:
            print(f"  {r['path']}  (not git)  travels as: full tree, {human(r['tree_bytes'])}")
            if r["secret_files"]:
                print(f"    secret-looking files, left behind unless picked: {', '.join(r['secret_files'])}")
    print("\nTOOLS the sessions ran (binaries are never copied; a Linux build is installed instead)")
    if not rep["tools"]:
        print("  none beyond the base system")
    for t in rep["tools"]:
        where = t["installed_by"] + (f" ({t['package']})" if t["package"] and t["package"] != t["name"] else "")
        print(f"  {t['name']:<16} {t['count']:>5}x  local: {where:<22} remote: {t['linux_install'] or 'nothing to install (OS or repo)'}")
    print("\nCREDENTIALS found (values never read into this report)")
    if not rep["credentials"]:
        print("  none")
    for c in rep["credentials"]:
        flag = "" if c["copyable"] else "  [never copied]"
        print(f"  {c['id']:<26} {c['kind']}: {c['path']}{flag}")
        print(f"    {'' if not c['copyable'] else 'instead of copying: '}{c['advice']}")
    if rep["flagged_env_names"]:
        print("  environment variables that look secret (not copyable; use project secrets):")
        print("    " + ", ".join(rep["flagged_env_names"]))
    t = rep["totals"]
    print("\nSIZE")
    print(f"  transcripts {human(t['transcript_bytes'])}; repos about {human(t['repo_bytes_estimate'])};"
          f" total about {human(t['total_bytes_estimate'])}")
    print("  (an estimate: repo history is the whole object store; skills, memory and the checked-out")
    print("  files come on top. `stage` prints the staged size.)")
    print("\nThe report covers credential files and values in transcripts and uncommitted changes.")
    print("It cannot see a secret committed and later deleted: that stays in the git history.")


def cmd_report(args) -> int:
    rep = build_report(_sessions(args.session), args.remote_harness,
                       exclude={str(Path(p).expanduser().resolve()) for p in args.exclude or []},
                       include={str(Path(p).expanduser().resolve()) for p in args.include or []},
                       ship={str(Path(p).expanduser().resolve()) for p in args.ship or []})
    if args.json:
        print(json.dumps(rep, indent=1))
    else:
        print_report(rep)
    return 0


# --------------------------------------------------------------------------- #
# stage / seal
# --------------------------------------------------------------------------- #


def cmd_stage(args) -> int:
    import stage as stg

    sessions = _sessions(args.session)
    opts = stg.StageOptions(
        out=Path(args.out).expanduser().resolve(),
        remote_harness=args.remote_harness,
        remote_home=args.remote_home,
        credentials=set(args.credential or []),
        tree_repos={str(Path(p).expanduser().resolve()) for p in args.tree or []},
        ship_repos={str(Path(p).expanduser().resolve()) for p in args.ship or []},
        memory=args.memory,
        cpu=args.cpu,
        exclude_repos={str(Path(p).expanduser().resolve()) for p in args.exclude or []},
        include_dirs={str(Path(p).expanduser().resolve()) for p in args.include or []},
        allow_transcript_secrets=args.allow_transcript_secrets,
        log=lambda m: print(m, file=sys.stderr),
    )
    manifest = stg.stage(sessions, opts)
    if args.json:
        print(json.dumps(manifest, indent=1))
    else:
        print(f"\nStaged {len(manifest['sessions'])} session(s) into {manifest['home']} ({human(manifest['bytes'])}).")
        print("Review it, then seal it with `handoff.py seal --staged", opts.out, "...`.")
    return 0


def cmd_seal(args) -> int:
    import seal as sl

    staged = Path(args.staged).expanduser().resolve()
    home = staged / "home" if (staged / "home").is_dir() else staged
    opts = sl.SealOptions(
        staged_home=home,
        volume=args.volume,
        artifact=args.artifact,
        merge_artifact=args.merge_artifact,
        bucket=args.bucket,
        project=args.project,
        domain=args.domain,
        flyte=args.flyte,
        mountpoint=Path(args.mountpoint).resolve() if args.mountpoint else None,
        dry_run=args.dry_run,
    )
    result = sl.seal(opts)
    print(json.dumps(result, indent=1))
    return 0


# Union's agent launcher, deployed in each organization that runs agents.
LAUNCHER_TASK = "agent-workspace-launcher.launch_agent_workspace"


# Runs inside the Python that has flyte-sdk (the user's flyte CLI's). The
# Python API rather than `flyte run deployed-task`, whose CLI cannot pass the
# launcher's enum input today.
LAUNCH_SNIPPET = """
import json, sys
import flyte, flyte.remote as r
a = json.loads(sys.argv[1])
if a["config"]:
    flyte.init_from_config(a["config"], project=a["project"], domain=a["domain"])
else:
    flyte.init_from_config(project=a["project"], domain=a["domain"])
task = r.Task.get(a["task"], project=a["project"], domain=a["domain"], auto_version="latest")
run = flyte.run(task, identifier=a["identifier"], home_locator=a["locator"])
# The launcher admits or refuses the agent (a login already in use, an
# agent already on) early in its run; wait that long for the verdict. A run
# still going after that is the agent, on.
import time
verdict, error = "on", ""
deadline = time.time() + a["wait"]
while time.time() < deadline:
    details = r.ActionDetails.get(run_name=run.name, name="a0")
    phase = details.pb2.status.phase
    if phase >= 5:  # finished before the agent came up: refused or failed
        verdict = "refused"
        error = details.pb2.error_info.message if details.pb2.HasField("error_info") else f"phase {phase}"
        break
    time.sleep(10)
print(json.dumps({"run": run.name, "url": run.url, "verdict": verdict, "error": error}))
"""


def flyte_python(flyte_cli: str) -> str:
    """The interpreter behind the user's `flyte` CLI, which has flyte-sdk."""
    import shutil

    path = shutil.which(flyte_cli)
    if path:
        try:
            first = open(path, "rb").readline().decode(errors="replace")
        except OSError:
            first = ""
        if first.startswith("#!") and "python" in first:
            return first[2:].strip().split()[0]
    return sys.executable


def launch_command(args) -> list[str]:
    payload = {"task": args.launcher_task, "project": args.project, "domain": args.domain,
               "identifier": args.identifier, "locator": args.locator, "config": args.config,
               "wait": args.wait}
    return [args.python or flyte_python(args.flyte), "-c", LAUNCH_SNIPPET, json.dumps(payload)]


def cmd_launch(args) -> int:
    """Start an agent whose home is the sealed volume. A new identifier makes
    a new agent; the identifier of an existing agent that is off gives that
    agent this home from now on (its later Turn ons continue from it). An
    agent that is on is refused by Union: turn it off first."""
    import subprocess

    cmd = launch_command(args)
    if args.dry_run:
        print(f"{args.launcher_task} in {args.project}/{args.domain}: identifier={args.identifier} "
              f"home_locator={args.locator} (via {cmd[0]})")
        return 0
    proc = subprocess.run(cmd, text=True, capture_output=True)
    out = proc.stdout + proc.stderr
    if proc.returncode != 0:
        print(out[-3000:], file=sys.stderr)
        if "AgentAlreadyOn" in out:
            print(f"Agent {args.identifier!r} is on. Turn it off first, or pick a new identifier.", file=sys.stderr)
        elif "No module named 'flyte'" in out:
            print("flyte-sdk is not importable there; pass --python <interpreter with flyte installed>.", file=sys.stderr)
        return proc.returncode
    result = json.loads(proc.stdout.strip().splitlines()[-1])
    print(json.dumps(result))
    if result["verdict"] == "refused":
        print(f"Union refused to start {args.identifier!r}: {result['error']}", file=sys.stderr)
        if "already in use" in result["error"]:
            print("Give this agent a login of its own (see the skill), then launch again with the same identifier.",
                  file=sys.stderr)
        return 1
    print(f"Agent {args.identifier!r} is starting (run {result['run']}). Its sessions wait under Done; "
          "Restart resumes each one.", file=sys.stderr)
    return 0


def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(prog="handoff.py", description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="command", required=True)
    harness_names = sorted(hz.READERS)

    p = sub.add_parser("discover", help="list recent sessions")
    p.add_argument("--harness", choices=harness_names)
    p.add_argument("--cwd", help="only sessions whose working directory is under this path")
    p.add_argument("--limit", type=int, default=20)
    p.add_argument("--json", action="store_true")
    p.set_defaults(func=cmd_discover)

    p = sub.add_parser("report", help="what moving these sessions involves")
    p.add_argument("--session", action="append", required=True, help="harness:id (repeatable)")
    p.add_argument("--remote-harness", choices=hz.REMOTE_HARNESSES, help="default: each session's own harness")
    p.add_argument("--exclude", action="append", help="leave this repo out")
    p.add_argument("--include", action="append", help="also move this repo or directory")
    p.add_argument("--ship", action="append", help="plan this repo to ship whole instead of re-cloning")
    p.add_argument("--json", action="store_true")
    p.set_defaults(func=cmd_report)

    p = sub.add_parser("stage", help="build the agent home locally")
    p.add_argument("--session", action="append", required=True)
    p.add_argument("--out", required=True, help="a new, empty directory")
    p.add_argument("--remote-harness", choices=hz.REMOTE_HARNESSES, required=True)
    p.add_argument("--remote-home", default="/root", help="the agent's home directory (default: /root)")
    p.add_argument("--credential", action="append", help="credential id from the report to copy (repeatable)")
    p.add_argument("--tree", action="append", help="copy this repo as a full tree instead of a bundle")
    p.add_argument("--ship", action="append", help="ship this repo's whole history instead of re-cloning it on first boot")
    p.add_argument("--memory", default="2Gi", help="memory for each restored session (default: 2Gi)")
    p.add_argument("--cpu", default="2", help="CPUs for each restored session (default: 2)")
    p.add_argument("--exclude", action="append", help="leave this repo behind")
    p.add_argument("--include", action="append", help="also move this repo or directory")
    p.add_argument("--allow-transcript-secrets", action="store_true",
                   help="stage even though a transcript holds credential-looking values")
    p.add_argument("--json", action="store_true")
    p.set_defaults(func=cmd_stage)

    p = sub.add_parser("seal", help="copy the staged home into a Union Volume and seal it")
    p.add_argument("--staged", required=True, help="the --out directory of `stage`")
    p.add_argument("--volume", help="name for a NEW volume")
    p.add_argument("--artifact", help="register the new volume under this artifact name")
    p.add_argument("--merge-artifact", help="fork this existing agent home and merge into it")
    p.add_argument("--bucket")
    p.add_argument("--project")
    p.add_argument("--domain")
    p.add_argument("--mountpoint", help="where to mount (default: a temp directory)")
    p.add_argument("--flyte", default="flyte", help="the flyte CLI to run")
    p.add_argument("--dry-run", action="store_true", help="print the flyte command only")
    p.set_defaults(func=cmd_seal)
    p = sub.add_parser("launch", help="start a Union agent with the sealed volume as its home")
    p.add_argument("--locator", required=True, help="the locator `seal` printed")
    p.add_argument("--identifier", required=True,
                   help="the agent's name: a new one, or an existing agent that is turned off (to give it this home)")
    p.add_argument("--launcher-task", default=LAUNCHER_TASK, help=f"the deployed task that starts agents (default: {LAUNCHER_TASK})")
    p.add_argument("--project", default="system", help="the project the launcher lives in (default: system)")
    p.add_argument("--domain", default="production", help="the domain the launcher lives in (default: production)")
    p.add_argument("--config", help="flyte config file (default: flyte's own lookup)")
    p.add_argument("--flyte", default="flyte", help="the flyte CLI whose Python to use")
    p.add_argument("--python", help="a Python with flyte-sdk installed (default: the flyte CLI's)")
    p.add_argument("--wait", type=int, default=180, help="seconds to wait for Union to admit or refuse it (default: 180)")
    p.add_argument("--dry-run", action="store_true", help="say what would be launched, launch nothing")
    p.set_defaults(func=cmd_launch)
    return ap


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if getattr(args, "command", None) == "seal" and bool(args.volume) == bool(args.merge_artifact):
        raise SystemExit("seal needs exactly one of --volume (new agent) or --merge-artifact (existing agent)")
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
