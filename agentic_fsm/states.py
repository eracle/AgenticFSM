"""States, declared like Django model fields and turned into a real enum.

    class Order(States):
        pending   = InitialState("Placed, not paid yet")
        paid      = State("Payment cleared, waiting to ship")
        delivered = FinalState("The carrier delivered it")

``Order`` is a ``str`` enum: ``Order.pending == "pending"``, ``Order("pending") is Order.pending``, and it
works as a Pydantic field type. Each member keeps its description and its kind.
"""

from enum import Enum, EnumMeta

INITIAL = "initial"
PLAIN = "state"
FINAL = "final"


# ── Declarations ──


class State:
    """A state with its definition. ``value=`` overrides the stored value (default: the name)."""

    kind = PLAIN

    def __init__(self, description: str, *, value: str | None = None):
        if not isinstance(description, str) or not description.strip():
            raise TypeError(f"{type(self).__name__} needs a description, the state's definition")
        if value is not None and (not isinstance(value, str) or not value):
            raise TypeError(f"value= must be a non-empty string, got {value!r}")
        self.description = description
        self.value = value


class InitialState(State):
    """The state every entity starts in. A machine has exactly one."""

    kind = INITIAL


class FinalState(State):
    """A state with no way out."""

    kind = FINAL


# ── The enum ──


class _StatesMeta(EnumMeta):
    """Replaces each ``State`` declaration with its stored value before the enum is built."""

    def __new__(mcls, cls_name, bases, classdict, **kwargs):
        declared = {
            name: classdict[name] for name in list(classdict._member_names)  # declaration order
        }
        if not declared:
            return super().__new__(mcls, cls_name, bases, classdict, **kwargs)

        _refuse_non_states(cls_name, declared)
        _refuse_duplicate_values(cls_name, declared)

        rewritten = mcls.__prepare__(cls_name, bases, **kwargs)
        for key, item in classdict.items():
            rewritten[key] = (item.value or key) if key in declared else item

        enum_cls = super().__new__(mcls, cls_name, bases, rewritten, **kwargs)
        enum_cls._declarations = declared
        return enum_cls


def _refuse_non_states(cls_name, declared):
    for name, item in declared.items():
        if not isinstance(item, State):
            raise TypeError(
                f"{cls_name}.{name} is {item!r}: every attribute of a States class must be a State, "
                "an InitialState or a FinalState"
            )


def _refuse_duplicate_values(cls_name, declared):
    seen = {}
    for name, item in declared.items():
        value = item.value or name
        if value in seen:
            raise TypeError(f"{cls_name}.{name} and {cls_name}.{seen[value]} share the value {value!r}")
        seen[value] = name


class States(str, Enum, metaclass=_StatesMeta):
    """Base class for a machine's states."""

    @property
    def description(self) -> str:
        return type(self)._declarations[self.name].description

    @property
    def kind(self) -> str:
        return type(self)._declarations[self.name].kind

    @property
    def is_initial(self) -> bool:
        return self.kind == INITIAL

    @property
    def is_final(self) -> bool:
        return self.kind == FINAL

    def __str__(self) -> str:
        return self.value
