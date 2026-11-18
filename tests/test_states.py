import pytest

from agentic_fsm import FinalState, InitialState, State, States

from .conftest import Order


def test_states_are_a_str_enum_in_declaration_order():
    assert [s.name for s in Order] == ["pending", "paid", "shipped", "delivered", "cancelled"]
    assert Order.paid == "paid"
    assert Order("paid") is Order.paid
    assert str(Order.paid) == "paid"


def test_each_state_keeps_its_description_and_kind():
    assert Order.paid.description == "Payment cleared, waiting to ship"
    assert Order.pending.is_initial and not Order.pending.is_final
    assert Order.delivered.is_final
    assert (Order.pending.kind, Order.paid.kind, Order.cancelled.kind) == ("initial", "state", "final")


def test_a_state_gives_back_its_declaration():
    class Perishable(State):
        def __init__(self, description, *, keeps_days):
            super().__init__(description)
            self.keeps_days = keeps_days

    class Crate(States):
        packed = InitialState("Packed, in the cold room")
        on_shelf = Perishable("On the shelf", keeps_days=3)
        sold = FinalState("Sold")

    assert Crate.on_shelf.declaration.keeps_days == 3
    assert Crate.on_shelf.kind == "state"
    assert type(Crate.packed.declaration) is InitialState


def test_value_overrides_the_stored_value():
    class Legacy(States):
        new = InitialState("Just made", value="NEW")
        done = FinalState("Finished")

    assert Legacy.new.value == "NEW"
    assert Legacy("NEW") is Legacy.new
    assert Legacy.new.name == "new"


def test_a_state_keeps_its_tags_and_has_none_by_default():
    class Tagged(States):
        new = InitialState("Just made")
        packing = State("Being packed", tags=["warehouse", "printer"])
        done = FinalState("Finished")

    assert Tagged.packing.tags == {"warehouse", "printer"}
    assert Tagged.new.tags == frozenset()


@pytest.mark.parametrize("tags", ["warehouse", [""], [3], 3])
def test_tags_must_be_a_collection_of_non_empty_strings(tags):
    with pytest.raises(TypeError, match="tag"):
        State("Being packed", tags=tags)


@pytest.mark.parametrize("description", ["", "   ", None])
def test_a_state_needs_a_description(description):
    with pytest.raises(TypeError, match="needs a description"):
        State(description)


def test_every_attribute_must_be_a_state():
    with pytest.raises(TypeError, match=r"Bad\.loose is 'x'"):

        class Bad(States):
            start = InitialState("Start")
            loose = "x"


def test_two_states_cannot_share_a_value():
    with pytest.raises(TypeError, match=r"Twins\.b and Twins\.a share the value 'a'"):

        class Twins(States):
            a = InitialState("First")
            b = FinalState("Second", value="a")
