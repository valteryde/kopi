"""Folder and Postgres source forms. The password is not stored here.

A Postgres source that names a connection pooler is refused until the admin
ticks the acknowledgement. The acknowledgement is not stored, so it is stated
again on every save.
"""

from django import forms

from kopi.models import Source
from kopi.postgres import POOLER_EXPLAINED, pooler_reason


class FolderSourceForm(forms.Form):
    name = forms.CharField(max_length=200, error_messages={"required": "Name is required."})
    root_path = forms.CharField(
        max_length=1024,
        error_messages={"required": "Root path is required."},
    )
    include_rules = forms.CharField(required=False)
    exclude_rules = forms.CharField(required=False)

    def __init__(self, *args, source: Source | None = None, **kwargs):
        self.source = source
        super().__init__(*args, **kwargs)

    def clean_name(self):
        return _unique_name(self.cleaned_data["name"], self.source)


class PostgresSourceForm(forms.Form):
    name = forms.CharField(max_length=200, error_messages={"required": "Name is required."})
    pg_host = forms.CharField(max_length=255, error_messages={"required": "Host is required."})
    pg_port = forms.IntegerField(
        min_value=1,
        max_value=65535,
        error_messages={
            "required": "Port is required.",
            "invalid": "Port must be from 1 to 65535.",
            "min_value": "Port must be from 1 to 65535.",
            "max_value": "Port must be from 1 to 65535.",
        },
    )
    pg_database = forms.CharField(
        max_length=63,
        error_messages={"required": "Database is required."},
    )
    pg_user = forms.CharField(max_length=255, error_messages={"required": "User is required."})
    pg_password = forms.CharField(required=False, strip=False)
    pg_dump_binary = forms.CharField(
        max_length=255,
        error_messages={
            "required": "Dump binary is required.",
            "max_length": "Choose a pg_dump binary from PATH.",
        },
    )
    # Not stored. The admin re-states this on every save that names a pooler.
    acknowledge_pooler = forms.BooleanField(required=False)

    def __init__(self, *args, source: Source | None = None, binaries=None, **kwargs):
        self.source = source
        self.binaries = list(binaries or [])
        self.pooler_reason = ""
        super().__init__(*args, **kwargs)

    def clean_name(self):
        return _unique_name(self.cleaned_data["name"], self.source)

    def clean(self):
        cleaned = super().clean()
        reason = pooler_reason(cleaned.get("pg_host", ""), cleaned.get("pg_port"))
        self.pooler_reason = reason or ""
        if reason and not cleaned.get("acknowledge_pooler"):
            self.add_error(
                "pg_host",
                f"The backup has to reach the Postgres server itself: {reason}. "
                f"{POOLER_EXPLAINED} Tick the box below only when the pooler "
                "resets every backend it returns to the pool.",
            )
        return cleaned

    def clean_pg_password(self):
        password = self.cleaned_data.get("pg_password") or ""
        if self.source is None and password == "":
            raise forms.ValidationError("Password is required.")
        return password

    def clean_pg_dump_binary(self):
        binary = self.cleaned_data["pg_dump_binary"]
        allowed = {path for path, _label in self.binaries}
        if self.source is not None and self.source.pg_dump_binary:
            allowed.add(self.source.pg_dump_binary)
        if binary not in allowed:
            raise forms.ValidationError("Choose a pg_dump binary from PATH.")
        return binary


def _unique_name(name: str, source: Source | None) -> str:
    others = Source.objects.filter(name=name)
    if source is not None and source.pk is not None:
        others = others.exclude(pk=source.pk)
    if others.exists():
        raise forms.ValidationError("A source already has this name.")
    return name
