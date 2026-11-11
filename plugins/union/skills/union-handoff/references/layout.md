# The agent home a handoff builds

`stage` writes `<out>/home`, laid out exactly as the Union agent's home will
be (`/root` by default; change it with `--remote-home`). `seal` copies that
tree into the volume unchanged.

```
src/<repo>/                       a repo shipped whole (no remote, or --ship)
.handoff/repos/<repo>/            a repo travelling by reference: repo.json (remote,
                                  branch, worktree), unpushed.bundle, *.patch, untracked/
.handoff/restore_repos.py         re-clones those on first boot (run by setup.sh)
.claude/projects/<slug>/<id>.jsonl  a resumable Claude session, re-keyed
.claude/projects/<slug>/<id>/       its sub-agent transcripts and saved tool output
.claude/projects/<slug>/memory/     the project's Claude memory
.claude/CLAUDE.md                   your user-level Claude instructions
.claude/skills/ or .agents/skills/  your user-level skills, for the remote harness
.agent-sessions/codex/sessions/<id>/sessions/YYYY/MM/DD/rollout-*.jsonl
                                    a resumable Codex session, re-keyed
.agent-sessions/finished.json       the finished-session list the agent shows
handoff/<session>/                  a session that cannot resume: transcript + HANDOFF.md
.handoff/setup.sh                   installs the tools the sessions used
.handoff/README.md                  what was moved, and where
.handoff/manifest.json              the same, machine-readable, with the path map
.handoff/instructions/              instructions from harnesses the agent does not run
CLAUDE.md or AGENTS.md              a short note pointing at .handoff/ (appended)
<credential paths>                  only the credentials you picked, mode 0600
```

## Repos

**By reference** (the default for a repo with a remote): nothing of the repo
itself is copied. `.handoff/repos/<repo>/repo.json` records the remote URL
(minus any `user:password@`), the branch, the commit, and which repo it is a
worktree of. Shipped beside it is only what the remote lacks: a bundle of the
commits after the newest one the remote has (by the last fetch), the staged
and unstaged diffs, and the untracked files. On first boot
`restore_repos.py` clones each main checkout, adds its worktrees with
`git worktree add`, fetches the bundle, checks out the branch at the same
commit, applies the staged diff to the index and the unstaged one to the
working tree, and copies the untracked files. A repo already restored is
skipped, so it can run again after a failed clone.

**Shipped whole** (no remote, or `--ship`): rebuilt from a bundle of its
local branches, remote-tracking branches and tags, so branch upstreams and
"ahead by N" survive, with the same diffs and untracked files reapplied.

Either way, remotes keep their URLs minus any `user:password@` part, and
nothing is fetched or pushed during the handoff. Files that look like secrets (`.env`, `.npmrc`, `*.pem`, SSH keys,
`credentials.json`, Terraform state, and similar) stay behind unless picked as
credentials, here and in every other copy: trees, memory and skills. A
symlink in a skill or memory directory that points outside it is not
followed. Submodules are not followed either; the agent re-clones them.

## Re-keying transcripts

A session's working directory changes, say from `/Users/you/code/web` to
`/root/src/web`. For a session that arrives resumable:

- **Claude Code** keeps transcripts under a directory named after the working
  directory, so the file moves to `.claude/projects/-root-src-web/`, and the
  `cwd` field on every record is rewritten to the new path.
- **Codex** records the working directory in `session_meta` and
  `turn_context`; those `cwd` and `workspace_roots` fields are rewritten, as
  is the `cwd` in thread settings and environment snapshots.

Only those metadata fields change, even where a message or tool call happens
to contain a key named `cwd`. Message text, tool output and thinking blocks are
copied byte for byte: some are signed, and a changed byte would make the
resume fail. So the conversation still mentions the old paths; the path map in
`.handoff/manifest.json` (and each `HANDOFF.md`) says what moved where.

## Finished sessions

`.agent-sessions/finished.json` holds one record per resumable session. Each
names the harness, the working directory relative to the home, and the
session id to resume, which is what makes the session show up as finished in
the agent and lets Restart resume it. When merging into an existing home,
records are added by key (the source harness and session id) and nothing
already there is replaced; a name that is taken gets a suffix.

## The setup script

One `need <binary> <install line>` per tool the sessions ran that the agent
image may lack. A tool already present is skipped, so running it twice is
harmless. Lines the script could not work out are left as `TODO` comments.
It first takes ownership of the copied repos (files sealed on another machine
keep its user id, and git refuses a repo owned by someone else), and it only
writes `~/.handoff/setup.done` once every tool it lists is on the PATH.
