from django.core.validators import MinValueValidator
from django.db import models

DEFAULT_SCRIPT_TIMEOUT_SECONDS = 300


class Source(models.Model):
    """A Postgres database or a folder tree. `pg_password` is age ciphertext."""

    class Kind(models.TextChoices):
        FOLDER = "folder"
        POSTGRES = "postgres"

    name = models.CharField(max_length=200, unique=True)
    kind = models.CharField(max_length=16, choices=Kind.choices)

    root_path = models.CharField(max_length=1024, blank=True)
    # One glob per line. A blank value matches the whole tree.
    include_rules = models.TextField(blank=True)
    exclude_rules = models.TextField(blank=True)

    pg_host = models.CharField(max_length=255, blank=True)
    pg_port = models.PositiveIntegerField(default=5432)
    pg_database = models.CharField(max_length=63, blank=True)
    pg_user = models.CharField(max_length=255, blank=True)
    pg_password = models.TextField(blank=True)
    pg_dump_binary = models.CharField(max_length=255, blank=True)

    class Meta:
        constraints = [
            models.CheckConstraint(
                name="source_folder_has_root",
                condition=~models.Q(kind="folder") | ~models.Q(root_path=""),
            ),
            models.CheckConstraint(
                name="source_postgres_has_connection",
                condition=(
                    ~models.Q(kind="postgres")
                    | (
                        ~models.Q(pg_host="")
                        & ~models.Q(pg_database="")
                        & ~models.Q(pg_user="")
                        & ~models.Q(pg_password="")
                    )
                ),
            ),
        ]

    def __str__(self):
        return self.name


class Target(models.Model):
    """An S3 bucket. Credential columns are age ciphertext."""

    name = models.CharField(max_length=200, unique=True)
    bucket = models.CharField(max_length=63)
    endpoint_url = models.CharField(max_length=500, blank=True)
    region = models.CharField(max_length=64, blank=True)
    path_style = models.BooleanField(default=False)
    access_key_id = models.TextField(blank=True)
    secret_access_key = models.TextField(blank=True)

    def __str__(self):
        return self.name


class Link(models.Model):
    """One backup job: a source shipped to a target."""

    class Retention(models.TextChoices):
        COUNT = "count"
        AGE = "age"

    source = models.ForeignKey(Source, on_delete=models.PROTECT, related_name="links")
    target = models.ForeignKey(Target, on_delete=models.PROTECT, related_name="links")
    cadence = models.CharField(max_length=100)
    timezone = models.CharField(max_length=64)
    retention_kind = models.CharField(max_length=16, choices=Retention.choices)
    retention_count = models.PositiveIntegerField(
        null=True, blank=True, validators=[MinValueValidator(1)]
    )
    retention_days = models.PositiveIntegerField(
        null=True, blank=True, validators=[MinValueValidator(1)]
    )
    pre_script_path = models.CharField(max_length=1024, blank=True)
    post_script_path = models.CharField(max_length=1024, blank=True)
    pre_script_timeout = models.PositiveIntegerField(
        default=DEFAULT_SCRIPT_TIMEOUT_SECONDS,
        validators=[MinValueValidator(1)],
    )
    post_script_timeout = models.PositiveIntegerField(
        default=DEFAULT_SCRIPT_TIMEOUT_SECONDS,
        validators=[MinValueValidator(1)],
    )

    class Meta:
        constraints = [
            models.CheckConstraint(
                name="link_retention_matches_kind",
                condition=(
                    models.Q(
                        retention_kind="count",
                        retention_count__isnull=False,
                        retention_days__isnull=True,
                    )
                    | models.Q(
                        retention_kind="age",
                        retention_days__isnull=False,
                        retention_count__isnull=True,
                    )
                ),
            ),
        ]

    def __str__(self):
        return f"{self.source_id} -> {self.target_id}"


class Run(models.Model):
    """One execution of a link. The child updates phase and bytes_moved while it runs."""

    class Status(models.TextChoices):
        PENDING = "pending"
        RUNNING = "running"
        SUCCEEDED = "succeeded"
        FAILED = "failed"

    class Phase(models.TextChoices):
        PRE = "pre"
        ARCHIVE = "archive"
        UPLOAD = "upload"
        POST = "post"

    link = models.ForeignKey(Link, on_delete=models.PROTECT, related_name="runs")
    status = models.CharField(max_length=16, choices=Status.choices, default=Status.PENDING)
    phase = models.CharField(max_length=16, choices=Phase.choices, blank=True)
    bytes_moved = models.PositiveBigIntegerField(default=0)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)
    started_at = models.DateTimeField(null=True, blank=True)
    finished_at = models.DateTimeField(null=True, blank=True)
    size = models.PositiveBigIntegerField(null=True, blank=True)
    checksum = models.CharField(max_length=64, blank=True)
    object_name = models.CharField(max_length=1024, blank=True)
    log = models.TextField(blank=True)

    class Meta:
        indexes = [
            models.Index(fields=["link", "status"], name="run_link_status"),
        ]

    def __str__(self):
        return f"{self.link_id} {self.status}"
