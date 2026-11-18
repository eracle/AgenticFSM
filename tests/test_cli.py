"""``agfsm`` through ``main(argv)``: each command calls one machine method, on both backends."""

import json

import pytest
from pydantic_ai import Agent, models
from pydantic_ai.models.test import TestModel

from agentic_fsm import Decide
from agentic_fsm.cli import EXIT_ERROR, EXIT_OK, main, mermaid

from . import machines
from .conftest import new_order, order_machine
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
    read_message = Decide(prompt=message_prompt, schema=MessageRead)
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


def test_show_prints_the_states_with_their_steps_and_descriptions_then_the_edges(capsys, cli_orders):
    _, out, _ = run(capsys, *ORDERS, "show")
    states, edges = out.split("\n\n")[1:]
    assert "pending    initial  await_payment   Placed, not paid yet" in states
    assert "delivered  final    -               The carrier delivered it" in states
    assert edges.splitlines()[0].split() == ["from", "to"]
    assert ["pending", "paid"] in [line.split() for line in edges.splitlines()]


def test_show_json_is_the_graph(capsys, cli_orders):
    code, data = run_json(capsys, *ORDERS, "show")
    assert code == EXIT_OK
    assert data["name"] == "order"
    assert data["states"][0] == {
        "name": "pending",
        "value": "pending",
        "kind": "initial",
        "description": "Placed, not paid yet",
        "tags": [],
        "step": "await_payment",
    }
    assert ["pending", "paid"] in data["edges"]


def test_show_mermaid_is_a_state_diagram(capsys, cli_orders):
    _, out, _ = run(capsys, *ORDERS, "show", "--mermaid")
    lines = out.splitlines()
    assert lines[0] == "stateDiagram-v2"
    assert "    [*] --> pending" in lines
    assert "    pending --> paid" in lines
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


def test_fire_runs_the_step_of_the_entitys_state(capsys, cli_orders, table):
    table["42"] = new_order(payment_cleared=True)
    cli_orders.start(42)
    code, out, _ = run(capsys, *ORDERS, "fire", "order:42")
    assert code == EXIT_OK
    assert out.strip() == "order:42 pending -> paid, decided by code"
    assert cli_orders.state_of(42).value == "paid"


def test_fire_says_not_now(capsys, cli_orders, table):
    table["42"] = new_order()
    cli_orders.start(42)
    assert run(capsys, *ORDERS, "fire", "order:42")[1].strip() == "not now"
    assert run_json(capsys, *ORDERS, "fire", "order:42") == (EXIT_OK, None)


def test_fire_of_a_finished_entity_is_an_error(capsys, cli_orders, table):
    table["42"] = new_order(cancel_requested=True)
    cli_orders.start(42)
    cli_orders.fire(42)
    code, _, err = run(capsys, *ORDERS, "fire", "order:42")
    assert code == EXIT_ERROR
    assert "a final state" in err


def test_fire_takes_no_step_name(capsys, cli_orders, table):
    table["42"] = new_order(payment_cleared=True)
    cli_orders.start(42)
    with pytest.raises(SystemExit) as exit_:
        main([*ORDERS, "fire", "order:42", "await_payment"])
    assert exit_.value.code == EXIT_ERROR
    assert cli_orders.state_of(42).value == "pending"


def test_fire_of_an_entity_not_started_is_an_error(capsys, cli_orders):
    code, _, err = run(capsys, *ORDERS, "fire", "order:42")
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
        "order:42 pending -> paid, decided by code",
        "",
        "order:42 paid -> shipped, decided by code",
    ]
    assert run(capsys, *ORDERS, "progress", "order:42")[1].strip() == "nothing moved"


def test_a_step_outside_its_states_edges_is_an_error_and_writes_nothing(capsys, backend, table, monkeypatch):
    machine = order_machine(backend, table, paid=lambda order: machine.states.delivered)  # paid has no edge there
    monkeypatch.setattr(machines, "orders", machine)
    table["1"] = new_order(payment_cleared=True)
    machine.start(1)
    machine.fire(1)
    code, _, err = run(capsys, *ORDERS, "progress_all")
    assert code == EXIT_ERROR
    assert "paid has edges only to cancelled, shipped" in err
    assert machine.state_of(1).value == "paid"


def test_due_lists_what_a_step_could_move_and_moves_nothing(capsys, backend, table, monkeypatch):
    from .test_machine_moves import checked_orders

    machine = checked_orders(backend, table)
    monkeypatch.setattr(machines, "orders", machine)
    table["1"] = new_order(payment_cleared=True)
    table["2"] = new_order()
    machine.start_all([1, 2])

    code, out, _ = run(capsys, *ORDERS, "due")

    assert code == EXIT_OK
    assert out.strip() == "order:1"
    assert machine.in_state(machine.states.pending) == ["1", "2"]


def test_a_tag_narrows_due_and_progress_all_and_show_prints_it(capsys, backend, table, monkeypatch):
    from .test_machine_moves import parcels_in_every_state

    monkeypatch.setattr(machines, "orders", parcels_in_every_state(backend, table))

    assert run(capsys, *ORDERS, "due", "--tag", "carrier")[1].strip() == "parcel:4"
    code, data = run_json(capsys, *ORDERS, "progress_all", "--tag", "warehouse")
    assert code == EXIT_OK
    assert [(m["entity"], m["to_state"]) for m in data] == [("2", "labelling"), ("3", "shipped")]
    assert "Packed: the warehouse labels it [printer, warehouse]" in run(capsys, *ORDERS, "show")[1]
    code, data = run_json(capsys, *ORDERS, "progress_all", "--limit", "1")
    assert [m["entity"] for m in data] == ["1"]
    assert run(capsys, *ORDERS, "progress_all", "--limit", "0")[0] == EXIT_ERROR
    code, _, err = run(capsys, *ORDERS, "due", "--tag", "nobody")
    assert code == EXIT_ERROR
    assert "no state of parcel is tagged 'nobody'" in err


def test_a_django_settings_module_not_installed_here_is_left_alone(capsys, cli_orders, monkeypatch):
    """A container that also runs a Django tool sets the variable for it, not for this machine."""
    monkeypatch.setenv("DJANGO_SETTINGS_MODULE", "some_other_tool.settings")

    code, out, _ = run(capsys, *ORDERS, "show")

    assert code == EXIT_OK
    assert out.startswith("order\n")


def test_progress_all_prints_the_moves_made_beside_a_failure(capsys, backend, table, monkeypatch):
    def pending(order):
        if order.payment_cleared == "provider down":
            raise RuntimeError("the payment provider is down")
        return machine.states.paid

    machine = order_machine(backend, table, pending=pending)
    monkeypatch.setattr(machines, "orders", machine)
    table["1"], table["2"] = new_order(payment_cleared="provider down"), new_order()
    machine.start_all([1, 2])

    code, out, err = run(capsys, *ORDERS, "progress_all")

    assert code == EXIT_ERROR
    assert out.strip() == "order:2 pending -> paid, decided by code"
    assert err.strip() == "agfsm: order:1: RuntimeError: the payment provider is down"


# ── A Decide at the CLI ──


def test_a_decide_fires_from_the_cli_as_from_python(capsys, monkeypatch, backend, table):
    model = TestModel(custom_output_args={"to": "confirmed", "reason": "They said yes"})
    agent = Agent(model, output_type=MessageRead)
    machine = machine_with(backend, table, Decide(prompt=message_prompt, agent=agent))
    monkeypatch.setattr(machines, "messages", machine)
    table["7"] = new_message_order(message="Yes, please send it")
    machine.start(7)
    code, out, _ = run(capsys, *MESSAGES, "fire", "order:7")
    assert code == EXIT_OK
    assert out.splitlines() == [
        "order:7 placed -> confirmed, decided by llm:test:test",
        "  reason: They said yes",
    ]


def test_agent_then_answer_finishes_a_decide_with_no_model_key(capsys, cli_messages, table):
    table["7"] = new_message_order(message="Please cancel it")
    cli_messages.start(7)

    code, brief = run_json(capsys, *MESSAGES, "fire", "order:7", "--agent")
    assert code == EXIT_OK
    assert brief["state"] == "placed"
    assert brief["prompt"] == "Where does this message put the order?\nPlease cancel it"
    assert [value for value, _ in brief["choices"]] == ["confirmed", "cancelled"]
    assert brief["schema"]["properties"]["to"]["enum"] == ["confirmed", "cancelled"]

    answer = json.dumps({"to": "cancelled", "reason": "They asked to cancel"})
    code, move = run_json(capsys, *MESSAGES, "fire", "order:7", "--answer", answer)
    assert code == EXIT_OK
    assert (move["to_state"], move["decided_by"]) == ("cancelled", "agent")
    assert move["decision"] == {"to": "cancelled", "reason": "They asked to cancel"}
    assert cli_messages.state_of(7).value == "cancelled"


def test_agent_prints_a_brief_to_read(capsys, cli_messages, table):
    table["7"] = new_message_order(message="Please cancel it")
    cli_messages.start(7)
    _, out, _ = run(capsys, *MESSAGES, "fire", "order:7", "--agent")
    assert out.startswith("order:7 in placed\n")
    assert "  cancelled  The customer called it off" in out
    assert "agfsm fire order:7 --answer" in out


def test_agent_says_not_now_when_the_prompt_has_nothing_to_ask(capsys, cli_messages, table):
    table["7"] = new_message_order()
    cli_messages.start(7)
    _, out, _ = run(capsys, *MESSAGES, "fire", "order:7", "--agent")
    assert out.startswith("not now")


def test_an_answer_outside_the_edges_goes_back_to_the_session(capsys, cli_messages, table):
    table["7"] = new_message_order(message="Ship it")
    cli_messages.start(7)
    answer = json.dumps({"to": "shipped", "reason": "They want it"})
    code, out, err = run(capsys, *MESSAGES, "fire", "order:7", "--answer", answer)
    assert (code, out) == (EXIT_ERROR, "")
    assert err.startswith("agfsm: the answer for placed doesn't validate")
    assert cli_messages.state_of(7).value == "placed"
    assert len(cli_messages.history(7)) == 1


def test_agent_on_a_state_whose_step_is_code_is_an_error(capsys, cli_orders, table):
    table["42"] = new_order()
    cli_orders.start(42)
    code, _, err = run(capsys, *ORDERS, "fire", "order:42", "--agent")
    assert code == EXIT_ERROR
    assert "pending's step is code that doesn't only read" in err


def test_agent_and_answer_are_not_given_together(capsys, cli_messages):
    with pytest.raises(SystemExit) as exit_:
        main([*MESSAGES, "fire", "order:7", "--agent", "--answer", "cancelled"])
    assert exit_.value.code == EXIT_ERROR
