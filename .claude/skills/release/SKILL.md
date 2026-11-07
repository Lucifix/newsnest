---
name: release
description: Cut a NewsNest release - decide whether main needs one, pick the version, bump it, draft notes, then tag and publish after the bump PR is merged. Use when asked to release, bump the version, tag, or "should we release?".
---

# Releasing NewsNest

Every push to main already publishes `latest`. A release gives people who pin `NEWSNEST_IMAGE_TAG` a version
to move to, puts the right number on the About page and in bug reports, and writes down what changed.

## 1. Is a release due?

```bash
git fetch --tags origin
git describe --tags --abbrev=0 origin/main          # last release, e.g. v0.3.0
git log --oneline --first-parent <last-tag>..origin/main
```

Read each merged PR (`gh pr view <n>`), not just the commit titles. Release when main has any of:
- something a user would notice (a feature, a changed default, a visible fix)
- a security fix: pinned installs only get it from a new tag
- a database migration or a config change older images can't read

Changes only to `site/`, `docs/`, tests or CI don't need a release. Say so and stop.

## 2. Pick the version

Until 1.0, use semver this way:
- **Minor** (0.3.0 → 0.4.0): any new feature, or a config/database change that older images can't read.
- **Patch** (0.3.0 → 0.3.1): fixes only, and rolling back stays safe.

Tell the user the version and why before going on.

## 3. Bump PR

Branch from `origin/main` as `claude/release-X.Y.Z` and change the old version to the new one in:
- `newsnest/__init__.py`: `__version__`, the one the app reads
- `docs/install.md`: "pin a release such as `X.Y.Z`"
- `.env.example`: the comment above `NEWSNEST_IMAGE_TAG`
- `.github/ISSUE_TEMPLATE/bug_report.yml`: the version placeholder

Then run `git grep -n '<old version>'` to catch any new places it appears. Change only these version strings
in this PR. Commit as `Release X.Y.Z`, then open a PR with the same title. In the body, list what changed
since the last tag, with PR numbers, and end with: "After merging, tag the merge commit `vX.Y.Z` and publish
the GitHub Release to build the versioned images."

## 4. Release notes

Draft them in the scratchpad and show them to the user. Write for people running the app, not for developers,
and keep the format of earlier releases (`gh release view <last-tag>`):

```markdown
## New
- **Short headline.** One or two sentences on what the user can now do. (#14)

## Fixed
- What went wrong, from the user's side, and what happens now. (#19)

## Update
`docker compose pull && docker compose up -d`, or pin `NEWSNEST_IMAGE_TAG=X.Y.Z`.
```

Under **Update**, add anything that makes rolling back unsafe, such as a config file older images reject, a
migration, or a renamed env var. Say how to undo it, for example restoring `config.yaml.bak`. Leave out
`site/`-only changes and refactors.

## 5. Tag and publish (only after the user merges, and only when they say to)

Pushing a tag and publishing a release are public and can't be cleanly undone. Ask before doing either,
unless the user already said to go ahead after the merge.

```bash
gh pr view <n> --json state,mergeCommit -q '.state, .mergeCommit.oid'    # must be MERGED
git fetch origin
git show <merge-sha>:newsnest/__init__.py | grep __version__              # must be the new version
git tag -a vX.Y.Z <merge-sha> -m "NewsNest X.Y.Z"
git push origin vX.Y.Z
gh release create vX.Y.Z --verify-tag --title "NewsNest X.Y.Z" --notes-file <notes.md>
```

Tag the PR's merge commit, not whatever is on main by then.

## 6. Check the images

The tag starts `docker-publish.yml`. Find the run with
`gh run list --workflow docker-publish.yml -L 3`, then wait for it in the background
(`gh run watch <id> --exit-status`). It should pass Test, both platform builds, and "Merge manifests & tag",
and push `X.Y.Z`, `X.Y` and `X` to GHCR (and to Docker Hub when those secrets are set), but not `latest`, which
only main moves. Report the tags it pushed. You can find them in the run log under `DOCKER_METADATA_OUTPUT_JSON`.

## Never

- Never move, delete or re-push a published tag. If a release is wrong, fix it on main and release X.Y.Z+1.
- Never tag before the bump PR is merged, or tag a commit whose `__version__` doesn't match the tag.
- Never mix other changes into the bump PR.
