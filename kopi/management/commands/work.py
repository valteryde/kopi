"""Supervise backup runs.

The command takes the worker lock, reconciles rows left running, then loops
a tick and a sleep. SIGTERM stops new claims, signals each child session,
and exits. Those rows stay running for the next start.
"""

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError
from django.utils import timezone

from kopi.child import reconcile_open
from kopi.worker import Children, serve


class Command(BaseCommand):
    help = "Supervise backup runs."

    def handle(self, *args, **options):
        children = Children()
        held = serve(
            children.hand,
            sleep_seconds=settings.WORKER_SLEEP_SECONDS,
            now=timezone.now,
            before_tick=children.reap,
            reconcile=reconcile_open,
            on_stop=children.stop,
        )
        if not held:
            raise CommandError("worker lock is held")
