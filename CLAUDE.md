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
- **The core imports no database.** Anything a state backend needs (Django, a driver) is
  imported inside that backend's package and is an optional extra in `pyproject.toml`. Pydantic
  AI is the library's one dependency: a model deciding a move is part of the core, not an extra.
- **Every default is a setting.** A default value is declared in `conf/global_settings.py` and
  read as `settings.NAME`, never written where it's used and never read from `os.environ` there.

## Project Overview

`agentic_fsm` keeps entity lifecycles: many rows that each sit in a state for days and are moved
by whoever knows something new. A machine is three plain Python things.

- **States** (`states.py`): declared like Django model fields, `pending = InitialState("...")`.
  `_StatesMeta` replaces each declaration with its stored value before the enum is built, so the
  class is a real `str` enum (`Order.paid == "paid"`, usable as a Pydantic type) whose members
  keep their description and kind. The description is required; it's the state's definition.
- **Steps**: a named function of the entity that returns the next state (or a `Decision`), or
  `None` for "not now"; or an `Ask`, which puts the question to a model (see *Ask*).
- **Transitions**: one list of `(from, to, step)` rows; `from` may be a tuple. A step's allowed
  targets out of a state are the rows it sits on there.

`Machine(states, rows, load=, backend=, name=)` (`machine.py`) checks the declaration and raises
`MachineError` naming the state or row. Then:

| Call | Does |
|---|---|
| `start(id)` | puts an entity in the initial state; `AlreadyStarted` if it has one |
| `fire(id, step)` | runs one step (the function, the `Ask` or its name); returns the logged `Move`, or `None` for "not now" |
| `fire(id, step, answer=)` | decides an `Ask` in the model's place; no model call, the prompt doesn't run |
| `brief(id, step)` | the `Brief` an `Ask` would give the model: instructions, prompt, choices, schema, tools |
| `advance()` | per unfinished entity, tries the steps out of its state in list order and stops at the first that doesn't return `None`: at most one move per entity per call |
| `state_of(id)`, `in_state(state)`, `history(id)` | reads |
| `graph()` | states and rows as data |

A result outside the step's rows, or one that isn't a state, is logged as a refused `Move`
(`applied=False`, with a `reason`) and never applied. `fire` raises `TransitionError` when the
move can't even be tried (unknown step, entity not started, step doesn't leave the state). Entity
ids are stored as text, so `load` receives a string. The machine never opens a transaction around
a step: a step may send an email or call a model.

## Ask

`ask.py` holds `Decision` (a Pydantic model whose one field is `to`), `Ask(name=, prompt=,
agent= | schema=)` and `Brief`. The `Agent` is Pydantic AI's and is the whole model-side
definition; without one, `Ask` builds an agent for the `ASK_MODEL` setting with the schema as
its output type, with the model check deferred so no key is needed until it runs.

- **Run**: `Ask.run(entity, targets)` calls the prompt function (falsy: "not now", no call),
  then `agent.run_sync(prompt, deps=entity, output_type=<narrowed schema>)`. The narrowed schema
  is a copy whose `to` is a `Literal` of the step's targets out of the entity's state, with each
  state's description in the field's description. The machine checks the result against the rows
  afterwards either way. A model that keeps answering outside them raises Pydantic AI's
  `UnexpectedModelBehavior`, and nothing is logged.
- **Answer**: `Ask.parse(answer, targets)` validates a dict, a JSON object or a bare state value
  against the same narrowed schema. The machine logs an invalid one as a refused move.
- **Logged**: `decided_by` is `llm:<provider>:<model>` for a run, `agent` for an answer and
  `code` for a function, and `Move.decision` holds the whole `Decision`.
- `Brief.instructions` and `Brief.tools` read the agent's plain-text instructions (a private
  attribute of `Agent`) and its function tools. `tests/test_ask.py` fails if Pydantic AI moves
  either.

Tests use Pydantic AI's `TestModel`; `models.ALLOW_MODEL_REQUESTS = False` keeps any test from
reaching a real model.

## CLI

`cli.py` is the console script `agfsm` (`main(argv)` returns the exit code), on `argparse` and
nothing else. It adds no behaviour: each command loads the machine (`--machine
package.module:name`, or the `MACHINE` setting) and calls one `Machine` method, `show` →
`graph()`, `start`, `list` → `in_state`, `fire` → `fire` / `brief` (`--agent`) /
`fire(answer=)` (`--answer`), `advance`. Each command returns a `Result` (the data for `--json`,
the text, the exit code: 0 applied or "not now", 1 refused, 2 can't be tried).

- An entity is `<machine name>:<id>`, checked against the loaded machine; a state given to
  `list` is its stored value.
- `--machine` and `--json` go before or after the command: the subcommands' copies default to
  `argparse.SUPPRESS` so they don't erase one given before.
- The working directory goes on `sys.path`, as `python -m` would put it.
- With `DJANGO_SETTINGS_MODULE` set, `backends/django/project.py:setup_project` runs before the
  machine's module is imported, since that module usually imports the project's models.
- Pydantic AI's first-run banner goes to stderr, and never for a run an installed package
  starts, so `--json` output stays clean.

`tests/test_cli.py` drives `main(argv)` on both backends with machines it puts in
`tests/machines.py`, the module `--machine tests.machines:orders` names.

## Settings

`conf/global_settings.py` lists every setting with its default (`BACKEND`, `ASK_MODEL`,
`MACHINE`), and
`conf/__init__.py` holds `settings`, as Django's `django.conf` does. A read takes the first of:
`settings.configure(...)`, the env var `AGENTIC_FSM_<NAME>`, the module named by
`AGENTIC_FSM_SETTINGS_MODULE`, the default. Nothing is cached, so import order doesn't matter. A
name `global_settings` doesn't declare raises.

## State backends

Laid out and chosen as Django lays out its database backends: one package per backend, each with
a `base` module holding a `StateBackend`, picked by the package's dotted path (the `BACKEND`
setting, e.g. `AGENTIC_FSM_BACKEND=agentic_fsm.backends.django`, or `Machine(..., backend=)`; a
machine with neither raises). `backends/__init__.py:get_backend` imports `<path>.base` on first use.

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
