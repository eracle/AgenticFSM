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
    State,
    States,
    TransitionError,
    check,
)

models.ALLOW_MODEL_REQUESTS = False


class MessageRead(Decision):
    reason: str


def message_prompt(order):
    return order.message and f"Where does this message put the order?\n{order.message}"


def new_order(**facts):
    return SimpleNamespace(**{"message": None, "tracking_number": None, "on_hold": False, **facts})


def model_saying(**fields) -> TestModel:
    """A model that answers every question with these fields."""
    return TestModel(custom_output_args=fields)


def reader(model, **agent_options) -> Decide:
    agent = Agent(model, output_type=MessageRead, instructions="You read customer messages.", **agent_options)
    return Decide(prompt=message_prompt, agent=agent)


def machine_with(backend, table, read_message, *, confirmed_step=None) -> Machine:
    """An order machine: a placed order is the model's, a confirmed one is shipped by code on a
    tracking number, held by code, and otherwise handed to the model."""

    @check
    def ship_or_read(order):
        if order.tracking_number:
            return Order.shipped
        return None if order.on_hold else read_message

    class Order(States):
        placed = InitialState("Placed, waiting for the customer to confirm it", step=read_message)
        confirmed = State("The customer confirmed it, waiting to ship", step=confirmed_step or ship_or_read)
        shipped = FinalState("Handed to the carrier")
        cancelled = FinalState("The customer called it off")

    # fmt: off
    edges = [
        # from             to
        (Order.placed,     Order.confirmed),
        (Order.placed,     Order.cancelled),
        (Order.confirmed,  Order.cancelled),
        (Order.confirmed,  Order.shipped),
    ]
    # fmt: on
    return Machine(Order, edges, load=lambda order_id: table[order_id], backend=backend)


def confirmed_order(orders, table, **facts):
    """Order 1, started and confirmed by an answer, with ``facts`` set on it."""
    table["1"] = new_order(message="Yes", **facts)
    orders.start(1)
    orders.fire(1, answer={"to": "confirmed", "reason": "They said yes"})


def offered(model: TestModel) -> dict:
    """The JSON Schema of the output the model was last asked for."""
    return model.last_model_request_parameters.output_tools[0].parameters_json_schema


# ── A model decides ──


def test_a_decide_moves_the_entity_and_logs_the_decision(backend, table):
    orders = machine_with(backend, table, reader(model_saying(to="confirmed", reason="They said yes")))
    table["1"] = new_order(message="Yes, please send it")
    orders.start(1)

    move = orders.fire(1)

    assert (move.from_state, move.to_state) == ("placed", "confirmed")
    assert move.decided_by == "llm:test:test"
    assert move.decision == {"to": "confirmed", "reason": "They said yes"}
    assert orders.state_of(1) is orders.states.confirmed
    assert orders.history(1)[-1] == move


def test_a_prompt_that_returns_nothing_makes_no_call(backend, table):
    model = model_saying(to="confirmed", reason="-")
    orders = machine_with(backend, table, reader(model))
    table["1"] = new_order()
    orders.start(1)

    assert orders.fire(1) is None

    assert model.last_model_request_parameters is None
    assert orders.state_of(1) is orders.states.placed
    assert len(orders.history(1)) == 1


def test_the_model_is_offered_only_the_states_its_state_leads_to_with_their_descriptions(backend, table):
    model = model_saying(to="cancelled", reason="-")
    orders = machine_with(backend, table, reader(model))
    table["1"] = new_order(message="Stop")
    table["2"] = new_order(message="Stop")
    orders.start(1)
    orders.start(2)
    orders.fire(2, answer={"to": "confirmed", "reason": "-"})

    orders.fire(1)
    out_of_placed = offered(model)["properties"]["to"]
    orders.fire(2)
    out_of_confirmed = offered(model)["properties"]["to"]

    assert out_of_placed["enum"] == ["confirmed", "cancelled"]
    assert "- confirmed: The customer confirmed it, waiting to ship" in out_of_placed["description"]
    assert "- cancelled: The customer called it off" in out_of_placed["description"]
    assert out_of_confirmed["enum"] == ["cancelled", "shipped"]


def test_a_model_that_insists_on_a_state_outside_the_edges_moves_nothing(backend, table):
    orders = machine_with(backend, table, reader(model_saying(to="shipped", reason="-")))
    table["1"] = new_order(message="Where is it?")
    orders.start(1)

    with pytest.raises(UnexpectedModelBehavior):
        orders.fire(1)

    assert orders.state_of(1) is orders.states.placed
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

    orders.fire(1)

    assert seen == [table["1"]]


def test_progress_all_runs_a_decide_like_any_step(backend, table):
    orders = machine_with(backend, table, reader(model_saying(to="cancelled", reason="Changed their mind")))
    table["1"] = new_order(message="Cancel it")
    table["2"] = new_order()
    orders.start(1)
    orders.start(2)

    moves = orders.progress_all()

    assert [(m.entity, m.to_state, m.decided_by) for m in moves] == [("1", "cancelled", "llm:test:test")]


def test_without_an_agent_the_schema_is_asked_of_haiku_and_needs_no_key_to_build(monkeypatch):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    read_message = Decide(prompt=message_prompt, schema=MessageRead)
    assert read_message.agent.model == "anthropic:claude-haiku-5-5"
    assert read_message.schema is MessageRead
    assert read_message.decided_by == "llm:anthropic:claude-haiku-5-5"


def test_the_default_model_is_a_setting(monkeypatch):
    monkeypatch.setenv("AGENTIC_FSM_DECIDE_MODEL", "test")
    read_message = Decide(prompt=message_prompt, schema=MessageRead)
    assert read_message.decided_by == "llm:test"


# ── A function that hands over to a Decide ──


def test_a_function_hands_over_to_the_model_which_decides(backend, table):
    orders = machine_with(backend, table, reader(model_saying(to="cancelled", reason="Changed their mind")))
    confirmed_order(orders, table)

    move = orders.fire(1)

    assert (move.from_state, move.to_state, move.decided_by) == ("confirmed", "cancelled", "llm:test:test")
    assert move.decision == {"to": "cancelled", "reason": "Changed their mind"}


def test_a_function_that_moves_the_entity_on_a_fact_never_asks_the_model(backend, table):
    model = model_saying(to="cancelled", reason="-")
    orders = machine_with(backend, table, reader(model))
    confirmed_order(orders, table, tracking_number="TRK1")

    move = orders.fire(1)

    assert (move.to_state, move.decided_by, move.decision) == ("shipped", "code", None)
    assert model.last_model_request_parameters is None


def test_due_lists_an_entity_handed_to_the_model_without_asking_it(backend, table):
    model = model_saying(to="cancelled", reason="-")
    orders = machine_with(backend, table, reader(model))
    confirmed_order(orders, table)
    table["2"] = new_order(message="Yes", on_hold=True)
    orders.start(2)
    orders.fire(2, answer={"to": "confirmed", "reason": "-"})

    assert orders.due() == ["1"]
    assert model.last_model_request_parameters is None


# ── A function that returns a Decision ──


def decided_in_code(backend, table, to):
    def read_message(order):
        return MessageRead(to=getattr(orders.states, to), reason="A rule matched")

    orders = machine_with(backend, table, read_message)
    table["1"] = new_order()
    orders.start(1)
    return orders


def test_a_function_may_return_a_decision(backend, table):
    orders = decided_in_code(backend, table, "cancelled")
    move = orders.fire(1)
    assert move.decided_by == "code"
    assert move.decision == {"to": "cancelled", "reason": "A rule matched"}
    assert orders.state_of(1) is orders.states.cancelled


def test_a_decision_outside_the_states_edges_raises_and_writes_nothing(backend, table):
    orders = decided_in_code(backend, table, "shipped")
    with pytest.raises(TransitionError, match="placed has edges only to confirmed, cancelled"):
        orders.fire(1)
    assert orders.state_of(1) is orders.states.placed
    assert len(orders.history(1)) == 1


# ── A session answers in the model's place ──


@pytest.fixture
def unreachable(backend, table):
    """A machine whose Decide has no model to call and a prompt that must not run."""

    def prompt(order):
        raise AssertionError("the prompt ran")

    agent = Agent(output_type=MessageRead, instructions="You read customer messages.")
    orders = machine_with(backend, table, Decide(prompt=prompt, agent=agent))
    table["1"] = new_order(message="Yes")
    orders.start(1)
    return orders


@pytest.mark.parametrize(
    "answer",
    [{"to": "confirmed", "reason": "They said yes"}, '{"to": "confirmed", "reason": "They said yes"}'],
)
def test_an_answer_is_applied_without_a_model_or_the_prompt(unreachable, answer):
    move = unreachable.fire(1, answer=answer)

    assert move.to_state == "confirmed"
    assert move.decided_by == "agent"
    assert move.decision == {"to": "confirmed", "reason": "They said yes"}
    assert unreachable.state_of(1) is unreachable.states.confirmed


@pytest.mark.parametrize(
    ("answer", "reason"),
    [
        ({"to": "shipped", "reason": "-"}, "to: Input should be"),
        ({"to": "confirmed"}, "reason: Field required"),
        ("confirmed", "reason: Field required"),
        ({"reason": "-"}, "to: Field required"),
    ],
)
def test_an_answer_the_schema_or_the_edges_do_not_allow_raises_and_writes_nothing(unreachable, answer, reason):
    with pytest.raises(TransitionError, match="the answer for placed doesn't validate") as error:
        unreachable.fire(1, answer=answer)

    assert reason in str(error.value)
    assert unreachable.state_of(1) is unreachable.states.placed
    assert len(unreachable.history(1)) == 1


def test_a_bare_decision_is_answered_with_a_state_alone(backend, table):
    orders = machine_with(backend, table, Decide(prompt=message_prompt))
    table["1"] = new_order(message="No thanks")
    orders.start(1)

    move = orders.fire(1, answer="cancelled")

    assert move.decision == {"to": "cancelled"}
    assert orders.state_of(1) is orders.states.cancelled


def test_an_answer_is_taken_where_the_function_hands_over(unreachable, table):
    unreachable.fire(1, answer={"to": "confirmed", "reason": "They said yes"})

    move = unreachable.fire(1, answer={"to": "cancelled", "reason": "They changed their mind"})

    assert (move.from_state, move.to_state, move.decided_by) == ("confirmed", "cancelled", "agent")


def test_an_answer_is_refused_when_the_function_moves_the_entity_on_a_fact(unreachable, table):
    unreachable.fire(1, answer={"to": "confirmed", "reason": "They said yes"})
    table["1"].tracking_number = "TRK1"

    with pytest.raises(TransitionError, match="confirmed's step moves order:1 to shipped itself"):
        unreachable.fire(1, answer={"to": "cancelled", "reason": "-"})
    with pytest.raises(TransitionError, match="confirmed's step moves order:1 to shipped itself"):
        unreachable.brief(1)

    assert unreachable.state_of(1) is unreachable.states.confirmed
    assert unreachable.fire(1).decided_by == "code"


def test_an_answer_is_refused_when_the_function_says_not_now(unreachable, table):
    unreachable.fire(1, answer={"to": "confirmed", "reason": "They said yes"})
    table["1"].on_hold = True

    with pytest.raises(TransitionError, match='confirmed\'s step says "not now" for order:1'):
        unreachable.fire(1, answer={"to": "cancelled", "reason": "-"})
    with pytest.raises(TransitionError, match='confirmed\'s step says "not now" for order:1'):
        unreachable.brief(1)

    assert unreachable.state_of(1) is unreachable.states.confirmed


def test_a_function_that_does_not_only_read_is_not_run_for_an_answer_or_a_brief(backend, table):
    def charge_and_ship(order):
        order.charged = True
        return orders.states.shipped

    orders = machine_with(backend, table, Decide(prompt=message_prompt), confirmed_step=charge_and_ship)
    confirmed_order(orders, table)

    with pytest.raises(TransitionError, match="confirmed's step is code that doesn't only read"):
        orders.fire(1, answer="shipped")
    with pytest.raises(TransitionError, match="confirmed's step is code that doesn't only read"):
        orders.brief(1)

    assert not hasattr(table["1"], "charged")


def test_a_final_state_takes_no_answer_and_has_no_brief(unreachable):
    unreachable.fire(1, answer={"to": "cancelled", "reason": "-"})

    with pytest.raises(TransitionError, match="order:1 is in cancelled, a final state"):
        unreachable.fire(1, answer="cancelled")
    with pytest.raises(TransitionError, match="order:1 is in cancelled, a final state"):
        unreachable.brief(1)


def test_a_brief_holds_what_the_model_would_get(backend, table):
    def past_orders(ctx: RunContext[object]) -> str:
        """The customer's earlier orders."""
        return "none"

    model = model_saying(to="confirmed", reason="-")
    orders = machine_with(backend, table, reader(model, tools=[past_orders]))
    table["1"] = new_order(message="Yes, please send it")
    orders.start(1)

    brief = orders.brief(1)

    assert brief.state == "placed"
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


def test_a_brief_is_of_the_decide_the_function_hands_over_to(backend, table):
    orders = machine_with(backend, table, reader(model_saying(to="confirmed", reason="-")))
    confirmed_order(orders, table)

    brief = orders.brief(1)

    assert brief.state == "confirmed"
    assert [value for value, _ in brief.choices] == ["cancelled", "shipped"]
    assert brief.prompt == "Where does this message put the order?\nYes"


def test_a_brief_with_no_prompt_says_not_now(backend, table):
    orders = machine_with(backend, table, reader(model_saying(to="confirmed", reason="-")))
    table["1"] = new_order()
    orders.start(1)
    assert orders.brief(1).prompt is None


def test_a_brief_is_written_from_the_entity_in_hand(backend, table):
    orders = machine_with(backend, table, reader(model_saying(to="confirmed", reason="-")))
    table["1"] = new_order()
    orders.start(1)

    brief = orders.brief(1, entity=new_order(message="Cancel it"))

    assert brief.prompt == "Where does this message put the order?\nCancel it"


# ── An effect that runs with the decision ──


def machine_with_effect(backend, table, model, effect) -> Machine:
    agent = Agent(model, output_type=MessageRead, instructions="You read customer messages.")
    read_message = Decide(prompt=message_prompt, agent=agent, effect=effect)
    orders = machine_with(backend, table, read_message)
    table["1"] = new_order(message="Yes, please send it", notes=[])
    orders.start(1)
    return orders


def note_the_reason(order, decision):
    order.notes.append((decision.to.value, decision.reason))


def test_an_effect_runs_with_the_models_decision_before_the_move(backend, table):
    seen = []

    def effect(order, decision):
        seen.append((orders.state_of(1).value, decision.to.value, decision.reason))

    orders = machine_with_effect(backend, table, model_saying(to="confirmed", reason="They said yes"), effect)

    orders.fire(1)

    assert seen == [("placed", "confirmed", "They said yes")]
    assert orders.state_of(1) is orders.states.confirmed


def test_an_effect_runs_with_an_answer_on_the_loaded_entity(backend, table):
    orders = machine_with_effect(backend, table, None, note_the_reason)

    orders.fire(1, answer={"to": "cancelled", "reason": "Changed their mind"})

    assert table["1"].notes == [("cancelled", "Changed their mind")]


def test_an_effect_that_raises_leaves_nothing_written(backend, table):
    def effect(order, decision):
        raise RuntimeError("the carrier is down")

    orders = machine_with_effect(backend, table, model_saying(to="confirmed", reason="-"), effect)

    with pytest.raises(RuntimeError, match="the carrier is down"):
        orders.fire(1)

    assert orders.state_of(1) is orders.states.placed
    assert len(orders.history(1)) == 1


def test_an_effect_does_not_run_for_a_decision_the_edges_refuse(backend, table):
    orders = machine_with_effect(backend, table, None, note_the_reason)

    with pytest.raises(TransitionError):
        orders.fire(1, answer={"to": "shipped", "reason": "-"})

    assert table["1"].notes == []


def test_no_prompt_no_effect(backend, table):
    orders = machine_with_effect(backend, table, model_saying(to="confirmed", reason="-"), note_the_reason)
    table["1"].message = None

    assert orders.fire(1) is None
    assert table["1"].notes == []


def test_a_subclass_asks_the_model_its_own_way(backend, table):
    class OwnWay(Decide):
        decided_by = "llm:own"

        def ask(self, prompt, entity, schema):
            return schema(to="cancelled", reason=f"read: {prompt.splitlines()[-1]}")

    orders = machine_with(backend, table, OwnWay(prompt=message_prompt, schema=MessageRead))
    table["1"] = new_order(message="No thanks")
    orders.start(1)

    move = orders.fire(1)

    assert move.decided_by == "llm:own"
    assert move.decision == {"to": "cancelled", "reason": "read: No thanks"}


def test_an_effect_must_be_a_function():
    with pytest.raises(TypeError, match="effect= must be a function"):
        Decide(prompt=message_prompt, effect="send it")


# ── Declaring a Decide ──


def test_an_agent_whose_output_is_not_a_decision_is_refused():
    with pytest.raises(TypeError, match="a Decide's output type must subclass Decision, got <class 'str'>"):
        Decide(prompt=message_prompt, agent=Agent(TestModel()))


def test_a_decide_takes_an_agent_or_a_schema_not_both():
    agent = Agent(TestModel(), output_type=MessageRead)
    with pytest.raises(TypeError, match="agent= or schema=, not both"):
        Decide(prompt=message_prompt, agent=agent, schema=MessageRead)


def test_a_decide_needs_a_prompt_function():
    with pytest.raises(TypeError, match="prompt= must be a function"):
        Decide(prompt="Read it")
