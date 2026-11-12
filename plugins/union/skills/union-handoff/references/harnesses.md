# Where harnesses keep their sessions

`scripts/harnesses.py` reads each harness below. Every location was checked
against that harness's source (or, for Claude Code, its shipped CLI and real
transcripts) in November 2026. Formats change; if a reader finds nothing, look
in the harness's docs for its current layout, and fall back to passing the
session as `file:<transcript>@<working directory>`.

| Harness | Sessions | Working directory | Shell commands | Resume by id |
|---|---|---|---|---|
| Claude Code | `$CLAUDE_CONFIG_DIR` or `~/.claude`, `projects/<slug>/<session-id>.jsonl` | `cwd` on every record | assistant `tool_use` blocks named `Bash`, `input.command` | `claude --resume <id>` |
| Codex CLI | `$CODEX_HOME` or `~/.codex`, `sessions/YYYY/MM/DD/rollout-<time>-<thread-id>.jsonl`; rollouts older than a week are `.jsonl.zst`; also `archived_sessions/` | `session_meta.payload.cwd`, `turn_context.payload.cwd` | `response_item` `function_call` named `exec_command` (`arguments.cmd`), older `shell` / `shell_command` (`arguments.command`), `local_shell_call` | `codex resume <id>` (no cwd filter) |
| pi | `$PI_CODING_AGENT_DIR` or `~/.pi/agent`, `sessions/--<cwd with / as ->--/<time>_<id>.jsonl` | first record `{"type": "session", "cwd": ...}` | assistant `toolCall` blocks named `bash`, `arguments.command` | not a Union harness |
| opencode | `$OPENCODE_DB` or `$XDG_DATA_HOME/opencode/opencode.db` (default `~/.local/share/opencode`), SQLite tables `session`, `message`, `part`; older builds used `storage/{session,message,part}/*.json` | `session.directory` | `part.data` with `type: "tool"`, `tool: "bash"`, `state.input.command` | not a Union harness |
| Hermes | `$HERMES_HOME` or `~/.hermes`, SQLite `state.db`, tables `sessions` and `messages` | `sessions.cwd` | assistant `messages.tool_calls` (JSON), function `terminal`, `arguments.command` | not a Union harness |

## The Claude Code project slug

Claude Code names a project directory after the working directory with every
character that is not a letter or digit replaced by `-`
(`/root/src/my_repo` becomes `-root-src-my-repo`). For paths longer than 200
characters it truncates and appends a hash, so match on the `cwd` field inside
the transcript rather than rebuilding a long slug.

## What the readers take from a session

Only what the handoff needs: the working directories, the commands its shell
tool ran, the file paths its tools named (to find repos it edited outside its
working directory), and its first and last messages (for the handoff note).
Readers open databases read-only and never write.
