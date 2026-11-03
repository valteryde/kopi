"""Run one claimed backup in this process.

The worker spawns `manage.py runchild <run id>` in its own session. The
run id is the only argument. Passwords stay in the database.
"""

from django.core.management.base import BaseCommand

from kopi.child import run_supervised


class Command(BaseCommand):
    help = "Run one claimed backup."

    def add_arguments(self, parser):
        parser.add_argument("run_id", type=int)

    def handle(self, *args, **options):
        run_supervised(options["run_id"])
