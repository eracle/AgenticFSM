"""``Decide`` steps: a model decides, or a session answers in its place. No test calls a real model."""

from types import SimpleNamespace

import pytest
from pydantic_ai import Agent, RunContext, models
from pydantic_ai.exceptions import UnexpectedModelBehavior
from pydantic_ai.models.test import TestModel

from agentic_fsm import (
    Decide,
    Decision,
    FinalState,
    InitialState,
    Machine,
    MachineError,
    State,
    States,
    TransitionError,
)

models.ALLOW_MODEL_REQUESTS = False


class Order(States):
    placed = InitialState("Placed, waiting for the customer to confirm it")
    confirmed = State("The customer confirmed it, waiting to ship")
    shipped = FinalState("Handed to the carrier")
    cancelled = FinalState("The customer called it off")


class MessageRead(Decision):
    to: Order
    reason: str


def message_prompt(order):
    return order.message and f"Where does this message put the order?\n{order.message}"


def ship(order):
    return Order.shipped if order.tracking_number else None


def new_order(**facts):
    return SimpleNamespace(**{"message": None, "tracking_number": None, **facts})


def model_saying(**fields) -> TestModel:
    """A model that answers every question with these fields."""
    return TestModel(custom_output_args=fields)


def reader(model, **agent_options) -> Decide:
    agent = Agent(model, output_type=MessageRead, instructions="You read customer messages.", **agent_options)
    return Decide(name="read_message", prompt=message_prompt, agent=agent)


def machine_with(backend, table, read_message) -> Machine:
    # fmt: off
    rows = [
        # from             to                step
        (Order.placed,     Order.confirmed,  read_message),
        (Order.placed,     Order.cancelled,  read_message),
        (Order.confirmed,  Order.cancelled,  read_message),
        (Order.confirmed,  Order.shipped,    ship),
    ]
    # fmt: on
    return Machine(Order, rows, load=lambda order_id: table[order_id], backend=backend)


def offered(model: TestModel) -> dict:
    """The JSON Schema of the output the model was last asked for."""
    return model.last_model_request_parameters.output_tools[0].parameters_json_schema


# ── A model decides ──


def test_a_decide_moves_the_entity_and_logs_the_decision(backend, table):
    orders = machine_with(backend, table, reader(model_saying(to="confirmed", reason="They said yes")))
    table["1"] = new_order(message="Yes, please send it")
    orders.start(1)

    move = orders.fire(1, "read_message")

    assert (move.step, move.from_state, move.to_state) == ("read_message", "placed", "confirmed")
    assert move.decided_by == "llm:test:test"
    assert move.decision == {"to": "confirmed", "reason": "They said yes"}
    assert orders.state_of(1) is Order.confirmed
    assert orders.history(1)[-1] == move


def test_a_prompt_that_returns_nothing_makes_no_call(backend, table):
    model = model_saying(to="confirmed", reason="-")
    orders = machine_with(backend, table, reader(model))
    table["1"] = new_order()
    orders.start(1)

    assert orders.fire(1, "read_message") is None

    assert model.last_model_request_parameters is None
    assert orders.state_of(1) is Order.placed
    assert len(orders.history(1)) == 1


def test_the_model_is_offered_only_the_steps_rows_with_their_descriptions(backend, table):
    model = model_saying(to="cancelled", reason="-")
    orders = machine_with(backend, table, reader(model))
    table["1"] = new_order(message="Stop")
    table["2"] = new_order(message="Stop")
    orders.start(1)
    orders.start(2)
    orders.fire(2, "read_message", answer={"to": "confirmed", "reason": "-"})

    orders.fire(1, "read_message")
    out_of_placed = offered(model)["properties"]["to"]
    orders.fire(2, "read_message")
    out_of_confirmed = offered(model)["properties"]["to"]

    assert out_of_placed["enum"] == ["confirmed", "cancelled"]
    assert "- confirmed: The customer confirmed it, waiting to ship" in out_of_placed["description"]
    assert "- cancelled: The customer called it off" in out_of_placed["description"]
    assert out_of_confirmed["const"] == "cancelled"


def test_a_model_that_insists_on_a_state_outside_the_rows_moves_nothing(backend, table):
    orders = machine_with(backend, table, reader(model_saying(to="shipped", reason="-")))
    table["1"] = new_order(message="Where is it?")
    orders.start(1)

    with pytest.raises(UnexpectedModelBehavior):
        orders.fire(1, "read_message")

    assert orders.state_of(1) is Order.placed
    assert len(orders.history(1)) == 1


def test_the_entity_reaches_the_agents_tools_as_deps(backend, table):
    seen = []

    def past_orders(ctx: RunContext[object]) -> str:
        """The customer's earlier orders."""
        seen.append(ctx.deps)
        return "none"

    model = TestModel(custom_output_args={"to": "confirmed", "reason": "-"})
    orders = machine_with(backend, table, reader(model, tools=[past_orders]))
    table["1"] = new_order(message="Yes")
    orders.start(1)

    orders.fire(1, "read_message")

    assert seen == [table["1"]]


def test_progress_all_runs_a_decide_like_any_step(backend, table):
    orders = machine_with(backend, table, reader(model_saying(to="cancelled", reason="Changed their mind")))
    table["1"] = new_order(message="Cancel it")
    table["2"] = new_order()
    orders.start(1)
    orders.start(2)

    moves = orders.progress_all()

    assert [(m.entity, m.step, m.to_state) for m in moves] == [("1", "read_message", "cancelled")]


def test_a_decide_runs_outside_a_machine_with_every_state_open():
    read_message = reader(model_saying(to="shipped", reason="-"))
    decision = read_message.run(new_order(message="Hello"))
    assert decision.to is Order.shipped
    assert read_message.run(new_order()) is None


def test_without_an_agent_the_schema_is_asked_of_haiku_and_needs_no_key_to_build(monkeypatch):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    read_message = Decide(name="read_message", prompt=message_prompt, schema=MessageRead)
    assert read_message.agent.model == "anthropic:claude-haiku-5-5"
    assert read_message.schema is MessageRead
    assert read_message.decided_by == "llm:anthropic:claude-haiku-5-5"


def test_the_default_model_is_a_setting(monkeypatch):
    monkeypatch.setenv("AGENTIC_FSM_DECIDE_MODEL", "test")
    read_message = Decide(name="read_message", prompt=message_prompt, schema=MessageRead)
    assert read_message.decided_by == "llm:test"


# ── A code step that returns a Decision ──


def decided_in_code(backend, table, to):
    def read_message(order):
        return MessageRead(to=to, reason="A rule matched")

    orders = machine_with(backend, table, read_message)
    table["1"] = new_order()
    orders.start(1)
    return orders, read_message


def test_a_code_step_may_return_a_decision(backend, table):
    orders, read_message = decided_in_code(backend, table, Order.cancelled)
    move = orders.fire(1, read_message)
    assert move.decided_by == "code"
    assert move.decision == {"to": "cancelled", "reason": "A rule matched"}
    assert orders.state_of(1) is Order.cancelled


def test_a_decision_outside_the_steps_rows_raises_and_writes_nothing(backend, table):
    orders, read_message = decided_in_code(backend, table, Order.shipped)
    with pytest.raises(TransitionError, match="may move placed only to confirmed, cancelled"):
        orders.fire(1, read_message)
    assert orders.state_of(1) is Order.placed
    assert len(orders.history(1)) == 1


# ── A session answers in the model's place ──


@pytest.fixture
def unreachable(backend, table):
    """A machine whose Decide has no model to call and a prompt that must not run."""

    def prompt(order):
        raise AssertionError("the prompt ran")

    agent = Agent(output_type=MessageRead, instructions="You read customer messages.")
    orders = machine_with(backend, table, Decide(name="read_message", prompt=prompt, agent=agent))
    table["1"] = new_order(message="Yes")
    orders.start(1)
    return orders


@pytest.mark.parametrize(
    "answer",
    [{"to": "confirmed", "reason": "They said yes"}, '{"to": "confirmed", "reason": "They said yes"}'],
)
def test_an_answer_is_applied_without_a_model_or_the_prompt(unreachable, answer):
    move = unreachable.fire(1, "read_message", answer=answer)

    assert move.to_state == "confirmed"
    assert move.decided_by == "agent"
    assert move.decision == {"to": "confirmed", "reason": "They said yes"}
    assert unreachable.state_of(1) is Order.confirmed


@pytest.mark.parametrize(
    ("answer", "reason"),
    [
        ({"to": "shipped", "reason": "-"}, "to: Input should be"),
        ({"to": "confirmed"}, "reason: Field required"),
        ("confirmed", "reason: Field required"),
        ({"reason": "-"}, "to: Field required"),
    ],
)
def test_an_answer_the_schema_or_the_rows_do_not_allow_raises_and_writes_nothing(unreachable, answer, reason):
    with pytest.raises(TransitionError, match="the answer to read_message doesn't validate") as error:
        unreachable.fire(1, "read_message", answer=answer)

    assert reason in str(error.value)
    assert unreachable.state_of(1) is Order.placed
    assert len(unreachable.history(1)) == 1


def test_a_bare_decision_is_answered_with_a_state_alone(backend, table):
    orders = machine_with(backend, table, Decide(name="read_message", prompt=message_prompt))
    table["1"] = new_order(message="No thanks")
    orders.start(1)

    move = orders.fire(1, "read_message", answer="cancelled")

    assert move.decision == {"to": "cancelled"}
    assert orders.state_of(1) is Order.cancelled


def test_a_code_step_takes_no_answer_and_has_no_brief(backend, table):
    orders = machine_with(backend, table, reader(model_saying(to="confirmed", reason="-")))
    table["1"] = new_order(message="Yes", tracking_number="TRK1")
    orders.start(1)
    orders.fire(1, "read_message")

    with pytest.raises(TransitionError, match="ship is code, not a Decide: it takes no answer"):
        orders.fire(1, ship, answer="shipped")
    with pytest.raises(TransitionError, match="ship is code, not a Decide: there is nothing to brief"):
        orders.brief(1, ship)


def test_a_brief_holds_what_the_model_would_get(backend, table):
    def past_orders(ctx: RunContext[object]) -> str:
        """The customer's earlier orders."""
        return "none"

    model = model_saying(to="confirmed", reason="-")
    orders = machine_with(backend, table, reader(model, tools=[past_orders]))
    table["1"] = new_order(message="Yes, please send it")
    orders.start(1)

    brief = orders.brief(1, "read_message")

    assert brief.step == "read_message"
    assert brief.instructions == ["You read customer messages."]
    assert brief.prompt == "Where does this message put the order?\nYes, please send it"
    assert brief.choices == [
        ("confirmed", "The customer confirmed it, waiting to ship"),
        ("cancelled", "The customer called it off"),
    ]
    assert brief.schema["properties"]["to"]["enum"] == ["confirmed", "cancelled"]
    assert brief.schema["required"] == ["to", "reason"]
    assert brief.tools == [("past_orders", "The customer's earlier orders.")]
    assert model.last_model_request_parameters is None
    assert len(orders.history(1)) == 1


def test_a_brief_with_no_prompt_says_not_now(backend, table):
    orders = machine_with(backend, table, reader(model_saying(to="confirmed", reason="-")))
    table["1"] = new_order()
    orders.start(1)
    assert orders.brief(1, "read_message").prompt is None


def test_a_brief_is_written_from_the_entity_in_hand(backend, table):
    orders = machine_with(backend, table, reader(model_saying(to="confirmed", reason="-")))
    table["1"] = new_order()
    orders.start(1)

    brief = orders.brief(1, "read_message", entity=new_order(message="Cancel it"))

    assert brief.prompt == "Where does this message put the order?\nCancel it"


# ── An effect that runs with the decision ──


def machine_with_effect(backend, table, model, effect) -> Machine:
    agent = Agent(model, output_type=MessageRead, instructions="You read customer messages.")
    read_message = Decide(name="read_message", prompt=message_prompt, agent=agent, effect=effect)
    orders = machine_with(backend, table, read_message)
    table["1"] = new_order(message="Yes, please send it", notes=[])
    orders.start(1)
    return orders


def note_the_reason(order, decision):
    order.notes.append((decision.to, decision.reason))


def test_an_effect_runs_with_the_models_decision_before_the_move(backend, table):
    seen = []

    def effect(order, decision):
        seen.append((orders.state_of(1), decision.to, decision.reason))

    orders = machine_with_effect(backend, table, model_saying(to="confirmed", reason="They said yes"), effect)

    orders.fire(1, "read_message")

    assert seen == [(Order.placed, Order.confirmed, "They said yes")]
    assert orders.state_of(1) is Order.confirmed


def test_an_effect_runs_with_an_answer_on_the_loaded_entity(backend, table):
    orders = machine_with_effect(backend, table, None, note_the_reason)

    orders.fire(1, "read_message", answer={"to": "cancelled", "reason": "Changed their mind"})

    assert table["1"].notes == [(Order.cancelled, "Changed their mind")]


def test_an_effect_that_raises_leaves_nothing_written(backend, table):
    def effect(order, decision):
        raise RuntimeError("the carrier is down")

    orders = machine_with_effect(backend, table, model_saying(to="confirmed", reason="-"), effect)

    with pytest.raises(RuntimeError, match="the carrier is down"):
        orders.fire(1, "read_message")

    assert orders.state_of(1) is Order.placed
    assert len(orders.history(1)) == 1


def test_an_effect_does_not_run_for_a_decision_the_rows_refuse(backend, table):
    orders = machine_with_effect(backend, table, None, note_the_reason)

    with pytest.raises(TransitionError):
        orders.fire(1, "read_message", answer={"to": "shipped", "reason": "-"})

    assert table["1"].notes == []


def test_no_prompt_no_effect(backend, table):
    orders = machine_with_effect(backend, table, model_saying(to="confirmed", reason="-"), note_the_reason)
    table["1"].message = None

    assert orders.fire(1, "read_message") is None
    assert table["1"].notes == []


def test_a_subclass_asks_the_model_its_own_way(backend, table):
    class OwnWay(Decide):
        decided_by = "llm:own"

        def ask(self, prompt, entity, schema):
            return schema(to="cancelled", reason=f"read: {prompt.splitlines()[-1]}")

    orders = machine_with(backend, table, OwnWay(name="read_message", prompt=message_prompt, schema=MessageRead))
    table["1"] = new_order(message="No thanks")
    orders.start(1)

    move = orders.fire(1, "read_message")

    assert move.decided_by == "llm:own"
    assert move.decision == {"to": "cancelled", "reason": "read: No thanks"}


def test_an_effect_must_be_a_function():
    with pytest.raises(TypeError, match="effect= must be a function"):
        Decide(name="read_message", prompt=message_prompt, effect="send it")


# ── Declaring a Decide ──


def test_a_decide_typed_for_another_machines_states_is_refused(backend, table):
    class Door(States):
        closed = InitialState("Shut")
        open = FinalState("Open")

    class DoorRead(Decision):
        to: Door

    with pytest.raises(MachineError, match="row 0: read_message's Decision types `to` as Door, not Order"):
        machine_with(backend, table, Decide(name="read_message", prompt=message_prompt, schema=DoorRead))


def test_an_agent_whose_output_is_not_a_decision_is_refused():
    with pytest.raises(TypeError, match="read_message: the output type <class 'str'> must subclass Decision"):
        Decide(name="read_message", prompt=message_prompt, agent=Agent(TestModel()))


def test_a_decide_takes_an_agent_or_a_schema_not_both():
    agent = Agent(TestModel(), output_type=MessageRead)
    with pytest.raises(TypeError, match="pass agent= or schema=, not both"):
        Decide(name="read_message", prompt=message_prompt, agent=agent, schema=MessageRead)


@pytest.mark.parametrize("options", [{"name": "", "prompt": message_prompt}, {"name": "read", "prompt": "Read it"}])
def test_a_decide_needs_a_name_and_a_prompt_function(options):
    with pytest.raises(TypeError):
        Decide(**options)


def test_a_decide_and_a_function_cannot_share_a_name(backend, table):
    def read_message(order):
        return Order.cancelled

    rows = [
        (Order.placed, Order.confirmed, Decide(name="read_message", prompt=message_prompt)),
        (Order.confirmed, Order.shipped, ship),
        (Order.confirmed, Order.cancelled, read_message),
    ]
    with pytest.raises(MachineError, match="two different steps are named 'read_message'"):
        Machine(Order, rows, load=table.get, backend=backend)
