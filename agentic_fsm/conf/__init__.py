"""Settings, kept the way Django keeps them: every default lives in ``global_settings``, and the
code reads ``settings.NAME``.

    from agentic_fsm.conf import settings
    settings.BACKEND

A setting takes the first value found in:

1. ``settings.configure(BACKEND=...)``, called from code;
2. the environment variable ``AGENTIC_FSM_<NAME>``;
3. the module named by ``AGENTIC_FSM_SETTINGS_MODULE``, which sets the names it wants to change;
4. ``global_settings``.

Values are looked up on each read, so nothing has to be imported in a particular order.
"""

import os
from importlib import import_module

from . import global_settings

ENV_PREFIX = "AGENTIC_FSM_"
SETTINGS_MODULE_ENV = "AGENTIC_FSM_SETTINGS_MODULE"


class Settings:
    """The settings as attributes. A name ``global_settings`` doesn't have is an error."""

    def __init__(self):
        self._configured = {}

    def configure(self, **options):
        """Set settings from code. Raises ``TypeError`` for a name that isn't a setting."""
        for name in options:
            _check_is_setting(name, TypeError)
        self._configured.update(options)

    def __getattr__(self, name):
        _check_is_setting(name, AttributeError)
        if name in self._configured:
            return self._configured[name]
        if ENV_PREFIX + name in os.environ:
            return os.environ[ENV_PREFIX + name]
        module = _settings_module()
        if module is not None and hasattr(module, name):
            return getattr(module, name)
        return getattr(global_settings, name)


def _check_is_setting(name: str, error: type[Exception]):
    if not (name.isupper() and hasattr(global_settings, name)):
        raise error(f"{name!r} is not a setting; they are listed in agentic_fsm/conf/global_settings.py")


def _settings_module():
    path = os.environ.get(SETTINGS_MODULE_ENV)
    return import_module(path) if path else None


settings = Settings()
