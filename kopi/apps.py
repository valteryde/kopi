from django.apps import AppConfig
from django.db.backends.signals import connection_created


def configure_sqlite(sender, connection, **kwargs):
    """WAL lets the web process and the worker write at the same time.

    The busy timeout is also set from DATABASES OPTIONS via sqlite3.connect.
    The pragma makes the same value visible on the open connection.
    """
    if connection.vendor != "sqlite":
        return
    timeout_ms = int(connection.settings_dict["OPTIONS"]["timeout"] * 1000)
    with connection.cursor() as cursor:
        cursor.execute("PRAGMA journal_mode=WAL")
        cursor.execute(f"PRAGMA busy_timeout={timeout_ms}")


class KopiConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "kopi"
    verbose_name = "Kopi"

    def ready(self):
        connection_created.connect(configure_sqlite, dispatch_uid="kopi.configure_sqlite")
