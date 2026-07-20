# CLAUDE.md

Guidance for Claude Code in this repo.

## Project Overview

`wb-vout-watchdog` is a Python MQTT service that supervises input power (Vin) on Wiren
Board controllers and gates the switched Vout line accordingly. On a dangerous
undervoltage it cuts Vout and requires a fresh external MQTT confirmation before
re-enabling it; it stays out of the way during normal operation and during
WBMZ-BATTERY-backed operation.

## Environment Setup

Use Python 3.13.5 — matches the target platform (Debian 13 / trixie).

```bash
./scripts/bootstrap-venv.sh
```

The script builds a self-contained `.venv` with Python 3.13.5 bundled inside
(`.venv/bundle/`), so the same `.venv` works both on the host and inside the
agent-vm (which bind-mount the project at the same absolute path but have
different `$HOME`). It is idempotent — safe to rerun to sync dependencies. The
venv is created with `--system-site-packages` so `gpio.py` can see `gpiod`,
which comes from the apt package `python3-libgpiod` (`sudo apt install
python3-libgpiod`) — there is no pip-installable equivalent.

Always use tools from `.venv/bin/...`.

## Mandatory Verification Pipeline (after any code change)

```bash
.venv/bin/isort --settings-file pyproject.toml .
.venv/bin/black --config pyproject.toml .
.venv/bin/pylint --rcfile pyproject.toml <package dir> tests
.venv/bin/pytest
```

(`pyproject.toml` baseline and the `../codestyle` checkout it comes from — see
@project-rules.md. Coverage enforcement is CI-level, via the Jenkinsfile's
`defaultCoverageMin`, not a local `pytest` flag.)

## Project Rules & Code Style

The agent workflow rules (commits, tests, renames, temp vars, private-attribute access,
pylint scoping, …) and the code style (enums over constants, structures over dict soup,
class method ordering) live in @project-rules.md — the single source of truth, imported
here so it loads in every session. Edit those rules there, not in this file.

## Task Workflow

Non-trivial changes follow plan → implement → review:

- **Plan** — `docs/<topic>_plan.md`. Written before implementation; intended approach and scope.
- **Review** — run after implementation, against @project-rules.md and the matching plan.

`<topic>` is a short snake_case slug. Agents that take a plan as input read the matching
`docs/<topic>_plan.md` first.

