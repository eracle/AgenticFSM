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
  `None` for "not now"; or a `Decide`, which puts the question to a model (see *Decide*).
  `@check` (`machine.py`) marks a function step that only reads, so `due` may run it; it sets
  an attribute on the function and refuses a `Decide`.
- **Transitions**: one list of `(from, to, step)` rows; `from` may be a tuple. A step's allowed
  targets out of a state are the rows it sits on there.

`Machine(states, rows, load=, backend=, name=)` (`machine.py`) checks the declaration and raises
`MachineError` naming the state or row. Then:

| Call | Does |
|---|---|
| `start(id)` | puts an entity in the initial state; `AlreadyStarted` if it has one |
| `start_all(ids)` | starts each id with no state yet (one `all_current` read, then a `start` each; a rival's `AlreadyStarted` is skipped), for entities another program makes |
| `fire(id, step)` | runs one step (the function, the `Decide` or its name); returns the logged `Move`, or `None` for "not now" |
| `fire(id, step, entity=)` | the same, on the entity already in hand instead of `load(id)` (`brief` takes it too) |
| `fire(id, step, answer=)` | decides a `Decide` in the model's place; no model call, the prompt doesn't run |
| `brief(id, step)` | the `Brief` a `Decide` would give the model: instructions, prompt, choices, schema, tools |
| `progress(id)` | moves one entity until it rests: each round tries the steps out of its state in list order on the entity loaded afresh, and commits its move; it stops when no step moves it, or after a move back to a state this call already left |
| `progress_all()` | per unfinished entity, tries the steps out of its state in list order and stops at the first that doesn't return `None`: at most one move per entity per call, each entity locked and committed on its own. An entity that raises is recorded and the rest tried; then `ProgressError(moves, failures)` |
| `due()` | the unfinished entities a step could move now, writing nothing: under each entity's lock, steps in list order, a `@check` step runs, any other step makes the entity due untried |
| `state_of(id)`, `in_state(state)`, `history(id)` | reads |
| `graph()` | states and rows as data |

**Every row in the log is a move that happened.** `fire` raises `TransitionError` and writes
nothing when the move can't be made: an unknown step, an entity not started, a step that doesn't
leave the state, a result outside the step's rows or not a state at all (a bug in the step), an
answer that doesn't validate. Entity ids are stored as text, so `load` receives a string.

**The entity is locked from read to write.** `fire` (and each round of `progress` and
`progress_all`) runs inside the backend's `lock(machine, entity)`: it reads the current state,
runs the step and writes the move there, so a second caller waits and reads the state the first
left. A step that raises undoes the move and, on the Django backend, the step's own writes.

## Decide

`decide.py` holds `Decision` (a Pydantic model whose one field is `to`), `Decide(name=, prompt=,
agent= | schema=)` and `Brief`. The `Agent` is Pydantic AI's and is the whole model-side
definition; without one, `Decide` builds an agent for the `DECIDE_MODEL` setting with the schema as
its output type, with the model check deferred so no key is needed until it runs.

- **Run**: `Decide.run(entity, targets)` calls the prompt function (falsy: "not now", no call),
  then `agent.run_sync(prompt, deps=entity, output_type=<narrowed schema>)`. The narrowed schema
  is a copy whose `to` is a `Literal` of the step's targets out of the entity's state, with each
  state's description in the field's description. The machine checks the result against the rows
  afterwards either way. A model that keeps answering outside them raises Pydantic AI's
  `UnexpectedModelBehavior`, and nothing is logged.
- **Answer**: `Decide.parse(answer, targets)` validates a dict, a JSON object or a bare state value
  against the same narrowed schema. The machine raises `TransitionError` on an invalid one.
- **Logged**: `decided_by` is `llm:<provider>:<model>` for a run, `agent` for an answer and
  `code` for a function, and `Move.decision` holds the whole `Decision`.
- `Brief.instructions` and `Brief.tools` read the agent's plain-text instructions (a private
  attribute of `Agent`) and its function tools. `tests/test_decide.py` fails if Pydantic AI moves
  either.

Tests use Pydantic AI's `TestModel`; `models.ALLOW_MODEL_REQUESTS = False` keeps any test from
reaching a real model.

## CLI

`cli.py` is the console script `agfsm` (`main(argv)` returns the exit code), on `argparse` and
nothing else. It adds no behaviour: each command loads the machine (`--machine
package.module:name`, or the `MACHINE` setting) and calls one `Machine` method, `show` →
`graph()`, `start`, `list` → `in_state`, `due`, `fire` → `fire` / `brief` (`--agent`) /
`fire(answer=)` (`--answer`), `progress`, `progress_all` (on `ProgressError`: the moves on
stdout, each failure on stderr, exit 2). Each command returns a `Result` (the
data for `--json`, the text, the exit code: 0 a move made or "not now", 2 one that can't be made,
with the reason on stderr).

- An entity is `<machine name>:<id>`, checked against the loaded machine; a state given to
  `list` is its stored value.
- `--machine` and `--json` go before or after the command: the subcommands' copies default to
  `argparse.SUPPRESS` so they don't erase one given before.
- The working directory goes on `sys.path`, as `python -m` would put it.
- With `DJANGO_SETTINGS_MODULE` set, and Django and that settings module both installed
  (`find_spec`), `backends/django/project.py:setup_project` runs before the machine's module is
  imported, since that module usually imports the project's models. A variable set for another
  program in a shared environment is left alone.
- Pydantic AI's first-run banner goes to stderr, and never for a run an installed package
  starts, so `--json` output stays clean.

`tests/test_cli.py` drives `main(argv)` on both backends with machines it puts in
`tests/machines.py`, the module `--machine tests.machines:orders` names.

## Settings

`conf/global_settings.py` lists every setting with its default (`BACKEND`, `DECIDE_MODEL`,
`MACHINE`, `SQLALCHEMY_URL`), and
`conf/__init__.py` holds `settings`, as Django's `django.conf` does. A read takes the first of:
`settings.configure(...)`, the env var `AGENTIC_FSM_<NAME>`, the module named by
`AGENTIC_FSM_SETTINGS_MODULE`, the default. Nothing is cached, so import order doesn't matter. A
name `global_settings` doesn't declare raises.

## State backends

Laid out and chosen as Django lays out its database backends: one package per backend, each with
a `base` module holding a `StateBackend`, picked by the package's dotted path (the `BACKEND`
setting, e.g. `AGENTIC_FSM_BACKEND=agentic_fsm.backends.django`, or `Machine(..., backend=)`; a
machine with neither raises). `backends/__init__.py:get_backend` imports `<path>.base` on first use.

- `backends/base/base.py`: `BaseStateBackend` (seven calls: `start`, `lock`, `move`, `current`,
  `in_state`, `all_current`, `history`), plus the `Move` and `Current` dataclasses.
- `backends/locmem/`: in memory, for tests; `lock` is a per-entity `RLock`.
- `backends/django/`: a Django app (`INSTALLED_APPS = ["agentic_fsm.backends.django"]`, label
  `agentic_fsm`) with two models, `LogEntry` (append-only) and `CurrentState` (one row per
  entity), and their migrations. `lock` is `transaction.atomic()` around a `select_for_update`
  of the entity's current-state row; SQLite ignores the row lock. It's the only place Django is
  imported.
- `backends/sqlalchemy/`: the same two tables as SQLAlchemy `Table`s (`tables.py`; names,
  columns, indexes and constraint match the Django models, and `test_backend_sqlalchemy.py`
  checks it), so the two backends share one database. `StateBackend(engine=None)` takes an
  engine, a function returning one, or nothing for one cached engine per `SQLALCHEMY_URL`; the
  last two resolve on first use (`engine` is a `cached_property`), so a module-level machine
  imports with no database configured. `create_tables()` makes what's missing. `lock` is
  `engine.begin()` around a `SELECT ... FOR UPDATE`, and the open connection sits in a
  `ContextVar` keyed by engine: `connection()` hands it to a step so its writes commit with the
  move, a lock inside it takes a savepoint, and reads inside it go through it. `UTCDateTime`
  gives back aware UTC datetimes on SQLite too. It's the only place SQLAlchemy is imported.

Every backend keeps the same two things and the same rule: `move`, called inside `lock`,
rewrites the entity's current-state row and appends its log row, and both commit when the lock
is released.

`0002_only_real_moves` deleted the refused log rows and dropped `applied`, `reason` and
`version`. An app whose migration writes those columns through the historical models declares `run_before = [("agentic_fsm", "0002_only_real_moves")]`.

## Tests

```bash
uv venv && uv pip install -e ".[dev]"
pytest
```

`tests/conftest.py` holds the example `Order` machine. Its `backend` fixture is parametrized, so
every test that takes `orders` or `backend` runs on `locmem`, on `django` (pytest-django,
in-memory SQLite, `tests/settings.py`) and on `sqlalchemy` (in-memory SQLite, `StaticPool`).
With `AGENTIC_FSM_TEST_POSTGRES_URL` set, the SQLAlchemy row lock is also tested on Postgres. A new backend is added to that fixture
and must pass the same tests. After changing a model, write the migration and check
`DJANGO_SETTINGS_MODULE=tests.settings python -m django makemigrations --check --dry-run`.
