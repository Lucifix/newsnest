# NewsNest

## Branch names

Name every branch `<type>/<short-name>`. The short name is 2–4 lowercase words joined by hyphens that say
what changes, for example `fix/feed-scroll-jump` or `feature/opml-import`.

| Type        | Use it when the branch…                                             |
|-------------|---------------------------------------------------------------------|
| `feature/`  | adds something a user can do or see                                 |
| `fix/`      | makes something broken work                                         |
| `docs/`     | changes only `README.md`, `docs/` or `site/` text                   |
| `refactor/` | changes code without changing what the app does                     |
| `chore/`    | changes CI, dependencies, tooling, config or Claude skills          |
| `release/`  | bumps the version: `release/X.Y.Z` (see the `release` skill)         |

When a branch mixes types, pick the one a user would notice most (`feature` > `fix` > the rest).

A session worktree starts on a branch the app named (`<prefix>/<session-name>`). Once you know the task's
type, and before the first push, rename it with `git branch -m <type>/<short-name>`.
