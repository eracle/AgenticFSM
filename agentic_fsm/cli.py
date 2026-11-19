"""``agfsm``: a machine's moves at the command line.

    agfsm --machine shop.lifecycle:orders show
    agfsm fire order:42 --agent
    agfsm fire order:42 --answer '{"to": "cancelled", "reason": "..."}'

Each command loads one machine and calls one of its methods, so an agent at the CLI can make
every move the code can and no other. ``--json`` prints the result as data. The exit code is 0
for a move made or "not now", 2 for one that can't be made (with the reason on stderr).
"""

import argparse
import json
import os
import sys
from dataclasses import asdict, dataclass
from datetime import datetime
from importlib import import_module
from importlib.util import find_spec

from .backends.base.base import AlreadyStarted, Move
from .conf import ENV_PREFIX, settings
from .machine import Graph, Machine, MachineError, ProgressError, TransitionError

EXIT_OK = 0
EXIT_ERROR = 2

DJANGO_SETTINGS_ENV = "DJANGO_SETTINGS_MODULE"


class CommandError(Exception):
    """A command that can't be run as given: no machine, a bad entity or state."""


@dataclass(frozen=True)
class Result:
    """What a command prints: ``data`` with ``--json``, ``text`` without, and its exit code."""

    data: object
    text: str
    code: int = EXIT_OK


def main(argv: list[str] | None = None) -> int:
    """Run ``agfsm`` with ``argv`` (default: the process's arguments) and return its exit code."""
    args = _parser().parse_args(argv)
    try:
        machine = load_machine(args.machine or settings.MACHINE)
        result = args.command(machine, args)
    except (CommandError, ValueError, TransitionError, AlreadyStarted, LookupError) as error:
        print(f"agfsm: {error}", file=sys.stderr)
        return EXIT_ERROR
    print(json.dumps(result.data, default=_json_default, indent=2) if args.json else result.text)
    return result.code


# ── Arguments ──


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="agfsm", description="A machine's moves at the command line.")
    _add_common(parser, default=None)
    shared = argparse.ArgumentParser(add_help=False)
    _add_common(shared, default=argparse.SUPPRESS)  # given after the command, without erasing one given before
    commands = parser.add_subparsers(required=True, metavar="command")

    show = commands.add_parser("show", parents=[shared], help="the states, their handlers and the edges")
    show.add_argument("--mermaid", action="store_true", help="as a Mermaid state diagram")
    show.set_defaults(command=show_command)

    start = commands.add_parser("start", parents=[shared], help="put an entity in the initial state")
    start.add_argument("entity", help="<machine>:<id>")
    start.set_defaults(command=start_command)

    listing = commands.add_parser("list", parents=[shared], help="the entities in a state")
    listing.add_argument("state", help="a state's stored value")
    listing.set_defaults(command=list_command)

    due = commands.add_parser("due", parents=[shared], help="the entities their state's handler could move now")
    due.add_argument("--tag", help="only the entities in a state carrying this tag")
    due.set_defaults(command=due_command)

    fire = commands.add_parser("fire", parents=[shared], help="run the handler of one entity's state")
    fire.add_argument("entity", help="<machine>:<id>")
    in_place = fire.add_mutually_exclusive_group()
    in_place.add_argument("--agent", action="store_true", help="print what the model would be given")
    in_place.add_argument("--answer", help="decide in the model's place: a JSON object or a state")
    fire.set_defaults(command=fire_command)

    progress = commands.add_parser("progress", parents=[shared], help="move one entity until it rests")
    progress.add_argument("entity", help="<machine>:<id>")
    progress.set_defaults(command=progress_command)

    progress_all = commands.add_parser("progress_all", parents=[shared], help="at most one move for every entity")
    progress_all.add_argument("--tag", help="only the entities in a state carrying this tag")
    progress_all.add_argument("--limit", type=int, help="stop once this many moves are made")
    progress_all.set_defaults(command=progress_all_command)
    return parser


def _add_common(parser: argparse.ArgumentParser, *, default):
    parser.add_argument(
        "--machine",
        default=default,
        help=f"package.module:name (default: the MACHINE setting, {ENV_PREFIX}MACHINE)",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        default=default if default is argparse.SUPPRESS else False,
        help="print the result as JSON",
    )


# ── Loading the machine ──


def load_machine(path: str | None) -> Machine:
    """The ``Machine`` at ``package.module:name``.

    With ``DJANGO_SETTINGS_MODULE`` naming a project installed here, the Django project is set
    up first, since a machine's module usually imports the project's models before anyone can
    ask which backend it uses.
    """
    if not path:
        raise CommandError(f"no machine: pass --machine package.module:name or set {ENV_PREFIX}MACHINE")
    module_path, _, name = path.partition(":")
    if not (module_path and name):
        raise CommandError(f"{path!r}: a machine is named as package.module:name")
    _import_from_working_directory()
    if _django_project():
        from .backends.django.project import setup_project

        setup_project()
    machine = getattr(_import(module_path), name, None)
    if not isinstance(machine, Machine):
        raise CommandError(f"{path!r} is not a Machine")
    return machine


def _django_project() -> bool:
    """Whether ``DJANGO_SETTINGS_MODULE`` names a project this process can set up.

    Django and the settings module must both be installed here. A variable set for another
    program sharing the environment (a container that also runs a Django tool) is not one.
    """
    settings_module = os.environ.get(DJANGO_SETTINGS_ENV)
    return bool(settings_module) and _installed("django") and _installed(settings_module)


def _installed(module: str) -> bool:
    try:
        return find_spec(module) is not None
    except ModuleNotFoundError:  # a parent package isn't installed
        return False


def _import_from_working_directory():
    """Let ``--machine`` name a module in the working directory, as ``python -m`` would."""
    if os.getcwd() not in sys.path:
        sys.path.insert(0, os.getcwd())


def _import(module_path: str):
    try:
        return import_module(module_path)
    except ModuleNotFoundError as error:
        if error.name and not module_path.startswith(error.name):
            raise  # the module exists; something it imports isn't installed
        raise CommandError(f"no module {module_path}") from None


# ── Commands ──


def show_command(machine: Machine, args) -> Result:
    if args.mermaid and args.json:
        raise CommandError("--mermaid and --json: choose one")
    graph = machine.graph()
    return Result(asdict(graph), mermaid(graph) if args.mermaid else _graph_text(graph))


def start_command(machine: Machine, args) -> Result:
    return _move_result(machine.start(_entity_id(machine, args.entity)))


def list_command(machine: Machine, args) -> Result:
    try:
        state = machine.states(args.state)
    except ValueError:
        values = ", ".join(s.value for s in machine.states)
        raise CommandError(f"{args.state!r} is not a state of {machine.name}; they are {values}") from None
    ids = machine.in_state(state)
    return Result(ids, "\n".join(f"{machine.name}:{entity_id}" for entity_id in ids))


def due_command(machine: Machine, args) -> Result:
    ids = machine.due(args.tag)
    return Result(ids, "\n".join(f"{machine.name}:{entity_id}" for entity_id in ids) or "nothing due")


def fire_command(machine: Machine, args) -> Result:
    entity_id = _entity_id(machine, args.entity)
    if args.agent:
        brief = machine.brief(entity_id)
        return Result(asdict(brief), _brief_text(brief, args.entity))
    move = machine.fire(entity_id, answer=args.answer)
    if move is None:
        return Result(None, "not now")
    return _move_result(move)


def progress_command(machine: Machine, args) -> Result:
    return _moves_result(machine.progress(_entity_id(machine, args.entity)))


def progress_all_command(machine: Machine, args) -> Result:
    """The moves made; with an entity whose handler raised, those moves still print, each failure
    goes to stderr, and the exit code is 2."""
    try:
        return _moves_result(machine.progress_all(args.tag, limit=args.limit))
    except ProgressError as error:
        for entity_id, failure in error.failures.items():
            print(f"agfsm: {machine.name}:{entity_id}: {type(failure).__name__}: {failure}", file=sys.stderr)
        moved = _moves_result(error.moves)
        return Result(moved.data, moved.text, EXIT_ERROR)


def _entity_id(machine: Machine, entity: str) -> str:
    """The id in ``<machine>:<id>``, checked against the loaded machine's name."""
    prefix, separator, entity_id = entity.partition(":")
    if not (separator and entity_id):
        raise CommandError(f"{entity!r}: name the entity as {machine.name}:<id>")
    if prefix != machine.name:
        raise CommandError(f"{entity!r} is not a {machine.name}; the machine loaded is {machine.name}")
    return entity_id


def _move_result(move: Move) -> Result:
    return Result(asdict(move), _move_text(move))


def _moves_result(moves: list[Move]) -> Result:
    return Result([asdict(move) for move in moves], "\n\n".join(map(_move_text, moves)) or "nothing moved")


# ── Text ──


def _graph_text(graph: Graph) -> str:
    states = _columns([(s.value, s.kind, s.handler or "-", _described(s)) for s in graph.states])
    edges = _columns([("from", "to"), *graph.edges])
    return f"{graph.name}\n\n{states}\n\n{edges}"


def _described(state) -> str:
    """A state's description, followed by its tags when it has any."""
    return f"{state.description} [{', '.join(state.tags)}]" if state.tags else state.description


def mermaid(graph: Graph) -> str:
    """The machine as a Mermaid state diagram, each state labelled with its description."""
    lines = ["stateDiagram-v2"]
    lines += [f"    {s.value} : {s.description}" for s in graph.states]
    lines += [f"    [*] --> {s.value}" for s in graph.states if s.kind == "initial"]
    lines += [f"    {source} --> {target}" for source, target in graph.edges]
    lines += [f"    {s.value} --> [*]" for s in graph.states if s.kind == "final"]
    return "\n".join(lines)


def _move_text(move: Move) -> str:
    entity = f"{move.machine}:{move.entity}"
    if move.from_state is None:
        return f"{entity} started in {move.to_state}"
    line = f"{entity} {move.from_state} -> {move.to_state}, decided by {move.decided_by}"
    details = {k: v for k, v in (move.decision or {}).items() if k != "to"}
    return "\n".join([line, *(f"  {k}: {v}" for k, v in details.items())])


def _brief_text(brief, entity: str) -> str:
    """The brief laid out for a session to read, ending with how to answer it."""
    if brief.prompt is None:
        return f"not now: there is nothing to ask about {entity} in {brief.state}"
    sections = [
        ("Instructions", "\n\n".join(brief.instructions) or "(none)"),
        ("Prompt", brief.prompt),
        ("Choices", _columns(brief.choices)),
        ("Answer schema", json.dumps(brief.schema, indent=2)),
    ]
    if brief.tools:
        sections.append(("The model's tools (gather the same facts with your own)", _columns(brief.tools)))
    sections.append(("Answer with", f"agfsm fire {entity} --answer '<JSON object>'"))
    body = "\n\n".join(f"{title}:\n{_indent(text)}" for title, text in sections)
    return f"{entity} in {brief.state}\n\n{body}"


def _columns(rows) -> str:
    rows = [tuple(map(str, row)) for row in rows]
    widths = [max(len(row[i]) for row in rows) for i in range(len(rows[0]) - 1)] if rows else []
    return "\n".join("  ".join([*(c.ljust(w) for c, w in zip(row, widths)), row[-1]]) for row in rows)


def _indent(text: str) -> str:
    return "\n".join(f"  {line}" if line else line for line in text.splitlines())


def _json_default(value):
    if isinstance(value, datetime):
        return value.isoformat()
    raise TypeError(f"{type(value).__name__} is not JSON serializable")
