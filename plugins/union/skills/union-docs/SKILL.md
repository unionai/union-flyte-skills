---
name: union-docs
description: Find the right Union documentation page before answering a question about Union or Flyte 2 (the flyte SDK and CLI, Volumes, apps, agents, deployment). Use when the user asks how to do something on Union, or when you are about to guess a flyte CLI flag or SDK signature.
---

# Union docs

Union is a commercial superset of open-source Flyte. It uses the same `flyte`
SDK and CLI, so most Flyte 2 questions are answered in the Union docs too.

Do not guess flags or signatures. Look them up:

1. Fetch the index, which lists every page with a one-line summary:
   `https://www.union.ai/docs/v2/union/llms.txt`
2. Pick the page that matches and fetch it. Every page has a Markdown twin:
   add `.md` to the page path instead of the trailing slash, for example
   `https://www.union.ai/docs/v2/union/user-guide/tasks/task-programming/volumes/local.md`.
3. For the open-source Flyte backend specifically, use
   `https://www.union.ai/docs/v2/flyte/llms.txt` instead.

The v1 docs are deprecated. Use them only when the user asks about v1.

For a CLI the user has installed, its own help is the ground truth for that
version: `flyte <verb> <noun> --help`, for example `flyte create volume --help`.
