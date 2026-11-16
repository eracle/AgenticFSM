"""Steps a model or an agent decides: ``Decision`` and ``Ask``.

    class MessageRead(Decision):
        to: Order
        reason: str

    read_message = Ask(
        name="read_message",
        prompt=lambda order: order.message and f"Where does this message put the order?\\n{order.message}",
        schema=MessageRead,
    )

An ``Ask`` sits on transition rows like a function step. When it runs, the model is offered only
the targets of its rows out of the entity's state, each with its description. A session can stand
in for the model: ``machine.brief(...)`` gives it what the model would get, and
``machine.fire(..., answer=...)`` applies what it decided.
"""

import json
from collections.abc import Callable
from dataclasses import dataclass
from typing import Literal

from pydantic import BaseModel, Field, ValidationError, create_model
from pydantic_ai import Agent

from .conf import settings
from .states import States


class Decision(BaseModel):
    """What a model, an agent or code decided: where the entity goes, and whatever a subclass adds.

    A subclass types ``to`` as its machine's own ``States`` class.
    """

    to: States


@dataclass(frozen=True)
class Brief:
    """Everything a session needs to decide an ``Ask`` in the model's place."""

    step: str
    instructions: list[str]
    prompt: str | None  # None: the prompt function said "not now"
    choices: list[tuple[str, str]]  # (state value, description)
    schema: dict  # JSON Schema of the answer, ``to`` narrowed to the choices
    tools: list[tuple[str, str]]  # (name, description) of the agent's own tools


class Ask:
    """A step decided by a Pydantic AI ``Agent``.

    ``prompt`` is a function of the entity; a falsy return means "not now" and no call is made.
    ``agent`` holds the whole model-side definition, and its output type must subclass
    ``Decision``. Without it, ``schema`` (default: a bare ``Decision``) is put to the
    ``ASK_MODEL`` setting's model.
    """

    def __init__(
        self,
        *,
        name: str,
        prompt: Callable[[object], str | None],
        agent: Agent | None = None,
        schema: type[Decision] | None = None,
    ):
        if not isinstance(name, str) or not name:
            raise TypeError(f"an Ask needs a name, got {name!r}")
        if not callable(prompt):
            raise TypeError(f"{name}: prompt= must be a function of the entity")
        if agent is not None and schema is not None:
            raise TypeError(f"{name}: pass agent= or schema=, not both; an agent's output type is its schema")
        if agent is None:
            agent = Agent(settings.ASK_MODEL, output_type=schema or Decision, defer_model_check=True)
        if not _is_decision_type(agent.output_type):
            raise TypeError(f"{name}: the output type {agent.output_type!r} must subclass Decision")
        self.name = name
        self.prompt = prompt
        self.agent = agent
        self.schema: type[Decision] = agent.output_type
        self._narrowed: dict[tuple[States, ...], type[Decision]] = {}

    def __repr__(self) -> str:
        return f"Ask({self.name!r})"

    @property
    def states(self) -> type[States]:
        """The ``States`` class the schema's ``to`` is typed as."""
        return self.schema.model_fields["to"].annotation

    @property
    def decided_by(self) -> str:
        """Who a move decided by this step's model is logged under: ``llm:<model>``."""
        model = self.agent.model
        if model is None or isinstance(model, str):
            return f"llm:{model}"
        return f"llm:{model.system}:{model.model_name}"

    # ── Deciding ──

    def run(self, entity, targets: tuple[States, ...] | None = None) -> Decision | None:
        """Ask the model about ``entity``: its decision, or ``None`` if the prompt said "not now".

        With ``targets``, the model is offered only those states.
        """
        prompt = self.prompt(entity)
        if not prompt:
            return None
        return self.agent.run_sync(prompt, deps=entity, output_type=self._schema_for(targets)).output

    def parse(self, answer, targets: tuple[States, ...] | None = None) -> Decision:
        """Validate an answer given in the model's place. Raises ``ValueError`` saying what's wrong."""
        try:
            return self._schema_for(targets).model_validate(answer_fields(answer))
        except ValidationError as error:
            raise ValueError(_summary(error)) from None

    def brief(self, entity, targets: tuple[States, ...]) -> Brief:
        """What the model would be given for ``entity``, for a session deciding in its place."""
        return Brief(
            step=self.name,
            instructions=_static_instructions(self.agent),
            prompt=self.prompt(entity) or None,
            choices=[(target.value, target.description) for target in targets],
            schema=self._schema_for(targets).model_json_schema(),
            tools=_function_tools(self.agent),
        )

    def _schema_for(self, targets: tuple[States, ...] | None) -> type[Decision]:
        """The schema with ``to`` narrowed to ``targets``, each listed with its description."""
        if not targets:
            return self.schema
        if targets not in self._narrowed:
            choices = "\n".join(f"- {target.value}: {target.description}" for target in targets)
            self._narrowed[targets] = create_model(
                self.schema.__name__,
                __base__=self.schema,
                to=(Literal[targets], Field(description=f"Where it goes next, one of:\n{choices}")),
            )
        return self._narrowed[targets]


# ── Answers ──


def answer_fields(answer) -> dict:
    """An answer as the schema's fields: a dict, a ``Decision``, a JSON object, or a bare state."""
    if isinstance(answer, dict):
        return answer
    if isinstance(answer, BaseModel):
        return answer.model_dump(mode="json")
    try:
        parsed = json.loads(answer)
    except (TypeError, ValueError):
        parsed = None
    return parsed if isinstance(parsed, dict) else {"to": answer}


def _summary(error: ValidationError) -> str:
    return "; ".join(f"{'.'.join(map(str, e['loc'])) or 'answer'}: {e['msg']}" for e in error.errors())


# ── Reading an Agent ──


def _is_decision_type(output_type) -> bool:
    return isinstance(output_type, type) and issubclass(output_type, Decision)


def _static_instructions(agent: Agent) -> list[str]:
    """The agent's instructions that are plain text. One computed per run needs a run to exist."""
    declared = getattr(agent, "_instructions", None) or []
    texts = [getattr(item, "instruction", item) for item in declared]
    return [text for text in texts if isinstance(text, str)]


def _function_tools(agent: Agent) -> list[tuple[str, str]]:
    """The agent's function tools. A toolset that lists its tools only once connected is skipped."""
    tools = []
    for toolset in agent.toolsets:
        for name, tool in getattr(toolset, "tools", {}).items():
            tools.append((name, tool.description or ""))
    return tools
