"""Link form. A bad cron or an unknown timezone is refused here."""

from django import forms

from kopi.models import Link, Source, Target
from kopi.schedule import known_cadence, known_timezone


class LinkForm(forms.Form):
    source = forms.ModelChoiceField(
        queryset=Source.objects.none(),
        empty_label=None,
        error_messages={
            "required": "Source is required.",
            "invalid_choice": "Choose a source.",
        },
    )
    target = forms.ModelChoiceField(
        queryset=Target.objects.none(),
        empty_label=None,
        error_messages={
            "required": "Target is required.",
            "invalid_choice": "Choose a target.",
        },
    )
    cadence = forms.CharField(
        max_length=100,
        error_messages={
            "required": "Cron is required.",
            "max_length": "Enter a valid cron expression.",
        },
    )
    timezone = forms.CharField(
        max_length=64,
        error_messages={
            "required": "Timezone is required.",
            "max_length": "Unknown timezone.",
        },
    )
    retention = forms.ChoiceField(
        choices=Link.Retention.choices,
        error_messages={
            "required": "Choose a retention rule.",
            "invalid_choice": "Choose keep-last or delete-older-than.",
        },
    )
    retention_count = forms.IntegerField(
        required=False,
        min_value=1,
        error_messages={
            "invalid": "Enter a whole number.",
            "min_value": "Keep at least 1.",
        },
    )
    retention_days = forms.IntegerField(
        required=False,
        min_value=1,
        error_messages={
            "invalid": "Enter a whole number.",
            "min_value": "Keep at least 1 day.",
        },
    )
    pre_script_path = forms.CharField(
        required=False,
        max_length=1024,
        error_messages={"max_length": "Path is too long."},
    )
    pre_script_timeout = forms.IntegerField(
        min_value=1,
        error_messages={
            "required": "Timeout is required.",
            "invalid": "Timeout must be a whole number of seconds.",
            "min_value": "Timeout must be at least 1 second.",
        },
    )
    post_script_path = forms.CharField(
        required=False,
        max_length=1024,
        error_messages={"max_length": "Path is too long."},
    )
    post_script_timeout = forms.IntegerField(
        min_value=1,
        error_messages={
            "required": "Timeout is required.",
            "invalid": "Timeout must be a whole number of seconds.",
            "min_value": "Timeout must be at least 1 second.",
        },
    )

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["source"].queryset = Source.objects.order_by("name", "pk")
        self.fields["target"].queryset = Target.objects.order_by("name", "pk")

    def clean(self):
        cleaned = super().clean()
        kind = cleaned.get("retention")
        if kind == Link.Retention.COUNT:
            self.errors.pop("retention_days", None)
            if "retention_count" not in self.errors and not cleaned.get("retention_count"):
                self.add_error("retention_count", "Enter how many successful backups to keep.")
        elif kind == Link.Retention.AGE:
            self.errors.pop("retention_count", None)
            if "retention_days" not in self.errors and not cleaned.get("retention_days"):
                self.add_error("retention_days", "Enter how many days to keep.")
        cadence = cleaned.get("cadence")
        zone = cleaned.get("timezone")
        if cadence and not known_cadence(cadence, zone or "UTC"):
            self.add_error("cadence", "Enter a valid cron expression.")
        if zone and not known_timezone(zone):
            self.add_error("timezone", "Unknown timezone.")
        return cleaned
