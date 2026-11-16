"""Loading a Django project for a process that isn't started by ``manage.py``, such as ``agfsm``."""

import django
from django.apps import apps


def setup_project():
    """Set up the project named by ``DJANGO_SETTINGS_MODULE``, unless it's already set up."""
    if not apps.ready:
        django.setup()
