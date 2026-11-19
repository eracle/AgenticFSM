"""Handlers: what decides where an entity goes from a state.

    def pending(order):
        if order.cancel_requested:
            return Order.cancelled
        return Order.paid if order.payment_cleared else None

    class Order(States):
        pending = InitialState("Placed, not paid yet", handler=pending)

A state has one handler. It is a plain function of the entity, or an object of a ``Handler``
subclass (a ``Decide``, or a caller's own). A function is wrapped into a ``Handler`` where the
state is declared, so the machine only ever runs ``handler.run(entity, targets)``.
"""

from abc import ABC, abstractmethod
from collections.abc import Callable

CHECK_ATTRIBUTE = "agentic_fsm_check"


class Handler(ABC):
    """What decides where an entity goes from a state.

    ``reads_only`` says ``Machine.due`` may run the handler: it changes nothing. ``decided_by``
    is who a move it decides is logged under. ``effect``, when set, is a function of the entity
    and the handler's result that the machine runs once the result is allowed and before the
    move is written.
    """

    reads_only: bool = False
    decided_by: str = "code"
    effect: Callable[[object, object], None] | None = None

    @abstractmethod
    def run(self, entity, targets: tuple):
        """Where ``entity`` goes, out of ``targets``: a state, a ``Decision``, another
        ``Handler`` to hand over to, or ``None`` for "not now"."""

    @property
    def label(self) -> str:
        """What the handler is called when a machine is shown."""
        return type(self).__name__


class FunctionHandler(Handler):
    """A plain function of the entity, as a handler."""

    def __init__(self, function: Callable):
        self.function = function
        self.reads_only = is_check(function)

    def __repr__(self) -> str:
        return f"FunctionHandler({self.label})"

    def run(self, entity, targets: tuple):
        return self.function(entity)

    @property
    def label(self) -> str:
        return getattr(self.function, "__name__", None) or repr(self.function)


def as_handler(handler) -> Handler | None:
    """``handler=`` as a ``Handler``: one as it is, a function wrapped, ``None`` for none."""
    if handler is None or isinstance(handler, Handler):
        return handler
    if callable(handler) and not isinstance(handler, type):
        return FunctionHandler(handler)
    raise TypeError(f"handler= must be a function of the entity or a Handler, got {handler!r}")


# ── Checks ──


def check(function: Callable) -> Callable:
    """Mark a function handler as a check: it only reads the entity, so ``Machine.due`` may run it.

    Used as a decorator. A ``Handler`` object says so itself, with ``reads_only``.
    """
    if isinstance(function, Handler) or not callable(function):
        raise TypeError(f"{function!r} can't be a check: only a function that only reads can")
    setattr(function, CHECK_ATTRIBUTE, True)
    return function


def is_check(function: Callable) -> bool:
    """Whether ``function`` was marked with ``check``."""
    return getattr(function, CHECK_ATTRIBUTE, False) is True
