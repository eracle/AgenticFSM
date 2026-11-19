# AgenticFSM

State machines for things that take days: orders, tickets, applications. Each state says who
decides what happens next (your code, a model, or an agent standing in for the model), every
move is checked against one list of allowed moves, and every move is logged.

```bash
pip install agentic-fsm
```

## Why

An order is placed today, paid tomorrow, shipped next week. In between it sits in a row, and
the rule for moving it on is spread across a webhook, a cron job and an admin action, each
writing a `status` column. Nothing says which moves are allowed, and nothing records who made
the last one.

AgenticFSM puts that in one declaration:

- **Each state names its handler**: the one function that looks at the entity and says where it
  goes next, or "not now".
- **One list of edges** says which moves exist. A handler that returns anything else is a bug,
  and nothing is written.
- **Every move is a row in a log**: from, to, when, and who decided (`code`, `llm:<model>` or
  `agent`).
- **A model can be a handler.** It is offered only the states the edges allow, each with its
  description, and its answer is logged with the move. A coding agent or a person can answer in
  its place, with no model key.

The library keeps the state and the log. Your entity stays in your own tables.

## Quick start

A complete, runnable machine, kept in memory:

```python
from dataclasses import dataclass
from agentic_fsm import Machine, States, InitialState, State, FinalState

@dataclass
class OrderRow:
    paid: bool = False
    tracking_number: str | None = None

ORDERS = {"42": OrderRow()}            # your own table


# A handler reads the entity and returns the next state, or None for "not now".
def await_payment(order):
    return Order.paid if order.paid else None

def pack(order):
    return Order.shipped if order.tracking_number else None


# States are declared like Django model fields: a description, and the state's handler.
class Order(States):
    pending = InitialState("Placed, not paid yet", handler=await_payment)
    paid    = State("Payment cleared, waiting to ship", handler=pack)
    shipped = FinalState("Handed to the carrier")


orders = Machine(
    Order,
    [(Order.pending, Order.paid), (Order.paid, Order.shipped)],   # the allowed moves
    load=ORDERS.get,                                              # id -> your entity
    backend="agentic_fsm.backends.locmem",                        # where state is kept
)

orders.start(42)
orders.fire(42)                        # None: not paid, so it stays in pending

ORDERS["42"].paid = True
orders.fire(42)                        # Move(from_state='pending', to_state='paid', decided_by='code', ...)

ORDERS["42"].tracking_number = "1Z999"
orders.progress(42)                    # moves it until it rests
orders.state_of(42)                    # Order.shipped
orders.history(42)                     # start, pending -> paid, paid -> shipped
```

In production, swap `locmem` for the Django or SQLAlchemy backend (see
[State backends](#state-backends)) and call `orders.progress_all()` from a scheduler.

## The pieces

| Piece | What it is |
|---|---|
| **State** | A member of a `States` class. Its description is its definition, and it names its handler. Exactly one `InitialState`; a `FinalState` has no handler |
| **Handler** | A function of the entity (or a `Handler` object, such as a `Decide`). Returns the next state, `None` for "not now", or another handler to hand over to |
| **Edges** | A list of `(from, to)` pairs. A handler may only return the states its state has an edge to |
| **Machine** | `Machine(States, edges, load=, backend=)`. Checks the declaration, then starts, fires and progresses entities |
| **Move** | One log row: `from_state`, `to_state`, `decided_by`, `at`, and the model's `decision` if one decided |
| **Backend** | Where state and the log are kept: in memory, a Django project's database, or any database SQLAlchemy reaches |

`load(id)` fetches the entity from your own tables. Ids are stored as text, so `load` receives a
string.

## Declaring a machine

A handler can check several things, in the order its body checks them:

```python
def await_payment(order):
    if order.cancel_requested:
        return Order.cancelled
    return Order.paid if order.payment.cleared else None

def pack(order):
    if order.cancel_requested:
        return Order.cancelled
    return Order.shipped if order.tracking_number else None

def await_delivery(order):
    return Order.delivered if order.carrier_status == "delivered" else None


class Order(States):
    pending   = InitialState("Placed, not paid yet", handler=await_payment)
    paid      = State("Payment cleared, waiting to ship", handler=pack)
    shipped   = State("Handed to the carrier", handler=await_delivery)
    delivered = FinalState("The carrier delivered it")
    cancelled = FinalState("Cancelled before it shipped")


EDGES = [
    (Order.pending, Order.paid),
    (Order.pending, Order.cancelled),
    (Order.paid,    Order.shipped),
    (Order.paid,    Order.cancelled),
    (Order.shipped, Order.delivered),
]

orders = Machine(Order, EDGES, load=get_order)
```

An edge's `from` may be a tuple, as shorthand for one edge from each:
`((Order.pending, Order.paid), Order.cancelled)`.

`Order` is a real `str` enum (`Order.paid == "paid"`), so it can be used as a Pydantic type or a
database value.

`Machine(...)` refuses a declaration that can't work, with a `MachineError` naming the state or
edge: not exactly one `InitialState`, a state that can't be reached from it, a non-final state
with no handler or no way out, a `FinalState` with a handler or an edge leaving it, an edge to a
state of another machine.

## Letting a model decide

A `Decide` is a handler that puts the question to a model. It has a prompt function and a schema:
a `Decision`, which says where the entity goes (`to`), plus any fields you want logged with the
move.

```python
from agentic_fsm import Decide, Decision

class MessageRead(Decision):
    reason: str

read_message = Decide(
    prompt=lambda order: order.message and f"Where does this message put the order?\n{order.message}",
    schema=MessageRead,
)
```

- **The model is offered only the states its state has an edge to**, each with its description,
  so the prompt never has to explain what a state means.
- **A prompt that returns nothing means "not now"**: no call is made and the entity stays.
- **The whole `Decision` is logged** with the move, and the move's `decided_by` is
  `llm:<model>`. The model is the `DECIDE_MODEL` setting (Claude Haiku by default).

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

read_message = Decide(agent=reader, prompt=...)
```

A plain function may also return a `Decision` in place of a state, when it decides by rule and
wants its reason logged.

### Code first, then the model

A state where facts and a model both have a say still has one handler: a function that moves the
entity on a fact and otherwise hands over, by returning the `Decide`.

```python
from agentic_fsm import check

@check
def pack(order):
    if order.tracking_number:
        return Order.shipped        # code moves it
    if not order.message:
        return None                 # not now
    return read_message             # the model decides

class Order(States):
    ...
    paid = State("Payment cleared, waiting to ship", handler=pack)
    ...
```

- **The move is logged under whoever decided it**: `code` when the function returned the state,
  `llm:<model>` when the model did.
- **The model is offered every state `paid` has an edge to**, `shipped` included. A state's
  description is what tells it which ones only a fact leads to ("Handed to the carrier; set
  when a tracking number arrives").
- **A function can hand over to any handler**, not only a `Decide`: an object of your own
  `Handler` subclass, with a `run(entity, targets)` that returns as a function does.
- **`@check` marks a function that only reads.** It lets `due()` try it without side effects,
  and lets a session answer in the model's place (below).

### An effect that goes with the decision

When a decision carries something to act on (a note the model wrote for the customer), give the
`Decide` an `effect`: a function of the entity and the decision.

```python
def send_note(order, decision):
    if decision.note:
        mail(order.customer, decision.note)

read_message = Decide(prompt=..., schema=MessageRead, effect=send_note)
```

The effect runs once the decision is allowed by the state's edges and before the move is
written, under the entity's lock, whether the model decided or an answer was given in its
place. If it raises, nothing is written.

A subclass that calls its model another way overrides `ask(prompt, entity, schema)` and
`decided_by`.

### A session in the model's place

A coding agent or a person can decide in a `Decide`'s place with no model key set.

```python
brief = orders.brief(42)
# brief.state, brief.instructions, brief.prompt, brief.choices, brief.schema, brief.tools

orders.fire(42, answer={"to": "cancelled", "reason": "They asked for a refund"})
```

`answer=` takes the fields as a dict or a JSON object, or a bare state value when the schema is
a plain `Decision`. The prompt function doesn't run again, and the move's `decided_by` is
`agent`. An answer the schema or the state's edges don't allow raises `TransitionError` with the
reason; nothing is written, and the session answers again.

When the state's handler is a function that hands over to a `Decide`, `brief` and `fire(answer=)`
run the function first and go on only if it hands over. If it moves the entity itself, or says
"not now", there is nothing to decide and both raise `TransitionError`: an answer never
overrides a fact. They run only a function marked `@check`, since it runs again each time and
so must only read.

## Moving entities

| Call | Does |
|---|---|
| `orders.start(42)` | puts an entity in the initial state |
| `orders.start_all(order_ids)` | starts each of the ids that has no state yet |
| `orders.fire(42)` | runs the handler of the entity's state; returns the `Move`, or `None` for "not now" |
| `orders.fire(42, entity=order)` | the same, on the order already in hand instead of `load(42)` |
| `orders.fire(42, answer=...)` | decides in the model's place |
| `orders.brief(42)` | what a `Decide` would give the model |
| `orders.progress(42)` | moves one entity until it rests |
| `orders.progress_all()` | runs the handler of each unfinished entity's state: at most one move each |
| `orders.due()` | the ids of the entities their state's handler could move now; moves nothing |
| `orders.state_of(42)` | the entity's current state |
| `orders.in_state(Order.paid)` | the ids of the entities in a state |
| `orders.history(42)` | every move logged for the entity |
| `orders.graph()` | the states, their handlers and the edges as data, for review |
| `orders.on_move(listener)` | calls `listener(move)` after every move, once it's committed |

### What is guaranteed

- **Every row in the log is a move that happened.** If `pending`'s handler returns anything but
  `Order.paid` or `Order.cancelled`, that's a bug: `fire` raises `TransitionError` and nothing
  is written. Whatever a handler can meet (a bounce, a failed payment) is a state with its own
  edge. A row's `from_state` says which handler ran, and `decided_by` who decided.
- **Two callers can't both move an entity.** `fire` locks the entity from the read of its state
  to the write of the move; a second caller waits, then reads the state the first left. With
  the Django and SQLAlchemy backends the handler's own writes commit with the move.
- **A handler with an outside effect should be safe to run twice.** If it sends an email and the
  process dies before the move commits, the next call runs it again.
- **`progress` never loops.** It goes on until the entity rests (a final state, or a handler
  that says "not now"), loads the entity afresh after each move, commits each move, and stops
  after a move back to a state it already left.

### Running a machine on a schedule

`progress_all` makes at most one move per entity per call. When to call it is up to you: a cron
job, a scheduler, a script.

- **One failing entity doesn't hold the others.** Its move is undone, the rest are tried, and
  `ProgressError` is raised at the end with the moves made (`.moves`) and each entity's
  exception (`.failures`).
- **`start_all` is for entities another program makes**: rows a web app inserts, started by
  whoever runs the machine. Hand it every id there is; the ones already started are left alone.
- **`due` says whether a run is worth starting.** It runs each `@check` handler, takes no lock
  and writes nothing. A handler that isn't a check (it sends an email, or a model decides it)
  can't be tried without its effect, so an entity is due when its state's handler is one, or
  when a check hands over to one.
- **`entities=` skips the rest.** When your own data already says which entities could move (a
  timestamp, a queue), pass their ids as `due(entities=...)` or `progress_all(entities=...)`,
  and the others are never loaded.
- **A tag makes a state a queue.** Give a state `tags=`, and `progress_all(tag)` and `due(tag)`
  try only the entities waiting in a state that carries it. One worker then does one kind of
  work, on its own schedule: the warehouse's run never calls the model, and a slow model never
  holds the warehouse. A state can carry several tags; a tag no state carries raises
  `MachineError`.

  ```python
  class Order(States):
      pending = InitialState("Placed, not paid yet", handler=await_payment)
      paid    = State("Payment cleared, waiting to ship", handler=pack, tags={"warehouse"})
      ...

  orders.progress_all("warehouse")   # only the orders in `paid`
  ```
- **`limit=` takes a few from the queue.** `progress_all("warehouse", limit=1)` stops after
  one move, oldest entity first, so a worker run every few minutes paces what it does. An
  entity that says "not now" or raises doesn't count, so one stuck at the head never holds
  the others.

### When an entity moves

`on_move` adds a listener: a function called with the `Move` after every move the machine
makes, a start included.

```python
@orders.on_move
def tell_the_warehouse(move):
    if move.to_state == Order.paid:
        print(f"order {move.entity} is paid (decided by {move.decided_by})")
```

- **A listener is told once the move is committed**, so it reads the state the move left and
  can make the next move itself. A move made inside a transaction of yours is told when that
  transaction commits, and never if it's undone.
- **A listener can't undo a move.** One that raises is logged (logger `agentic_fsm.machine`)
  and the others are still told; `fire` returns the move as usual.
- **It's told in the process that made the move**, and only once: if that process dies between
  the commit and the call, nobody is told. For something that must follow every move whatever
  happens, read the log (`history`, or the log table) from a job of your own.

## At the command line

`agfsm` makes the same moves from a shell, so a coding agent or a script can do every move your
code can and no other. Each command calls one of the methods above.

| Command | Calls |
|---|---|
| `agfsm show [--mermaid]` | `orders.graph()`: the states with their handlers and descriptions, then the edges |
| `agfsm start order:42` | `orders.start(42)` |
| `agfsm list paid` | `orders.in_state(Order.paid)` |
| `agfsm due [--tag warehouse]` | `orders.due()`, or `orders.due("warehouse")` |
| `agfsm fire order:42` | `orders.fire(42)` |
| `agfsm fire order:42 --agent` | `orders.brief(42)` |
| `agfsm fire order:42 --answer '{...}'` | `orders.fire(42, answer=...)` |
| `agfsm progress order:42` | `orders.progress(42)` |
| `agfsm progress_all [--tag warehouse] [--limit 1]` | `orders.progress_all()`, or `orders.progress_all("warehouse", limit=1)` |

- **The machine** is named as `--machine package.module:name` (a module in the working directory
  works), or by the `MACHINE` setting: `export AGENTIC_FSM_MACHINE=shop.lifecycle:orders`.
- **An entity** is `<machine name>:<id>`, and the name must be the loaded machine's.
- **`--json`** prints the result as data: the `Move`, `Brief` or `Graph`, with times as ISO text.
- **The exit code** is 0 for a move made or "not now", 2 for one that can't be made (an entity
  not started or finished, an answer that doesn't validate, no machine), with the reason on
  stderr. `progress_all` with an entity whose handler raised prints the moves it made, puts each
  failure on stderr, and exits 2.
- **With `DJANGO_SETTINGS_MODULE` naming a project installed here**, `agfsm` sets the Django
  project up before it loads the machine, as `manage.py` would. When Django or that settings
  module isn't installed (the variable was set for another program), it's left alone.

`--agent` then `--answer` lets a session decide in a `Decide`'s place with no model key: the
first prints the instructions, the prompt, the choices and the answer's schema; the second
applies the answer.

## State backends

Where state is kept is a backend. The core imports no database; each backend is its own package,
chosen by its dotted path, the way Django chooses a database engine.

```bash
pip install "agentic-fsm[django]"      # the Django backend
pip install "agentic-fsm[sqlalchemy]"  # the SQLAlchemy backend; add your database's driver
export AGENTIC_FSM_BACKEND=agentic_fsm.backends.django
```

or `Machine(..., backend="agentic_fsm.backends.django")`. A machine with neither raises.

| Backend | Keeps state in | Needs |
|---|---|---|
| `agentic_fsm.backends.django` | a Django project's database | `agentic-fsm[django]` |
| `agentic_fsm.backends.sqlalchemy` | any database SQLAlchemy reaches | `agentic-fsm[sqlalchemy]` and a driver |
| `agentic_fsm.backends.locmem` | this process's memory, for tests | nothing |

Both database backends keep the same two tables: a log (append-only) and one current-state row
per entity. So they can keep machines in one database and read each other's.

### Django

Add the app and migrate:

```python
INSTALLED_APPS = [..., "agentic_fsm.backends.django"]
```

It writes through Django's own connection, so wrapping `fire` in `transaction.atomic()` commits
your rows and the move together.

### SQLAlchemy

The backend connects to the `SQLALCHEMY_URL` setting, or takes an engine, or a function that
makes one (for a machine declared at import whose database is known only when it runs; it's
called on first use):

```python
from agentic_fsm.backends.sqlalchemy.base import StateBackend

backend = StateBackend(engine)  # or built from AGENTIC_FSM_SQLALCHEMY_URL
backend.create_tables()         # where they don't exist yet


def pack(order):
    backend.connection().execute(insert(shipments).values(order_id=order.id))
    return Order.shipped


orders = Machine(Order, EDGES, load=load_order, backend=backend)
```

`backend.connection()`, called while a handler runs, is the connection of the move's
transaction: what the handler writes through it commits with the move and is undone if the
handler raises. `create_tables()` only makes what's missing: a column a later release drops from
the log is left in a database made before it, unused, until you drop it.

### Your own

A backend of your own is a package whose `base` module has a `StateBackend` subclassing
`agentic_fsm.BaseStateBackend`. Its `on_commit(callback)` is what `on_move` rests on: it calls
the callback once the writes made in the open lock are committed.

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

The suite runs on all three backends. Set `AGENTIC_FSM_TEST_POSTGRES_URL` (e.g.
`postgresql+psycopg://postgres:pg@localhost:5432/postgres`) to also test the SQLAlchemy
backend's row lock on Postgres; the test drops and recreates the two tables there.

## Licence

Apache-2.0. See `LICENCE.md`.
