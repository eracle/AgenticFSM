# AgenticFSM

Entity lifecycles declared once and advanced the same way by code, a model or an agent.

An order, a ticket or an application sits in a state for days and is moved along by whatever
knows something new about it. AgenticFSM keeps the allowed moves in one list, records every move
in an append-only log, and refuses a move the list doesn't have.

## Install

```bash
pip install agentic-fsm            # the library; brings Pydantic AI for steps a model decides
pip install "agentic-fsm[django]"  # with the Django state backend
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
function or an `Ask`, every `Ask` deciding among this machine's states, no two steps sharing a
name.

## A step a model decides

An `Ask` is a step that puts the question to a model. It has a name, a prompt function and a
schema: a `Decision` whose `to` is typed as your states, plus any fields you want logged with
the move.

```python
from agentic_fsm import Ask, Decision

class MessageRead(Decision):
    to: Order
    reason: str

read_message = Ask(
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

read_message = Ask(name="read_message", agent=reader, prompt=...)
```

A function step may also return a `Decision` in place of a state, for code that calls
`read_message.run(order)` itself or decides by rule and wants its reason logged.

### A session in the model's place

A coding agent or a person can decide an `Ask` with no model key set.

```python
brief = orders.brief(42, "read_message")
# brief.instructions, brief.prompt, brief.choices, brief.schema, brief.tools

orders.fire(42, "read_message", answer={"to": "cancelled", "reason": "They asked for a refund"})
```

`answer=` takes the fields as a dict or a JSON object, or a bare state value when the schema is
a plain `Decision`. The prompt function doesn't run again. An answer the schema or the step's
rows don't allow is refused and logged, and the move's `decided_by` is `agent`.

## Moving entities

| Call | Does |
|---|---|
| `orders.start(42)` | puts an entity in the initial state |
| `orders.fire(42, payment_cleared)` | runs one step on one entity |
| `orders.fire(42, "read_message", answer=...)` | decides an `Ask` in the model's place |
| `orders.brief(42, "read_message")` | what an `Ask` would give the model |
| `orders.advance()` | tries the steps out of each unfinished entity's state |
| `orders.state_of(42)` | the entity's current state |
| `orders.in_state(Order.paid)` | the ids of the entities in a state |
| `orders.history(42)` | every move logged for the entity |
| `orders.graph()` | the states and rows as data, for review |

- **A step's targets are the rows it sits on.** If `payment_cleared` returns anything but
  `Order.paid`, the move is refused and logged, and the order stays where it is.
- **`fire` returns the logged `Move`**, with `applied` true or false and a `reason` when
  refused, or `None` when the step said "not now".
- **`advance` tries steps in list order** and stops at the first that doesn't return `None`, so
  each entity makes at most one move per call. When to call it is up to you: a cron job, a
  scheduler, a script.
- **Two callers can't both move an entity.** A move is written only if the entity hasn't moved
  since it was read. The one that comes second is refused and logged.
- **A step with an outside effect should be safe to run twice.** If it sends an email and the
  process dies before the move is written, the next `advance` runs it again.

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
| `agfsm advance` | `orders.advance()` |

- **The machine** is named as `--machine package.module:name` (a module in the working directory
  works), or by the `MACHINE` setting: `export AGENTIC_FSM_MACHINE=shop.lifecycle:orders`.
- **An entity** is `<machine name>:<id>`, and the name must be the loaded machine's.
- **`--json`** prints the result as data: the `Move`, `Brief` or `Graph`, with times as ISO text.
- **The exit code** is 0 for a move applied or "not now", 1 for a move refused, 2 for one that
  can't be tried (an unknown step, an entity not started, no machine).
- **With `DJANGO_SETTINGS_MODULE` set**, `agfsm` sets the Django project up before it loads the
  machine, as `manage.py` would.

`--agent` then `--answer` lets a session decide an `Ask` with no model key: the first prints the
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
| `agentic_fsm.backends.locmem` | this process's memory, for tests | nothing |

For the Django backend, add its app and migrate:

```python
INSTALLED_APPS = [..., "agentic_fsm.backends.django"]
```

It writes through Django's own connection, so wrapping `fire` in `transaction.atomic()` commits
your rows and the move together.

A backend of your own is a package whose `base` module has a `StateBackend` subclassing
`agentic_fsm.BaseStateBackend`.

## Settings

Every default is in `agentic_fsm/conf/global_settings.py`, the way Django keeps its own.

| Setting | Default | Is |
|---|---|---|
| `BACKEND` | `None` | the state backend of a machine built without `backend=` |
| `ASK_MODEL` | `"anthropic:claude-haiku-5-5"` | the model of an `Ask` built without `agent=` |
| `MACHINE` | `None` | the machine `agfsm` loads without `--machine`, as `package.module:name` |

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

## Licence

Apache-2.0. See `LICENCE.md`.
