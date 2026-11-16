"""Machines the CLI tests load by dotted path (``tests.machines:orders``).

Each test sets them to machines built on the backend under test, so the CLI runs on both.
"""

orders = None
messages = None
not_a_machine = "an order"
