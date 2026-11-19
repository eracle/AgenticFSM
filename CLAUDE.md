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
  keep their description, kind, tags and handler. The description is required; it's the state's
  definition. `handler=` is the one handler that decides where an entity goes from the state
  (`States.handler`); a final state has none. `tags=` makes a state a queue: `progress_all(tag)` and `due(tag)` try only the
  entities in a state carrying the tag, so one worker does one kind of work (`Machine._tagged`;
  a tag no state carries raises `MachineError`). `agfsm due` and `progress_all` take `--tag`.
  `progress_all(limit=)` (`--limit`) stops after that many moves, oldest entity first; an
  entity that doesn't move or raises doesn't count. `due(entities=)` and
  `progress_all(entities=)` try only those ids, oldest first (`Machine._waiting`), for a
  caller whose own data says which entities could move.
- **Handlers** (`handlers.py`): a state has exactly one, and it has no name: the state identifies
  it. `Handler` is the abstract base, with `run(entity, targets)` and three attributes the machine
  reads: `reads_only` (`due` may run it), `decided_by` (who its moves are logged under,
  default `code`) and `effect` (run with the result before the move is written). A plain
  function of the entity is wrapped into a `FunctionHandler` by `as_handler` where the state is
  declared (`State.__init__`), so the machine only ever calls `handler.run`. `run` returns the
  next state (or a `Decision`), `None` for "not now", or **another `Handler` to hand over to**,
  which the machine then runs with the same targets (`Machine._try`); a `Decide` is one (see
  *Decide*). `@check` marks a function that only reads: it sets an attribute on the function,
  which `FunctionHandler` reads into `reads_only`, and refuses a `Handler` object.
- **Edges**: one list of `(from, to)` pairs; `from` may be a tuple. A state's handler may return
  only the states its state has an edge to (`Machine._targets`).

`Machine(states, edges, load=, backend=, name=)` (`machine.py`) checks the declaration and raises
`MachineError` naming the state or edge: one initial state, a handler on every state but a final
one and none on those, no edges out of a final state, no other state without one, every state
reachable. Then:

| Call | Does |
|---|---|
| `start(id)` | puts an entity in the initial state; `AlreadyStarted` if it has one |
| `start_all(ids)` | starts each id with no state yet (one `all_current` read, then a `start` each; a rival's `AlreadyStarted` is skipped), for entities another program makes |
| `fire(id)` | runs the handler of the entity's state, and each handler it hands over to; returns the logged `Move`, or `None` for "not now" |
| `fire(id, entity=)` | the same, on the entity already in hand instead of `load(id)` (`brief` takes it too) |
| `fire(id, answer=)` | decides in a `Decide`'s place; no model call, the prompt doesn't run, its `effect` does |
| `brief(id)` | the `Brief` a `Decide` would give the model: state, instructions, prompt, choices, schema, tools |
| `progress(id)` | moves one entity until it rests: each round runs its state's handler on the entity loaded afresh, and commits its move; it stops when the handler doesn't move it, or after a move back to a state this call already left |
| `progress_all()` | per unfinished entity, runs its state's handler: at most one move per entity per call, each entity locked and committed on its own. An entity that raises is recorded and the rest tried; then `ProgressError(moves, failures)` |
| `due()` | the unfinished entities their state's handler could move now, writing nothing and taking no lock: a handler that only reads runs (and so does one it hands over to, if that only reads too); any other handler makes the entity due untried (`_would_move`) |
| `state_of(id)`, `in_state(state)`, `history(id)` | reads |
| `graph()` | states (each with its handler's `label`) and edges as data |
| `on_move(listener)` | adds a listener, called with the `Move` after every move and start; returns it, so it decorates |

**A listener is told once the move is committed.** `start` and `_apply` hand the move to
`_announce`, which gives the backend's `on_commit` a callback: outside a lock it runs at once,
inside one when the outermost lock's writes commit, and never if they are undone (a savepoint
that rolls back drops its own). `_tell` calls the listeners in the order added; one that raises
is logged on `agentic_fsm.machine` and nothing is raised, since the move happened. A move
written straight through a backend (`backend.move`) tells no one. `tests/test_on_move.py` runs
the Django backend with `transaction=True`: in pytest-django's usual test transaction nothing
commits.

**Every row in the log is a move that happened.** `fire` raises `TransitionError` and writes
nothing when the move can't be made: an entity not started or in a final state, a result outside
the state's edges or not a state at all (a bug in the handler), an answer that doesn't validate or
that the state's code leaves no room for. A log row names no handler: its `from_state` says
which handler ran (a start has none) and `decided_by` who decided. Entity ids are stored as text,
so `load` receives a string.

**An answer is taken at the end.** `fire(answer=)` and `brief` find the `Decide` with
`Machine._decide_for`: the state's handler if it is one, else the handler is run and must hand over
to one. It is run only if it only reads (`@check`), since it runs again on every brief and
answer; one that doesn't raises `TransitionError` untried, and so does one that returns a state
("moves it itself") or `None` ("not now"). An answer never overrides what the code settles.

**The entity is locked from read to write.** `fire` (and each round of `progress` and
`progress_all`) runs inside the backend's `lock(machine, entity)`: it reads the current state,
runs the handler and writes the move there, so a second caller waits and reads the state the first
left. A handler that raises undoes the move and, on the Django backend, the handler's own writes.

## Decide

`decide.py` holds `Decision` (a Pydantic model whose one field is `to`), `Decide(prompt=,
agent= | schema=, effect=)`, a `Handler`, and `Brief`. The `Agent` is Pydantic AI's and
is the whole model-side definition; without one, `Decide` builds an agent for the `DECIDE_MODEL`
setting with the schema as its output type, with the model check deferred so no key is needed
until it runs. A `Decide` is a state's handler (`handler=Decide(...)`) or what a state's function
returns to hand over. A schema leaves `to` typed as `States`: a `Decide` given in a class body
can't name the class being built, and `to` is narrowed when it runs.

- **Run**: `Decide.run(entity, targets)` calls the prompt function (falsy: "not now", no call),
  then `ask(prompt, entity, <narrowed schema>)`, which is `agent.run_sync(prompt, deps=entity,
  output_type=...)`; a subclass that calls its model another way overrides `ask` and
  `decided_by`. The narrowed schema
  is a copy whose `to` is a `Literal` of the state's targets, with each state's description in
  the field's description. The machine checks the result against the state's edges afterwards
  either way. A model that keeps answering outside them raises Pydantic AI's
  `UnexpectedModelBehavior`, and nothing is logged. A `Decide` a function hands over to is
  offered every target of the state, those only the function's facts lead to included: the
  states' descriptions are what tell the model which is which.
- **Answer**: `Decide.parse(answer, targets)` validates a dict, a JSON object or a bare state value
  against the same narrowed schema. The machine raises `TransitionError` on an invalid one.
- **Effect**: `Decide(effect=)` is a function of the entity and the decision. `Machine._apply`
  runs the deciding handler's `effect` after the target is checked against the edges and before
  `backend.move`, for a run and for an answer alike (`_answer` loads the entity only when the
  handler has one). One that raises leaves nothing written.
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

- `backends/base/base.py`: `BaseStateBackend` (eight calls: `start`, `lock`, `move`,
  `on_commit`, `current`, `in_state`, `all_current`, `history`), plus the `Move` and `Current`
  dataclasses.
- `backends/locmem/`: in memory, for tests; `lock` is a per-entity `RLock`. Nothing is undone
  there, so `on_commit` callbacks run when the thread's outermost lock ends, raised or not.
- `backends/django/`: a Django app (`INSTALLED_APPS = ["agentic_fsm.backends.django"]`, label
  `agentic_fsm`) with two models, `LogEntry` (append-only) and `CurrentState` (one row per
  entity), and their migrations. `lock` is `transaction.atomic()` around a `select_for_update`
  of the entity's current-state row; SQLite ignores the row lock. `on_commit` is
  `transaction.on_commit`. It's the only place Django is imported.
- `backends/sqlalchemy/`: the same two tables as SQLAlchemy `Table`s (`tables.py`; names,
  columns, indexes and constraint match the Django models, and the two store the same values:
  SQL NULL for a move with no decision, and on SQLite the same datetime text;
  `test_backend_sqlalchemy.py` checks both), so the two backends share one database. `StateBackend(engine=None)` takes an
  engine, a function returning one, or nothing for one cached engine per `SQLALCHEMY_URL`; the
  last two resolve on first use (`engine` is a `cached_property`), so a module-level machine
  imports with no database configured. `create_tables()` makes what's missing. `lock` is
  `engine.begin()` around a `SELECT ... FOR UPDATE`, and the open connection sits in a
  `ContextVar` keyed by engine: `connection()` hands it to a handler so its writes commit with the
  move, a lock inside it takes a savepoint, and reads inside it go through it. A second
  `ContextVar` holds the `on_commit` callbacks of the open lock, run after `engine.begin()`
  commits. `UTCDateTime`
  gives back aware UTC datetimes on SQLite too. It's the only place SQLAlchemy is imported.

Every backend keeps the same two things and the same rule: `move`, called inside `lock`,
rewrites the entity's current-state row and appends its log row, and both commit when the lock
is released.

`0003_no_step_name` dropped the log's `step` column, which named what is now a state's handler. The SQLAlchemy backend's `create_tables()`
only makes what's missing, so a database it made earlier keeps the column, nullable and unused,
until whoever owns it drops it. `0002_only_real_moves` deleted the refused log rows and dropped
`applied`, `reason` and `version`. An app whose migration writes those columns through the historical models declares `run_before = [("agentic_fsm", "0002_only_real_moves")]`.

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
