# CLAUDE.md

The global `~/.claude/CLAUDE.md` (development workflow, branching, pull requests, issue tracking, documentation conventions) applies here as well. This file holds only what is specific to this project, and wins where the two disagree.

## Commands

- `task check` runs every check: ruff format and lint, `ty check`, and `pytest`. Run it before each commit. `task --list` shows the rest.
- Git hooks come from `.lefthook.yml` (`task setup` installs them; lefthook itself is on the system, not a project dependency). Pre-commit checks the staged files, pre-push runs `task check`.

## CI

GitHub Actions (`.github/workflows/ci.yml`) runs `task check` on pull requests and pushes to `main`, skipping changes to Markdown and `docs/` only. It checks the branch head at each push.
