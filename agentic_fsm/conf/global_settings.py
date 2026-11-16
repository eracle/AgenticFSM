"""Every setting and its default. Read them through ``agentic_fsm.conf.settings``."""

# The state backend a machine uses when it's given none: a backend package's dotted path,
# e.g. "agentic_fsm.backends.django". With none set, such a machine raises when it's built.
BACKEND = None

# The machine `agfsm` loads when it's given no --machine: "package.module:name".
MACHINE = None

# The model an Ask with no agent= of its own puts its schema to, as Pydantic AI names models.
ASK_MODEL = "anthropic:claude-haiku-5-5"
