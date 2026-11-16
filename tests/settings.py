"""Django settings for the Django backend's tests."""

INSTALLED_APPS = ["agentic_fsm.backends.django"]
DATABASES = {"default": {"ENGINE": "django.db.backends.sqlite3", "NAME": ":memory:"}}
USE_TZ = True
