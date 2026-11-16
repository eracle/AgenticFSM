# CLAUDE.md

## Rules

- **Commits**: No `Co-Authored-By` lines. Single-line messages (no body). Same convention as
  OpenOutFind, OpenOutSend and OpenOutLearn.
- **Always work on `main`.** Never create or switch to a feature branch.
- **Every green push to `main` is a release.** `.github/workflows/deploy.yml` runs the tests, then
  derives the version from the commit count and publishes to PyPI via trusted publishing
  (environment `pypi`). `needs: test` is the whole gate.
- **No memory**: never use the auto-memory system.
- **No roadmap here.** Work is tracked in `openoutreach-docs/roadmap/AgenticFSM/`. The design,
  the milestones and what was weighed and not taken are in
  `p1-e3-agentic-fsm-first-version.md` there. Read it before changing the interface.
- **This is a general-purpose library.** Nothing from OpenOutreach's domain (deals, leads,
  batches) goes in the code, the docstrings, the README or the tests. The example everywhere is
  a shop `Order`. A machine for a real domain lives in the repo that uses the library, as models
  live in a Django project and not in Django.
- **The core imports no database and no framework.** `states.py` and `machine.py` use the
  standard library only. Anything a backend needs is imported inside that backend's package and
  is an optional extra in `pyproject.toml`.

## Project Overview

`agentic_fsm` keeps entity lifecycles: many rows that each sit in a state for days and are moved
by whoever knows something new. A machine is three plain Python things.

- **States** (`states.py`): declared like Django model fields, `pending = InitialState("...")`.
  `_StatesMeta` replaces each declaration with its stored value before the enum is built, so the
  class is a real `str` enum (`Order.paid == "paid"`, usable as a Pydantic type) whose members
  keep their description and kind. The description is required; it's the state's definition.
- **Steps**: a named function of the entity that returns the next state, or `None` for "not now".
- **Transitions**: one list of `(from, to, step)` rows; `from` may be a tuple. A step's allowed
  targets out of a state are the rows it sits on there.

`Machine(states, rows, load=, backend=, name=)` (`machine.py`) checks the declaration and raises
`MachineError` naming the state or row. Then:

| Call | Does |
|---|---|
| `start(id)` | puts an entity in the initial state; `AlreadyStarted` if it has one |
| `fire(id, step)` | runs one step (the function or its name); returns the logged `Move`, or `None` for "not now" |
| `advance()` | per unfinished entity, tries the steps out of its state in list order and stops at the first that doesn't return `None`: at most one move per entity per call |
| `state_of(id)`, `in_state(state)`, `history(id)` | reads |
| `graph()` | states and rows as data |

A result outside the step's rows, or one that isn't a state, is logged as a refused `Move`
(`applied=False`, with a `reason`) and never applied. `fire` raises `TransitionError` when the
move can't even be tried (unknown step, entity not started, step doesn't leave the state). Entity
ids are stored as text, so `load` receives a string. The machine never opens a transaction around
a step: a step may send an email or call a model.

## State backends

Laid out and chosen as Django lays out its database backends: one package per backend, each with
a `base` module holding a `StateBackend`, picked by the package's dotted path
(`AGENTIC_FSM_BACKEND=agentic_fsm.backends.django`, or `Machine(..., backend=)`; a machine with
neither raises). `backends/__init__.py:get_backend` imports `<path>.base` on first use.

- `backends/base/base.py`: `BaseStateBackend` (seven calls: `start`, `move`, `refuse`, `current`,
  `in_state`, `all_current`, `history`), plus the `Move` and `Current` dataclasses.
- `backends/locmem/`: in memory, for tests.
- `backends/django/`: a Django app (`INSTALLED_APPS = ["agentic_fsm.backends.django"]`, label
  `agentic_fsm`) with two models, `LogEntry` (append-only) and `CurrentState` (one row per
  entity, with a `version`), and their migration. It's the only place Django is imported.

Every backend keeps the same two things and the same rule: a move rewrites the entity's
current-state row and appends its log row in one transaction, and only if the entity's version is
still the one read before the step ran. A move that finds another version is logged as refused.

## Tests

```bash
uv venv && uv pip install -e ".[dev]"
pytest
```

`tests/conftest.py` holds the example `Order` machine. Its `backend` fixture is parametrized, so
every test that takes `orders` or `backend` runs once on `locmem` and once on `django`
(pytest-django, in-memory SQLite, `tests/settings.py`). A new backend is added to that fixture
and must pass the same tests. After changing a model, write the migration and check
`DJANGO_SETTINGS_MODULE=tests.settings python -m django makemigrations --check --dry-run`.
