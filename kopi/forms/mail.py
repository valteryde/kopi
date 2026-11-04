"""The SMTP channel. A blank password keeps the stored ciphertext."""

from django import forms
from django.core.exceptions import ValidationError
from django.core.validators import validate_email

from kopi.models import Channel
from kopi.schedule import known_timezone


class MailForm(forms.Form):
    host = forms.CharField(
        max_length=255,
        error_messages={"required": "Host is required.", "max_length": "Host is too long."},
    )
    port = forms.IntegerField(
        min_value=1,
        max_value=65535,
        error_messages={
            "required": "Port is required.",
            "invalid": "Enter a port from 1 to 65535.",
            "min_value": "Enter a port from 1 to 65535.",
            "max_value": "Enter a port from 1 to 65535.",
        },
    )
    from_address = forms.EmailField(
        max_length=254,
        error_messages={
            "required": "From address is required.",
            "invalid": "Enter an email address.",
        },
    )
    recipients = forms.CharField(
        error_messages={"required": "Enter at least one recipient."},
    )
    username = forms.CharField(required=False, max_length=255)
    password = forms.CharField(required=False, strip=False, widget=forms.PasswordInput)
    digest = forms.ChoiceField(
        choices=Channel.Digest.choices,
        error_messages={
            "required": "Choose a digest.",
            "invalid_choice": "Choose monthly, weekly, or daily.",
        },
    )
    hour = forms.IntegerField(
        min_value=0,
        max_value=23,
        error_messages={
            "required": "Hour is required.",
            "invalid": "Enter an hour from 0 to 23.",
            "min_value": "Enter an hour from 0 to 23.",
            "max_value": "Enter an hour from 0 to 23.",
        },
    )
    timezone = forms.CharField(
        max_length=64,
        error_messages={
            "required": "Timezone is required.",
            "max_length": "Unknown timezone.",
        },
    )

    def clean_recipients(self):
        found = []
        for chunk in self.cleaned_data["recipients"].replace("\n", ",").split(","):
            item = chunk.strip()
            if not item:
                continue
            try:
                validate_email(item)
            except ValidationError as exc:
                raise forms.ValidationError("Enter an email address.") from exc
            found.append(item)
        if not found:
            raise forms.ValidationError("Enter at least one recipient.")
        return ", ".join(found)

    def clean_timezone(self):
        zone = self.cleaned_data["timezone"]
        if not known_timezone(zone):
            raise forms.ValidationError("Unknown timezone.")
        return zone
