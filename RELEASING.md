# Releasing

How to publish a new version of `union-flyte-skills` to PyPI. The version
comes from `plugins/union/.claude-plugin/plugin.json`, and the tag must match
it; CI refuses to publish otherwise.

Most users never need the PyPI package: `npx skills add unionai/union-flyte-skills`
and the Claude Code and Codex marketplaces all install straight from this repo.
The package is for `uvx union-flyte-skills install`, and its downloads are
counted (pypistats), which git installs are not.

## First-time setup

Needed once per repository. Skip it if
<https://pypi.org/project/union-flyte-skills/> already exists.

- [ ] Create a GitHub environment named `pypi` (**Settings > Environments**).
      Add required reviewers for a human gate before each publish.
- [ ] Sign in to the PyPI account that should own the project, and add a
      trusted publisher at <https://pypi.org/manage/account/publishing/>.
      Before the first release the project does not exist, so add it under
      **pending publishers**:

| Field | Value |
|---|---|
| PyPI project name | `union-flyte-skills` |
| Owner | `unionai` |
| Repository name | `union-flyte-skills` |
| Workflow name | `publish.yml` |
| Environment name | `pypi` |

A pending publisher does not reserve the name: whoever uploads first gets it.
The first release is what claims it, so cut `v0.1.0` soon after the setup.

## Release checklist

1. Pick the version. **patch** for wording and fixes, **minor** for new skills
   or a changed workflow, **major** for renaming or removing a skill. Check it
   is free (a version can never be re-uploaded, even after a yank):

   ```bash
   curl -s -o /dev/null -w '%{http_code}\n' https://pypi.org/pypi/union-flyte-skills/0.2.0/json
   # 404 = free
   ```

2. Set it everywhere and verify, on a branch:

   ```bash
   python packaging/set_version.py 0.2.0
   python packaging/verify.py
   git commit -s -am "Release 0.2.0"
   ```

   Land it through a PR.

3. Rehearse: run the `publish` workflow by hand with `dry_run` checked. It
   builds, verifies and runs `twine check`, and publishes nothing.

4. Tag the merged commit on `main` and push the tag:

   ```bash
   git tag v0.2.0 && git push origin v0.2.0
   ```

   The workflow publishes to PyPI through the `pypi` environment and attaches
   the wheel and sdist to a GitHub release.

5. Check it installs: `uvx union-flyte-skills@0.2.0 list`.
