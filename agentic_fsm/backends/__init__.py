"""State backends, laid out and chosen the way Django lays out its database backends.

Each backend is a package whose ``base`` module holds a ``StateBackend``, and one is chosen by
the package's dotted path:

    AGENTIC_FSM_BACKEND=agentic_fsm.backends.django

``locmem`` keeps state in memory, for tests. ``django`` keeps it in a Django project's database
and is the only place Django is imported. A backend from outside this package works the same
way.
"""

from importlib import import_module

from ..conf import ENV_PREFIX, settings
from .base.base import BaseStateBackend

BACKEND_ENV = f"{ENV_PREFIX}BACKEND"


def get_backend(backend: BaseStateBackend | str | None = None) -> BaseStateBackend:
    """The backend to use: an instance as given, or one built from a backend package's path.

    With no argument the path is the ``BACKEND`` setting.
    """
    if isinstance(backend, BaseStateBackend):
        return backend
    path = backend or settings.BACKEND
    if not path:
        raise LookupError(f"no state backend: pass backend= or set {BACKEND_ENV}")
    return _load(path)()


def _load(path: str) -> type[BaseStateBackend]:
    module_path = f"{path}.base"
    try:
        module = import_module(module_path)
    except ModuleNotFoundError as error:
        if error.name and not module_path.startswith(error.name):
            raise  # the backend exists; something it needs (Django, a driver) isn't installed
        raise LookupError(f"{path!r} is not a state backend: no module {module_path}") from None
    backend_class = getattr(module, "StateBackend", None)
    if not (isinstance(backend_class, type) and issubclass(backend_class, BaseStateBackend)):
        raise LookupError(f"{module_path} has no StateBackend subclassing BaseStateBackend")
    return backend_class
