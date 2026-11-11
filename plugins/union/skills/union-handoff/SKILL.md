---
name: union-handoff
description: Move work from this machine's coding agent (Claude Code, Codex, pi, opencode, Hermes) into a Union agent. Finds recent sessions with their repos, branches, uncommitted changes, memory and skills; reports sizes, tools used and credentials found before copying anything; packages the chosen sessions into a sealed Union Volume as the agent's home, where they are listed as finished sessions that resume on Restart. Use when the user wants to hand off, move, continue or offload a local session to Union, or run it remotely.
---

# Hand off local sessions to a Union agent

You move one or more of this machine's coding-agent sessions, and everything
they were working on, into a **Union agent**: a hosted coding agent whose home
directory is a Union Volume. The sessions arrive as finished sessions in that
agent, and the user resumes each one from there.

The work is done by `scripts/handoff.py` (Python 3.9+, standard library only).
Run it with the skill directory as the base, for example
`python3 <skill-dir>/scripts/handoff.py discover`.

Follow the steps in order. **Copy nothing before step 3's report has been
shown and the user has answered steps 4 and 5.** Ask one question at a time
and wait for the answer.

## 1. Discover

```bash
python3 scripts/handoff.py discover --limit 20
```

It reads every harness it knows, newest first: session id, last update, size,
working directory and title. Filter with `--harness claude|codex|pi|opencode|hermes`
or `--cwd <dir>`. Where each harness keeps its sessions, and how that was
verified, is in [references/harnesses.md](references/harnesses.md).

The session you are running in right now is usually the newest one for your
own harness. Point it out.

**Unknown harness.** If yours is not listed or nothing is found, you know
better than the script where your own sessions and working files live. Find
the transcript file (or directory) and the working directory, and pass the
session as `file:<transcript path>@<working directory>` in the steps below.
Such a session always arrives as a written handoff (step 4).

## 2. Ask which sessions to move

Show the list and ask which to move: one or several. Sessions that share a repo
share its copy.

## 3. Report, before copying anything

```bash
python3 scripts/handoff.py report --session claude:<id> [--session ...]
```

Show the user the whole report, in your own words where it helps. It covers:

- **Repos.** Every git repo the sessions worked in (their working directory,
  plus any repo their tools edited), with its branch and worktrees. A repo
  with a remote travels **by reference**: the agent re-clones it from that
  remote when it first boots, recreates its worktrees, and checks out the same
  branch. Only what the remote does not have is shipped: commits not pushed
  yet (as a small bundle), staged and unstaged changes, and untracked files.
  A repo with no remote travels as a **git bundle** of its whole history plus
  that uncommitted work. A directory that is not a git repo travels as a
  **full tree**, without `node_modules`, virtualenvs and build output. Offer
  `--ship <repo>` to send a repo's whole history instead of re-cloning it,
  `--tree <repo>` for one the user wants copied as-is, `--exclude <repo>` to
  leave one behind, and
  `--include <dir>` for a repo the sessions used that the report missed (the
  script finds repos from working directories, file tools and `cd`/`git -C` in
  shell commands, but a script can reach a repo in ways it cannot see). Pass
  the same flags to `report` again to see the result.
- **Transcripts** and their sizes, and an estimated **total**.

For each repo with **unpushed commits or uncommitted changes**, ask the user
which they prefer: push them to the remote now (they, or you with their
explicit go-ahead, run `git push`; uncommitted work needs committing first),
or let them travel in the volume. Pushing keeps the volume small and the
work safe on the remote; shipping keeps it private until they push it.
A repo that travels by reference needs **read access to its remote from the
agent**: copy the GitHub CLI login (step below) or plan to sign in there
before running the setup script.
- **Tools** the sessions ran, counted from their shell commands, with how each
  was installed locally (brew, npm, pip/uv, cargo) and the Linux line that
  installs it in the agent. **Binaries are never copied**: a macOS binary does
  not run on Linux. A setup script installs Linux builds on first boot.
- **Credentials** found: GitHub CLI, git credential files, SSH keys, cloud CLI
  logins (AWS, Google Cloud, Azure), kubeconfigs, registry and package tokens,
  secret-looking files such as `.env`, `.npmrc` or `id_ed25519` in a repo, and
  the names (never the values) of secret-looking environment variables. The
  report never prints a credential's value.
- **Credentials inside what would be copied.** A transcript is copied whole,
  so a token the user pasted or a session printed travels with it; the report
  flags each transcript that holds credential-looking values, and the
  uncommitted changes too. Files that look like secrets and are already
  **committed** travel in the git history, and so does a secret committed and
  later deleted, which the report cannot see. Say so plainly; the fix is to
  rotate the secret, or leave that session or repo out.

For **each** credential, ask whether to copy it. Say plainly:

> Copying a credential into the agent's home means anyone who can open that
> agent, or mount its volume, can read it. The alternative is to sign in again
> inside the agent, or to give it a project secret.

The default is not to copy. Pick one with `--credential <id>` at the stage
step, using the id from the report (a file inside a repo has an id like
`file:/path/to/repo/.env`). Never offer the Claude or Codex login itself: the
agent signs in on its own. Environment variables cannot be copied; suggest
project secrets for them.

`stage` refuses to copy a transcript that holds credential-looking values
until the user has seen that and said to go ahead (`--allow-transcript-secrets`).

## 4. Ask which harness the agent runs

Union agents run **Claude Code** and **Codex**. Default to the local harness
(`claude` to `claude`, `codex` to `codex`). Before the user picks, explain what
each choice means, using the report's `arrives as` column:

- **Same harness:** each session arrives **resumable**, with its full
  conversation, in a working directory that maps to the old one.
- **Different harness, or one Union does not run (pi, opencode, Hermes, a
  `file:` session):** the files all move, but each session arrives as a
  **written handoff**: its transcript plus a `HANDOFF.md` note that a new
  session starts from. It cannot be resumed as-is.

Re-run the report with `--remote-harness <choice>` if it changes anything.

## 5. A new agent (merging into an existing one is not supported yet)

The handoff goes into a fresh volume, and a new agent starts with it as its
home (step 7). **Merging into an existing agent's home is not supported in
the Union console yet**: the console can only give a home volume to a new
agent. If the user asks for a merge, say so, and offer a new agent instead.

Also ask how much **memory** each restored session should get (default
`2Gi`; pass `--memory`, and `--cpu` for CPUs, default 2). It is recorded with
each session and used when it is restarted.

## 6. Stage, review, seal

Stage into a new local directory first, so the user can look before anything
leaves the machine:

```bash
python3 scripts/handoff.py stage --out ~/union-handoff/<name> \
  --remote-harness claude --session claude:<id> [--session ...] \
  [--credential <id> ...] [--ship <repo>] [--tree <repo>] [--exclude <repo>] \
  [--memory 2Gi] [--cpu 2]
```

Then, before sealing:

1. **Write the summaries.** For each session that arrives as a written
   handoff, open `home/handoff/<session>/HANDOFF.md` and replace the comment
   under "Summary" with a short summary of that session from its transcript:
   the goal, what is done, what is in progress, and the next step.
2. **Review the setup script** at `home/.handoff/setup.sh`. Fill in or delete
   its `TODO` lines; you know these tools better than the script does. It
   exits non-zero, and does not mark itself done, until every tool it lists
   is installed.
3. Tell the user what was staged and where (`home/.handoff/README.md` lists
   it), and ask them to confirm.

Seal it into a Union Volume with the `flyte` CLI from `flyteplugins-union`
(`pip install flyteplugins-union`; check `flyte create volume --help`). The
user needs a Union login (`flyte` config for their org, project and domain).

```bash
# New agent
python3 scripts/handoff.py seal --staged ~/union-handoff/<name> \
  --volume <name> --artifact <name> --project <project> --domain <domain>

```

Add `--dry-run` first to show the exact `flyte` command. `seal` mounts the
volume, copies the staged home in, and stops the mount, which seals a new,
immutable version and prints its locator. Pass `--bucket s3://...` to pick the
storage instead of the project's default.

Stopping a mount in any normal way (Ctrl-C, `kill`, `flyte unmount volume`)
publishes what is in it. So if the copy fails or is interrupted part way,
`seal` leaves the mount up and unpublished, and prints how to finish the copy
and publish, or discard it. Relay that to the user as it is; do not stop the
mount yourself.

## 7. Start the agent

`seal` printed the new version's **locator** (`s3://.../<id>.metadata.json`).
Do not start any session; they wait in the home, finished.

In the Union console, open **New agent** and paste the locator into
**Home volume** (an optional field). The agent boots with the handoff as its
home. The field takes a locator, not an artifact name, so keep the one `seal`
printed (it is also in the JSON `seal` writes, under `locator`).

Then tell the user where to find each session:

- **Resumable sessions** are listed under the agent's finished sessions, by
  the names in `home/.handoff/README.md`. **Restart** resumes one where it
  left off, in its repo under `src/`.
- **Written handoffs** are in `~/handoff/<session>/` inside the agent. Start a
  new session and ask it to read `~/handoff/<session>/HANDOFF.md` first.
- On first boot, run `bash ~/.handoff/setup.sh` once, **before restarting any
  session** (the home's `CLAUDE.md` or `AGENTS.md` tells the agent to). It
  re-clones the repos that travelled by reference and reapplies their local
  work, then installs the tools. A clone that fails usually means the agent
  is not signed in to the git host yet: sign in and run it again.
- Credentials that were not copied need a fresh sign-in inside the agent.

What lands where in the agent's home, and why, is in
[references/layout.md](references/layout.md).
