from django.apps import AppConfig


class AgenticFSMConfig(AppConfig):
    name = "agentic_fsm.backends.django"
    label = "agentic_fsm"
    verbose_name = "Agentic FSM"
    default_auto_field = "django.db.models.BigAutoField"
