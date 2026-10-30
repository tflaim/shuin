# Changelog

All notable changes to this project are recorded in this file. The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/).

## 0.1.0 - 2026-10-30

Initial release as Shuin, formerly Hanko.

### Added

- `install.sh`, which installs Shuin into its own virtualenv and links `shuin` into `~/.local/bin`.
- `SECURITY.md`, `CONTRIBUTING.md`, and this changelog.
- A "Known limits and threat model" section in the README.

### Changed

- The brand mark on the board and in the CLI changed from 判 to 朱, and the kicker from ハンコ to 朱印.
- The README is rewritten and shorter.
- `.gitignore` now also lists `owner.token`, `tokens.json`, `jobs/`, and `evidence/`.
- Global options are only removed before `--`, preserving all arguments passed to a job command.

### Migrating from Hanko

- The command `hanko` is now `shuin`, and `python -m hanko` is now `python -m shuin`.
- The data directory `~/.hanko` is now `~/.shuin`. Shuin does not move it for you. Stop any running `hanko serve` and `hanko job run` processes, then run `[ -e ~/.shuin ] && echo '~/.shuin already exists' || mv ~/.hanko ~/.shuin`. The database format did not change.
- The environment variables `HANKO_*` are now `SHUIN_*`: `HANKO_DB`, `HANKO_HOME`, `HANKO_TOKEN`, `HANKO_ALLOW_LOCALHOST`, and the `HANKO_JOB_*` test hooks. The old names are not read. `LEDGER_TOKEN` is unchanged.
- The MCP server now reports its name as `shuin` (it was `hanko`). In your MCP client configuration, change the command to `shuin` and the token variable to `SHUIN_TOKEN`.
- The webhook `User-Agent` header is now `shuin/0.1` (it was `hanko/0.1`).
- Job worktrees are created at `<repository parent>/.shuin-worktrees/<repository>/<job id>` (the directory was `.hanko-worktrees`). Existing worktrees under the old directory are left in place.
- Commands started by `shuin job run` receive `SHUIN_TOKEN`, `SHUIN_DB`, and `SHUIN_JOB` in their environment (the old names were `HANKO_*`). Update any wrapper script that reads them.
- Python code that imports the package must change `import hanko` to `import shuin`.
