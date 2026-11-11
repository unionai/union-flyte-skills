# The agent home a handoff builds

`stage` writes `<out>/home`, laid out exactly as the Union agent's home will
be (`/root` by default; change it with `--remote-home`). `seal` copies that
tree into the volume unchanged.

```
src/<repo>/                       one per repo the sessions used
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

A git repo is rebuilt from a bundle of its local branches, remote-tracking
branches and tags, so branch upstreams and "ahead by N" survive. HEAD is
checked out as it was (branch or detached), the staged diff is applied to the
index, the unstaged diff to the working tree, and untracked files are copied.
Remotes keep their URLs; nothing is fetched or pushed. Untracked files that
look like secrets (`.env`, `*.pem`, `*.key`, `credentials.json`) stay behind
unless picked as credentials. Submodules are not followed.

## Re-keying transcripts

A session's working directory changes, say from `/Users/you/code/web` to
`/root/src/web`. For a session that arrives resumable:

- **Claude Code** keeps transcripts under a directory named after the working
  directory, so the file moves to `.claude/projects/-root-src-web/`, and the
  `cwd` field on every record is rewritten to the new path.
- **Codex** records the working directory in `session_meta` and
  `turn_context`; those `cwd` and `workspace_roots` fields are rewritten.

Only those fields change. Message text, tool output and thinking blocks are
copied byte for byte: some are signed, and a changed byte would make the
resume fail. So the conversation still mentions the old paths; the path map in
`.handoff/manifest.json` (and each `HANDOFF.md`) says what moved where.

## Finished sessions

`.agent-sessions/finished.json` holds one record per resumable session. Each
names the harness, the working directory relative to the home, and the
session id to resume, which is what makes the session show up as finished in
the agent and lets Restart resume it. When merging into an existing home,
records are added by key and nothing already there is replaced.

## The setup script

One `need <binary> <install line>` per tool the sessions ran that the agent
image may lack. A tool already present is skipped, so running it twice is
harmless. Lines the script could not work out are left as `TODO` comments.
