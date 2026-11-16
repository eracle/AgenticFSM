"""Every setting and its default. Read them through ``agentic_fsm.conf.settings``."""

# The state backend a machine uses when it's given none: a backend package's dotted path,
# e.g. "agentic_fsm.backends.django". With none set, such a machine raises when it's built.
BACKEND = None

# The database the sqlalchemy backend connects to when it's built with no engine, as SQLAlchemy
# writes URLs, e.g. "postgresql+psycopg://user:password@host/db".
SQLALCHEMY_URL = None

# The machine `agfsm` loads when it's given no --machine: "package.module:name".
MACHINE = None

# The model a Decide with no agent= of its own puts its schema to, as Pydantic AI names models.
DECIDE_MODEL = "anthropic:claude-haiku-5-5"
