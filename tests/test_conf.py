"""Settings: defaults in ``global_settings``, changed from code, the environment or a module."""

import sys
from types import ModuleType

import pytest

from agentic_fsm.conf import Settings, global_settings


@pytest.fixture
def settings(monkeypatch):
    monkeypatch.delenv("AGENTIC_FSM_BACKEND", raising=False)
    monkeypatch.delenv("AGENTIC_FSM_SETTINGS_MODULE", raising=False)
    return Settings()


@pytest.fixture
def settings_module(monkeypatch):
    """A module named by AGENTIC_FSM_SETTINGS_MODULE that changes one setting."""
    module = ModuleType("shop_fsm_settings")
    module.BACKEND = "from.the.module"
    monkeypatch.setitem(sys.modules, "shop_fsm_settings", module)
    monkeypatch.setenv("AGENTIC_FSM_SETTINGS_MODULE", "shop_fsm_settings")
    return module


def test_a_setting_defaults_to_global_settings(settings):
    assert settings.BACKEND is global_settings.BACKEND is None
    assert settings.ASK_MODEL == global_settings.ASK_MODEL == "anthropic:claude-haiku-5-5"


def test_a_settings_module_changes_only_what_it_names(settings, settings_module):
    assert settings.BACKEND == "from.the.module"
    assert settings.ASK_MODEL == global_settings.ASK_MODEL


def test_the_environment_wins_over_the_settings_module(settings, settings_module, monkeypatch):
    monkeypatch.setenv("AGENTIC_FSM_BACKEND", "from.the.environment")
    assert settings.BACKEND == "from.the.environment"


def test_configure_wins_over_everything(settings, settings_module, monkeypatch):
    monkeypatch.setenv("AGENTIC_FSM_BACKEND", "from.the.environment")
    settings.configure(BACKEND="from.code")
    assert settings.BACKEND == "from.code"


def test_an_unknown_setting_is_an_error(settings):
    with pytest.raises(AttributeError, match="'BACKENDS' is not a setting"):
        settings.BACKENDS
    with pytest.raises(TypeError, match="'BACKENDS' is not a setting"):
        settings.configure(BACKENDS="x")


def test_a_settings_module_that_does_not_exist_is_an_error(settings, monkeypatch):
    monkeypatch.setenv("AGENTIC_FSM_SETTINGS_MODULE", "no_such_settings")
    with pytest.raises(ModuleNotFoundError):
        settings.BACKEND
