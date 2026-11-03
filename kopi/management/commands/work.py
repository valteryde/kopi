"""Supervise backup runs.

The command takes the worker lock, then loops a tick and a sleep. The lock
lasts until the process exits. One tick does not sleep.
"""

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError
from django.utils import timezone

from kopi.worker import serve


class Command(BaseCommand):
    help = "Supervise backup runs."

    def handle(self, *args, **options):
        held = serve(_child, sleep_seconds=settings.WORKER_SLEEP_SECONDS, now=timezone.now)
        if not held:
            raise CommandError("worker lock is held")


def _child(run):
    return None
