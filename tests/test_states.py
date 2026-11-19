import pytest

from agentic_fsm import FinalState, InitialState, State, States, Handler

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


def test_a_state_keeps_its_handler_and_a_function_is_wrapped_into_one():
    def shelve(crate):
        return Crate.on_shelf

    class Restock(Handler):
        def run(self, entity, targets):
            return None

    restock = Restock()

    class Crate(States):
        packed = InitialState("Packed, in the cold room", handler=shelve)
        on_shelf = State("On the shelf", handler=restock)
        sold = FinalState("Sold")

    assert isinstance(Crate.packed.handler, Handler)
    assert Crate.packed.handler.run(object(), ()) is Crate.on_shelf
    assert Crate.packed.handler.label == "shelve"
    assert Crate.on_shelf.handler is restock
    assert Crate.on_shelf.handler.label == "Restock"
    assert Crate.sold.handler is None


@pytest.mark.parametrize("handler", ["shelve", 3, Handler])
def test_a_handler_must_be_a_function_or_a_handler(handler):
    with pytest.raises(TypeError, match="handler= must be a function of the entity or a Handler"):
        State("On the shelf", handler=handler)


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
