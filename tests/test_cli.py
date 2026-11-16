"""``agfsm`` through ``main(argv)``: each command calls one machine method, on both backends."""

import json

import pytest
from pydantic_ai import Agent, models
from pydantic_ai.models.test import TestModel

from agentic_fsm import Decide, Machine
from agentic_fsm.cli import EXIT_ERROR, EXIT_OK, main, mermaid

from . import machines
from .conftest import new_order
from .test_decide import MessageRead, machine_with, message_prompt
from .test_decide import new_order as new_message_order

models.ALLOW_MODEL_REQUESTS = False

ORDERS = ["--machine", "tests.machines:orders"]
MESSAGES = ["--machine", "tests.machines:messages"]


@pytest.fixture
def cli_orders(monkeypatch, orders):
    monkeypatch.setattr(machines, "orders", orders)
    return orders


@pytest.fixture
def cli_messages(monkeypatch, backend, table):
    """The message machine with the default Decide: no agent of its own and no model key."""
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    read_message = Decide(name="read_message", prompt=message_prompt, schema=MessageRead)
    machine = machine_with(backend, table, read_message)
    monkeypatch.setattr(machines, "messages", machine)
    return machine


def run(capsys, *argv) -> tuple[int, str, str]:
    code = main(list(argv))
    out, err = capsys.readouterr()
    return code, out, err


def run_json(capsys, *argv) -> tuple[int, object]:
    code, out, _ = run(capsys, *argv, "--json")
    return code, json.loads(out)


# ── Loading the machine ──


def test_the_machine_comes_from_the_machine_setting(capsys, monkeypatch, cli_orders):
    monkeypatch.setenv("AGENTIC_FSM_MACHINE", "tests.machines:orders")
    code, out, _ = run(capsys, "show")
    assert code == EXIT_OK
    assert out.startswith("order\n")


def test_machine_may_be_given_before_or_after_the_command(capsys, cli_orders):
    assert run(capsys, *ORDERS, "show")[0] == EXIT_OK
    assert run(capsys, "show", *ORDERS)[0] == EXIT_OK


@pytest.mark.parametrize(
    "path, said",
    [
        (None, "no machine"),
        ("tests.machines", "package.module:name"),
        ("tests.nowhere:orders", "no module tests.nowhere"),
        ("tests.machines:not_a_machine", "is not a Machine"),
        ("tests.machines:missing", "is not a Machine"),
    ],
)
def test_a_machine_that_cannot_be_loaded_is_an_error(capsys, path, said):
    argv = ["show"] if path is None else ["--machine", path, "show"]
    code, out, err = run(capsys, *argv)
    assert code == EXIT_ERROR
    assert out == ""
    assert said in err


# ── show ──


def test_show_prints_the_states_with_their_descriptions_then_the_rows(capsys, cli_orders):
    _, out, _ = run(capsys, *ORDERS, "show")
    states, rows = out.split("\n\n")[1:]
    assert "pending    initial  Placed, not paid yet" in states
    assert "delivered  final    The carrier delivered it" in states
    assert rows.splitlines()[0].split() == ["from", "to", "step"]
    assert ["pending", "paid", "payment_cleared"] in [line.split() for line in rows.splitlines()]


def test_show_json_is_the_graph(capsys, cli_orders):
    code, data = run_json(capsys, *ORDERS, "show")
    assert code == EXIT_OK
    assert data["name"] == "order"
    assert data["states"][0] == {
        "name": "pending",
        "value": "pending",
        "kind": "initial",
        "description": "Placed, not paid yet",
    }
    assert ["pending", "paid", "payment_cleared"] in data["transitions"]


def test_show_mermaid_is_a_state_diagram(capsys, cli_orders):
    _, out, _ = run(capsys, *ORDERS, "show", "--mermaid")
    lines = out.splitlines()
    assert lines[0] == "stateDiagram-v2"
    assert "    [*] --> pending" in lines
    assert "    pending --> paid : payment_cleared" in lines
    assert "    pending : Placed, not paid yet" in lines
    assert "    delivered --> [*]" in lines
    assert out.strip() == mermaid(cli_orders.graph())


def test_show_refuses_mermaid_and_json_together(capsys, cli_orders):
    code, _, err = run(capsys, *ORDERS, "show", "--mermaid", "--json")
    assert code == EXIT_ERROR
    assert "choose one" in err


# ── start and list ──


def test_start_prints_the_move(capsys, cli_orders):
    code, out, _ = run(capsys, *ORDERS, "start", "order:42")
    assert code == EXIT_OK
    assert out.strip() == "order:42 started in pending"
    assert cli_orders.state_of(42).value == "pending"


def test_start_json_is_the_move_with_its_time_as_iso_text(capsys, cli_orders):
    _, data = run_json(capsys, *ORDERS, "start", "order:42")
    assert data["entity"] == "42"
    assert data["to_state"] == "pending"
    assert "T" in data["at"]


def test_starting_twice_is_an_error(capsys, cli_orders):
    run(capsys, *ORDERS, "start", "order:42")
    code, _, err = run(capsys, *ORDERS, "start", "order:42")
    assert code == EXIT_ERROR
    assert err.startswith("agfsm: ")


@pytest.mark.parametrize("entity, said", [("42", "order:<id>"), ("order:", "order:<id>"), ("parcel:42", "not a order")])
def test_an_entity_is_named_by_the_loaded_machine(capsys, cli_orders, entity, said):
    code, _, err = run(capsys, *ORDERS, "start", entity)
    assert code == EXIT_ERROR
    assert said in err
    assert cli_orders.in_state(cli_orders.initial) == []


def test_list_prints_the_entities_in_a_state(capsys, cli_orders):
    cli_orders.start(1)
    cli_orders.start(2)
    code, out, _ = run(capsys, *ORDERS, "list", "pending")
    assert code == EXIT_OK
    assert sorted(out.split()) == ["order:1", "order:2"]
    assert sorted(run_json(capsys, *ORDERS, "list", "pending")[1]) == ["1", "2"]
    assert run_json(capsys, *ORDERS, "list", "paid")[1] == []


def test_list_refuses_a_value_that_is_not_a_state(capsys, cli_orders):
    code, _, err = run(capsys, *ORDERS, "list", "lost")
    assert code == EXIT_ERROR
    assert "pending, paid" in err


# ── fire, progress and progress_all with code steps ──


def test_fire_applies_a_code_step(capsys, cli_orders, table):
    table["42"] = new_order(payment_cleared=True)
    cli_orders.start(42)
    code, out, _ = run(capsys, *ORDERS, "fire", "order:42", "payment_cleared")
    assert code == EXIT_OK
    assert out.strip() == "order:42 pending -> paid by payment_cleared, decided by code"
    assert cli_orders.state_of(42).value == "paid"


def test_fire_says_not_now(capsys, cli_orders, table):
    table["42"] = new_order()
    cli_orders.start(42)
    assert run(capsys, *ORDERS, "fire", "order:42", "payment_cleared")[1].strip() == "not now"
    assert run_json(capsys, *ORDERS, "fire", "order:42", "payment_cleared") == (EXIT_OK, None)


@pytest.mark.parametrize("step", ["ship", "no_such_step"])
def test_fire_of_a_step_that_cannot_be_tried_is_an_error(capsys, cli_orders, table, step):
    table["42"] = new_order()
    cli_orders.start(42)
    code, _, err = run(capsys, *ORDERS, "fire", "order:42", step)
    assert code == EXIT_ERROR
    assert step in err


def test_fire_of_an_entity_not_started_is_an_error(capsys, cli_orders):
    code, _, err = run(capsys, *ORDERS, "fire", "order:42", "payment_cleared")
    assert code == EXIT_ERROR
    assert "hasn't started" in err


def test_progress_all_prints_the_moves_made(capsys, cli_orders, table):
    table["1"] = new_order(payment_cleared=True)
    table["2"] = new_order()
    cli_orders.start(1)
    cli_orders.start(2)
    code, data = run_json(capsys, *ORDERS, "progress_all")
    assert code == EXIT_OK
    assert [(m["entity"], m["to_state"]) for m in data] == [("1", "paid")]


def test_progress_moves_one_entity_until_it_rests(capsys, cli_orders, table):
    table["42"] = new_order(payment_cleared=True, tracking_number="TRK1")
    cli_orders.start(42)
    code, out, _ = run(capsys, *ORDERS, "progress", "order:42")
    assert code == EXIT_OK
    assert out.strip().splitlines() == [
        "order:42 pending -> paid by payment_cleared, decided by code",
        "",
        "order:42 paid -> shipped by ship, decided by code",
    ]
    assert run(capsys, *ORDERS, "progress", "order:42")[1].strip() == "nothing moved"


def test_a_step_outside_its_rows_is_an_error_and_writes_nothing(capsys, backend, table, monkeypatch):
    from .conftest import TRANSITIONS, Order

    def ship(order):
        return Order.delivered  # not a row of ship's

    rows = [row if row[2].__name__ != "ship" else (row[0], row[1], ship) for row in TRANSITIONS]
    machine = Machine(Order, rows, load=lambda order_id: table[order_id], backend=backend)
    monkeypatch.setattr(machines, "orders", machine)
    table["1"] = new_order(payment_cleared=True)
    machine.start(1)
    machine.fire(1, "payment_cleared")
    code, _, err = run(capsys, *ORDERS, "progress_all")
    assert code == EXIT_ERROR
    assert "may move paid only to shipped" in err
    assert machine.state_of(1).value == "paid"


# ── A Decide at the CLI ──


def test_a_decide_fires_from_the_cli_as_from_python(capsys, monkeypatch, backend, table):
    model = TestModel(custom_output_args={"to": "confirmed", "reason": "They said yes"})
    agent = Agent(model, output_type=MessageRead)
    machine = machine_with(backend, table, Decide(name="read_message", prompt=message_prompt, agent=agent))
    monkeypatch.setattr(machines, "messages", machine)
    table["7"] = new_message_order(message="Yes, please send it")
    machine.start(7)
    code, out, _ = run(capsys, *MESSAGES, "fire", "order:7", "read_message")
    assert code == EXIT_OK
    assert out.splitlines() == [
        "order:7 placed -> confirmed by read_message, decided by llm:test:test",
        "  reason: They said yes",
    ]


def test_agent_then_answer_finishes_a_decide_with_no_model_key(capsys, cli_messages, table):
    table["7"] = new_message_order(message="Please cancel it")
    cli_messages.start(7)

    code, brief = run_json(capsys, *MESSAGES, "fire", "order:7", "read_message", "--agent")
    assert code == EXIT_OK
    assert brief["prompt"] == "Where does this message put the order?\nPlease cancel it"
    assert [value for value, _ in brief["choices"]] == ["confirmed", "cancelled"]
    assert brief["schema"]["properties"]["to"]["enum"] == ["confirmed", "cancelled"]

    answer = json.dumps({"to": "cancelled", "reason": "They asked to cancel"})
    code, move = run_json(capsys, *MESSAGES, "fire", "order:7", "read_message", "--answer", answer)
    assert code == EXIT_OK
    assert (move["to_state"], move["decided_by"]) == ("cancelled", "agent")
    assert move["decision"] == {"to": "cancelled", "reason": "They asked to cancel"}
    assert cli_messages.state_of(7).value == "cancelled"


def test_agent_prints_a_brief_to_read(capsys, cli_messages, table):
    table["7"] = new_message_order(message="Please cancel it")
    cli_messages.start(7)
    _, out, _ = run(capsys, *MESSAGES, "fire", "order:7", "read_message", "--agent")
    assert out.startswith("read_message on order:7\n")
    assert "  cancelled  The customer called it off" in out
    assert "agfsm fire order:7 read_message --answer" in out


def test_agent_says_not_now_when_the_prompt_has_nothing_to_ask(capsys, cli_messages, table):
    table["7"] = new_message_order()
    cli_messages.start(7)
    _, out, _ = run(capsys, *MESSAGES, "fire", "order:7", "read_message", "--agent")
    assert out.startswith("not now")


def test_an_answer_outside_the_rows_goes_back_to_the_session(capsys, cli_messages, table):
    table["7"] = new_message_order(message="Ship it")
    cli_messages.start(7)
    answer = json.dumps({"to": "shipped", "reason": "They want it"})
    code, out, err = run(capsys, *MESSAGES, "fire", "order:7", "read_message", "--answer", answer)
    assert (code, out) == (EXIT_ERROR, "")
    assert err.startswith("agfsm: the answer to read_message doesn't validate")
    assert cli_messages.state_of(7).value == "placed"
    assert len(cli_messages.history(7)) == 1


def test_agent_on_a_code_step_is_an_error(capsys, cli_orders, table):
    table["42"] = new_order()
    cli_orders.start(42)
    code, _, err = run(capsys, *ORDERS, "fire", "order:42", "payment_cleared", "--agent")
    assert code == EXIT_ERROR
    assert "is code, not a Decide" in err


def test_agent_and_answer_are_not_given_together(capsys, cli_messages):
    with pytest.raises(SystemExit) as exit_:
        main([*MESSAGES, "fire", "order:7", "read_message", "--agent", "--answer", "cancelled"])
    assert exit_.value.code == EXIT_ERROR
