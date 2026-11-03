from django import forms
from django.contrib.auth.forms import UsernameField
from django.contrib.auth.models import User
from django.contrib.auth.password_validation import validate_password
from django.core.exceptions import ValidationError


class SetupForm(forms.Form):
    name = UsernameField(max_length=150, error_messages={"required": "Name is required."})
    password = forms.CharField(
        strip=False,
        error_messages={"required": "Password is required."},
    )

    def clean(self):
        cleaned = super().clean()
        password = cleaned.get("password")
        if password is None:
            return cleaned
        user = User(username=cleaned.get("name") or "")
        try:
            validate_password(password, user=user)
        except ValidationError as exc:
            self.add_error("password", exc)
        return cleaned


class LoginForm(forms.Form):
    name = UsernameField(max_length=150, error_messages={"required": "Name is required."})
    password = forms.CharField(
        strip=False,
        error_messages={"required": "Password is required."},
    )


def first_error(form: forms.Form) -> str:
    for field in form:
        if field.errors:
            return field.errors[0]
    errors = form.non_field_errors()
    if errors:
        return errors[0]
    return ""
