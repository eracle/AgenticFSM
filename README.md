# AgenticFSM

Entity lifecycles declared once and advanced the same way by code, a model or an agent.

An order, a ticket or an application sits in a state for days and is moved along by whatever
knows something new about it. AgenticFSM keeps the allowed moves in one list, records every move
in an append-only log, and raises on a move the list doesn't have.

## Install

```bash
pip install agentic-fsm            # the library; brings Pydantic AI for steps a model decides
pip install "agentic-fsm[django]"  # with the Django state backend
pip install "agentic-fsm[sqlalchemy]"  # with the SQLAlchemy state backend; add your database's driver
```

## A machine

States are declared like Django model fields. Each one carries its definition.

```python
from agentic_fsm import States, State, InitialState, FinalState

class Order(States):
    pending   = InitialState("Placed, not paid yet")
    paid      = State("Payment cleared, waiting to ship")
    shipped   = State("Handed to the carrier")
    delivered = FinalState("The carrier delivered it")
    cancelled = FinalState("Cancelled before it shipped")
```

A step is a function of the entity. It returns the next state, or `None` for "not now".

```python
def cancel(order):
    return Order.cancelled if order.cancel_requested else None

def payment_cleared(order):
    return Order.paid if order.payment.cleared else None

def ship(order):
    return Order.shipped if order.tracking_number else None

def carrier_delivered(order):
    return Order.delivered if order.carrier_status == "delivered" else None
```

One list holds the whole graph, one row per edge.

```python
from agentic_fsm import Machine

TRANSITIONS = [
    # from                         to                step
    ((Order.pending, Order.paid),  Order.cancelled,  cancel),
    (Order.pending,                Order.paid,       payment_cleared),
    (Order.paid,                   Order.shipped,    ship),
    (Order.shipped,                Order.delivered,  carrier_delivered),
]

orders = Machine(Order, TRANSITIONS, load=get_order)
```

`load(id)` fetches the entity from your own tables. The library stores the state, never the
entity. Ids are stored as text, so `load` receives a string.

`Machine(...)` checks the declaration and raises a `MachineError` naming the state or the row:
exactly one `InitialState`, every state reachable from it, no rows leaving a `FinalState`, no
other state without a way out, every row's states belonging to the machine, every step a named
function or a `Decide`, every `Decide` deciding among this machine's states, no two steps sharing a
name.

## A step a model decides

A `Decide` is a step that puts the question to a model. It has a name, a prompt function and a
schema: a `Decision` whose `to` is typed as your states, plus any fields you want logged with
the move.

```python
from agentic_fsm import Decide, Decision

class MessageRead(Decision):
    to: Order
    reason: str

read_message = Decide(
    name="read_message",
    prompt=lambda order: order.message and f"Where does this message put the order?\n{order.message}",
    schema=MessageRead,
)

TRANSITIONS = [
    ...
    (Order.pending,  Order.cancelled,  read_message),
    (Order.paid,     Order.cancelled,  read_message),
]
```

- **The model is offered only the step's rows** out of the order's state, each with its
  description, so the prompt never has to explain what a state means.
- **A prompt that returns nothing means "not now"**: no call is made and the order stays.
- **The whole `Decision` is logged** with the move, and the move's `decided_by` is
  `llm:<model>`.

For tools, another model, retries or output validators, declare a
[Pydantic AI](https://ai.pydantic.dev) `Agent` and pass it as `agent=` in place of `schema=`.
Its output type is the schema, and the entity is passed as `deps`, so each tool reads it from
`ctx.deps`.

```python
from pydantic_ai import Agent

reader = Agent(
    "anthropic:claude-haiku-5-5",
    deps_type=OrderRow,
    output_type=MessageRead,
    instructions="You read messages from a shop's customers.",
    tools=[past_orders],
)

read_message = Decide(name="read_message", agent=reader, prompt=...)
```

A function step may also return a `Decision` in place of a state, for code that calls
`read_message.run(order)` itself or decides by rule and wants its reason logged.

### A session in the model's place

A coding agent or a person can decide a `Decide` step with no model key set.

```python
brief = orders.brief(42, "read_message")
# brief.instructions, brief.prompt, brief.choices, brief.schema, brief.tools

orders.fire(42, "read_message", answer={"to": "cancelled", "reason": "They asked for a refund"})
```

`answer=` takes the fields as a dict or a JSON object, or a bare state value when the schema is
a plain `Decision`. The prompt function doesn't run again, and the move's `decided_by` is
`agent`. An answer the schema or the step's rows don't allow raises `TransitionError` with the
reason; nothing is written, and the session answers again.

## Moving entities

| Call | Does |
|---|---|
| `orders.start(42)` | puts an entity in the initial state |
| `orders.fire(42, payment_cleared)` | runs one step on one entity |
| `orders.fire(42, payment_cleared, entity=order)` | the same, on the order already in hand instead of `load(42)` |
| `orders.fire(42, "read_message", answer=...)` | decides a `Decide` step in the model's place |
| `orders.brief(42, "read_message")` | what a `Decide` step would give the model |
| `orders.progress(42)` | moves one entity until it rests |
| `orders.progress_all()` | tries the steps out of each unfinished entity's state: at most one move each |
| `orders.state_of(42)` | the entity's current state |
| `orders.in_state(Order.paid)` | the ids of the entities in a state |
| `orders.history(42)` | every move logged for the entity |
| `orders.graph()` | the states and rows as data, for review |

- **Every row in the log is a move that happened.** A step's targets are the rows it sits on:
  if `payment_cleared` returns anything but `Order.paid`, that's a bug, `fire` raises
  `TransitionError`, and nothing is written. Whatever a step can meet (a bounce, a failed
  payment) is a state with its own row.
- **`fire` returns the logged `Move`**, or `None` when the step said "not now".
- **`progress_all` tries steps in list order** and stops at the first that doesn't return
  `None`, so each entity makes at most one move per call. When to call it is up to you: a cron
  job, a scheduler, a script.
- **`progress` goes on until the entity rests**: a final state, or one whose steps all say "not
  now". It loads the entity afresh after each move, commits each move, and stops after a move
  that brings the entity back to a state it already left, so it never loops.
- **Two callers can't both move an entity.** `fire` locks the entity from the read of its state
  to the write of the move; a second caller waits, then reads the state the first left. With
  the Django and SQLAlchemy backends the step's own writes commit with the move.
- **A step with an outside effect should be safe to run twice.** If it sends an email and the
  process dies before the move commits, the next call runs it again.

## At the command line

`agfsm` makes the same moves from a shell, so a coding agent or a script can do every move your
code can and no other. Each command calls one of the methods above.

| Command | Calls |
|---|---|
| `agfsm show [--mermaid]` | `orders.graph()`: the states with their descriptions, then the rows |
| `agfsm start order:42` | `orders.start(42)` |
| `agfsm list paid` | `orders.in_state(Order.paid)` |
| `agfsm fire order:42 payment_cleared` | `orders.fire(42, "payment_cleared")` |
| `agfsm fire order:42 read_message --agent` | `orders.brief(42, "read_message")` |
| `agfsm fire order:42 read_message --answer '{...}'` | `orders.fire(42, "read_message", answer=...)` |
| `agfsm progress order:42` | `orders.progress(42)` |
| `agfsm progress_all` | `orders.progress_all()` |

- **The machine** is named as `--machine package.module:name` (a module in the working directory
  works), or by the `MACHINE` setting: `export AGENTIC_FSM_MACHINE=shop.lifecycle:orders`.
- **An entity** is `<machine name>:<id>`, and the name must be the loaded machine's.
- **`--json`** prints the result as data: the `Move`, `Brief` or `Graph`, with times as ISO text.
- **The exit code** is 0 for a move made or "not now", 2 for one that can't be made (an unknown
  step, an entity not started, an answer that doesn't validate, no machine), with the reason on
  stderr.
- **With `DJANGO_SETTINGS_MODULE` set**, `agfsm` sets the Django project up before it loads the
  machine, as `manage.py` would.

`--agent` then `--answer` lets a session decide a `Decide` step with no model key: the first prints the
instructions, the prompt, the choices and the answer's schema; the second applies the answer.

## State backends

Where state is kept is a backend. The core imports no database; each backend is its own package,
chosen by its dotted path, the way Django chooses a database engine.

```bash
export AGENTIC_FSM_BACKEND=agentic_fsm.backends.django
```

or `Machine(..., backend="agentic_fsm.backends.django")`. A machine with neither raises.

| Backend | Keeps state in | Needs |
|---|---|---|
| `agentic_fsm.backends.django` | a Django project's database | `agentic-fsm[django]` |
| `agentic_fsm.backends.sqlalchemy` | any database SQLAlchemy reaches | `agentic-fsm[sqlalchemy]` and a driver |
| `agentic_fsm.backends.locmem` | this process's memory, for tests | nothing |

For the Django backend, add its app and migrate:

```python
INSTALLED_APPS = [..., "agentic_fsm.backends.django"]
```

It writes through Django's own connection, so wrapping `fire` in `transaction.atomic()` commits
your rows and the move together.

The SQLAlchemy backend connects to the `SQLALCHEMY_URL` setting, or takes an engine:

```python
from agentic_fsm.backends.sqlalchemy.base import StateBackend

backend = StateBackend(engine)  # or built from AGENTIC_FSM_SQLALCHEMY_URL
backend.create_tables()         # where they don't exist yet
orders = Machine(Order, TRANSITIONS, load=load_order, backend=backend)


def ship(order):
    backend.connection().execute(insert(shipments).values(order_id=order.id))
    return Order.shipped
```

`backend.connection()`, called while a step runs, is the connection of the move's transaction:
what the step writes through it commits with the move and is undone if the step raises. Its two
tables are the Django backend's, so the two backends can keep machines in one database and read
each other's.

A backend of your own is a package whose `base` module has a `StateBackend` subclassing
`agentic_fsm.BaseStateBackend`.

## Settings

Every default is in `agentic_fsm/conf/global_settings.py`, the way Django keeps its own.

| Setting | Default | Is |
|---|---|---|
| `BACKEND` | `None` | the state backend of a machine built without `backend=` |
| `DECIDE_MODEL` | `"anthropic:claude-haiku-5-5"` | the model of a `Decide` built without `agent=` |
| `MACHINE` | `None` | the machine `agfsm` loads without `--machine`, as `package.module:name` |
| `SQLALCHEMY_URL` | `None` | the database of a SQLAlchemy backend built without an engine |

A setting takes the first value found in `settings.configure(BACKEND=...)`, the environment
variable `AGENTIC_FSM_<NAME>`, the module named by `AGENTIC_FSM_SETTINGS_MODULE`, then the
default.

```python
from agentic_fsm import settings

settings.configure(BACKEND="agentic_fsm.backends.django")
```

## Development

```bash
uv venv && uv pip install -e ".[dev]"
pytest
```

Set `AGENTIC_FSM_TEST_POSTGRES_URL` (e.g. `postgresql+psycopg://postgres:pg@localhost:5432/postgres`)
to also test the SQLAlchemy backend's row lock on Postgres; the test drops and recreates the two
tables there.

## Licence

Apache-2.0. See `LICENCE.md`.
