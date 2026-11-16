# AgenticFSM

Entity lifecycles declared once and advanced the same way by code, a model or an agent.

An order, a ticket or an application sits in a state for days and is moved along by whatever
knows something new about it. AgenticFSM keeps the allowed moves in one list, records every move
in an append-only log, and refuses a move the list doesn't have.

## Install

```bash
pip install agentic-fsm            # the core, no dependencies
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
function, no two steps sharing a name.

## Moving entities

| Call | Does |
|---|---|
| `orders.start(42)` | puts an entity in the initial state |
| `orders.fire(42, payment_cleared)` | runs one step on one entity |
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

## Development

```bash
uv venv && uv pip install -e ".[dev]"
pytest
```

## Licence

GPL-3.0-or-later. See `LICENCE.md`.
