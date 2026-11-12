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
.handoff/xdg-config/                what belongs in ~/.config (a picked gh or gcloud login)
```

A Union Volume reserves a few names at its root (`.config`, `.stats`,
`.accesslog`, `.trash`, `.control`): they are read-only virtual entries, so
nothing can be written under them. Anything staged under one moves aside,
`~/.config` to `.handoff/xdg-config/` and the others to
`.handoff/reserved/<name>/`, and the setup script copies `xdg-config` into
`$XDG_CONFIG_HOME`, or `~/.config` where that is a directory, or else
`~/.xdg-config` (exporting `XDG_CONFIG_HOME` in `~/.bashrc`).

## Repos

**By reference** (the default for a repo with a remote): nothing of the repo
itself is copied. `.handoff/repos/<repo>/repo.json` records the remote's name
and URL as configured (minus any http(s) credentials; an ssh user name is
kept), every local branch with its commit and upstream, the checked-out
branch or commit, and which repo it is a worktree of. What the remote has is
asked of the remote itself (`git ls-remote`), not read from tracking refs
that may be stale. Shipped beside it is only what the remote lacks: one
bundle of the commits of HEAD and of the branches checked out in the repo's
worktrees not reachable from the remote's current tips, the staged and
unstaged diffs, and the untracked files (without virtualenvs, `node_modules`
or build output). With `--all-branches` the bundle also carries every other
local branch and tag; without it, a branch whose commits stay behind is left
out of `repo.json` too, since the agent could not recreate it. If the remote cannot be reached, or shares no commits with the repo,
the repo is shipped whole instead.

On first boot `restore_repos.py` clones each main checkout (keeping the
remote's name), adds its worktrees with `git worktree add` (fetching a
worktree's own remote first when it differs), fetches the bundle, recreates
the local branches with their upstreams, checks out the same branch and
commit, applies the staged diff to the index and the unstaged one to the
working tree, copies the untracked files, and initialises submodules. It
works in a temporary directory and moves the result into place only when
every step succeeded, so a failure leaves nothing half-restored and a rerun
retries just that repo. A destination that already holds something is
refused, not overwritten.

Not carried either way: work inside submodules that was never pushed, and
Git LFS files that were never pushed (the report warns about both).

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
keep its user id, and git refuses a repo owned by someone else) and puts back
`~/.config` (above). Before re-cloning the repos it signs in to GitHub when a
re-cloned repo is on github.com or the sessions used `gh` (with no login yet,
`gh auth login --web` prints a one-time code and a link, then waits), runs
`gh auth setup-git`, and, when a repo's remote uses SSH and no SSH key was
copied, switches github.com to HTTPS (`gh config set git_protocol https` and
an `insteadOf` rule for `git@github.com:`). It only writes `~/.handoff/setup.done` once every tool
it lists is on the PATH.
