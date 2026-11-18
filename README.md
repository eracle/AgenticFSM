# AgenticFSM

Entity lifecycles declared once and advanced the same way by code, a model or an agent.

An order, a ticket or an application sits in a state for days and is moved along by whatever
knows something new about it. AgenticFSM gives each state one step that decides where an entity
goes next, keeps the allowed moves in one list, records every move in an append-only log, and
raises on a move the list doesn't have.

## Install

```bash
pip install agentic-fsm            # the library; brings Pydantic AI for steps a model decides
pip install "agentic-fsm[django]"  # with the Django state backend
pip install "agentic-fsm[sqlalchemy]"  # with the SQLAlchemy state backend; add your database's driver
```

## A machine

A step is a function of the entity. It returns the next state, or `None` for "not now". What it
checks first is whatever its body checks first.

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
```

States are declared like Django model fields. Each one carries its definition and its step: the
one thing that decides where an entity goes from it. A final state has none.

```python
from agentic_fsm import States, State, InitialState, FinalState

class Order(States):
    pending   = InitialState("Placed, not paid yet", step=await_payment)
    paid      = State("Payment cleared, waiting to ship", step=pack)
    shipped   = State("Handed to the carrier", step=await_delivery)
    delivered = FinalState("The carrier delivered it")
    cancelled = FinalState("Cancelled before it shipped")
```

One list holds the whole graph, one pair per edge. It says which states each state's step may
return.

```python
from agentic_fsm import Machine

EDGES = [
    # from                         to
    ((Order.pending, Order.paid),  Order.cancelled),
    (Order.pending,                Order.paid),
    (Order.paid,                   Order.shipped),
    (Order.shipped,                Order.delivered),
]

orders = Machine(Order, EDGES, load=get_order)
```

`load(id)` fetches the entity from your own tables. The library stores the state, never the
entity. Ids are stored as text, so `load` receives a string.

`Machine(...)` checks the declaration and raises a `MachineError` naming the state or the edge:
exactly one `InitialState`, every state reachable from it, a step on every state but a
`FinalState` and none on those, no edges leaving a `FinalState`, no other state without a way
out, every edge's states belonging to the machine.

## A step a model decides

A `Decide` is a step that puts the question to a model. It has a prompt function and a schema:
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

class Ticket(States):
    open     = InitialState("The customer wrote, nobody answered", step=read_message)
    refunded = FinalState("Money returned")
    closed   = FinalState("Answered")
```

- **The model is offered only the states its state has an edge to**, each with its description,
  so the prompt never has to explain what a state means.
- **A prompt that returns nothing means "not now"**: no call is made and the entity stays.
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

read_message = Decide(agent=reader, prompt=...)
```

A function may also return a `Decision` in place of a state, when it decides by rule and wants
its reason logged.

### Code first, then the model

A state where facts and a model both have a say is still one step: a function that moves the
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
    paid = State("Payment cleared, waiting to ship", step=pack)
    ...
```

- **The move is logged under whoever decided it**: `code` when the function returned the state,
  `llm:<model>` when the model did.
- **The model is offered every state `paid` has an edge to**, `shipped` included. A state's
  description is what tells it which ones only a fact leads to ("Handed to the carrier; set
  when a tracking number arrives").
- **A function can hand over to any step**, not only a `Decide`: an object of your own `Step`
  subclass, with a `run(entity, targets)` that returns as a function does.

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

When the state's step is a function that hands over to a `Decide`, `brief` and `fire(answer=)`
run the function first and go on only if it hands over. If it moves the entity itself, or says
"not now", there is nothing to decide and both raise `TransitionError`: an answer never
overrides a fact. They run only a function marked `@check`, since it runs again each time and
so must only read.

## Moving entities

| Call | Does |
|---|---|
| `orders.start(42)` | puts an entity in the initial state |
| `orders.start_all(order_ids)` | starts each of the ids that has no state yet |
| `orders.fire(42)` | runs the step of the entity's state |
| `orders.fire(42, entity=order)` | the same, on the order already in hand instead of `load(42)` |
| `orders.fire(42, answer=...)` | decides in the model's place |
| `orders.brief(42)` | what a `Decide` would give the model |
| `orders.progress(42)` | moves one entity until it rests |
| `orders.progress_all()` | runs the step of each unfinished entity's state: at most one move each |
| `orders.due()` | the ids of the entities their state's step could move now; moves nothing |
| `orders.state_of(42)` | the entity's current state |
| `orders.in_state(Order.paid)` | the ids of the entities in a state |
| `orders.history(42)` | every move logged for the entity |
| `orders.graph()` | the states, their steps and the edges as data, for review |
| `orders.on_move(listener)` | calls `listener(move)` after every move, once it's committed |

- **Every row in the log is a move that happened.** A step's targets are the states its state
  has an edge to: if `pending`'s step returns anything but `Order.paid` or `Order.cancelled`,
  that's a bug, `fire` raises `TransitionError`, and nothing is written. Whatever a step can
  meet (a bounce, a failed payment) is a state with its own edge.
- **`fire` returns the logged `Move`**, or `None` when the step said "not now". A row's
  `from_state` says which step ran, and `decided_by` who decided.
- **`progress_all` makes at most one move per entity per call.** When to call it is up to you:
  a cron job, a scheduler, a script. An entity whose step raises doesn't hold the others: its
  move is undone, the rest are tried, and `ProgressError` is raised at the end with the moves
  made (`.moves`) and each entity's exception (`.failures`).
- **`start_all` is for entities another program makes**: rows a web app inserts, started by
  whoever runs the machine. Hand it every id there is; the ones already started are left alone.
- **`due` says whether a run is worth starting.** Mark a function that only reads the entity
  with `@check`; `due` runs it, under the entity's lock, and writes nothing. A step that isn't a
  check (it sends an email, or a model decides it) can't be tried without its effect, so an
  entity is due when its state's step is one, or when a check hands over to one.

  ```python
  from agentic_fsm import check

  @check
  def await_payment(order):
      return Order.paid if order.payment_cleared else None
  ```
- **A tag makes a state a queue.** Give a state `tags=`, and `progress_all(tag)` and `due(tag)`
  try only the entities waiting in a state that carries it. One worker then does one kind of
  work, on its own schedule: the warehouse's run never calls the model, and a slow model never
  holds the warehouse. A state can carry several tags; a tag no state carries raises
  `MachineError`.

  ```python
  class Order(States):
      pending   = InitialState("Placed, not paid yet", step=await_payment)
      paid      = State("Payment cleared, waiting to ship", step=pack, tags={"warehouse"})
      ...

  orders.progress_all("warehouse")   # only the orders in `paid`
  ```
- **`limit=` takes a few from the queue.** `progress_all("warehouse", limit=1)` stops after
  one move, oldest entity first, so a worker run every few minutes paces what it does. An
  entity that says "not now" or raises doesn't count, so one stuck at the head never holds
  the others.
- **`progress` goes on until the entity rests**: a final state, or one whose step says "not
  now". It loads the entity afresh after each move, commits each move, and stops after a move
  that brings the entity back to a state it already left, so it never loops.
- **Two callers can't both move an entity.** `fire` locks the entity from the read of its state
  to the write of the move; a second caller waits, then reads the state the first left. With
  the Django and SQLAlchemy backends the step's own writes commit with the move.
- **A step with an outside effect should be safe to run twice.** If it sends an email and the
  process dies before the move commits, the next call runs it again.

## When an entity moves

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
| `agfsm show [--mermaid]` | `orders.graph()`: the states with their steps and descriptions, then the edges |
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
  stderr. `progress_all` with an entity whose step raised prints the moves it made, puts each
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

The SQLAlchemy backend connects to the `SQLALCHEMY_URL` setting, or takes an engine, or a
function that makes one (for a machine declared at import whose database is known only when it
runs; it's called on first use):

```python
from agentic_fsm.backends.sqlalchemy.base import StateBackend

backend = StateBackend(engine)  # or built from AGENTIC_FSM_SQLALCHEMY_URL
backend.create_tables()         # where they don't exist yet


def pack(order):
    backend.connection().execute(insert(shipments).values(order_id=order.id))
    return Order.shipped


orders = Machine(Order, EDGES, load=load_order, backend=backend)
```

`backend.connection()`, called while a step runs, is the connection of the move's transaction:
what the step writes through it commits with the move and is undone if the step raises. Its two
tables are the Django backend's, so the two backends can keep machines in one database and read
each other's. `create_tables()` only makes what's missing: a column a later release drops from
the log is left in a database made before it, unused, until you drop it.

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

Set `AGENTIC_FSM_TEST_POSTGRES_URL` (e.g. `postgresql+psycopg://postgres:pg@localhost:5432/postgres`)
to also test the SQLAlchemy backend's row lock on Postgres; the test drops and recreates the two
tables there.

## Licence

Apache-2.0. See `LICENCE.md`.
