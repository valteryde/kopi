"""A new operator: name, login, password, and one role."""

from django import forms
from django.contrib.auth.forms import UsernameField
from django.contrib.auth.models import User
from django.contrib.auth.password_validation import validate_password
from django.contrib.auth.validators import UnicodeUsernameValidator
from django.core.exceptions import ValidationError

from kopi.access import ADMIN, VIEWER


class UserForm(forms.Form):
    name = forms.CharField(
        max_length=150,
        error_messages={"required": "Name is required."},
    )
    login = UsernameField(
        max_length=150,
        validators=[
            UnicodeUsernameValidator(
                message="Login may contain only letters, numbers, and @/./+/-/_."
            )
        ],
        error_messages={"required": "Login is required."},
    )
    password = forms.CharField(
        strip=False,
        error_messages={"required": "Password is required."},
    )
    role = forms.ChoiceField(
        choices=((ADMIN, "Admin"), (VIEWER, "Viewer")),
        error_messages={
            "required": "Role is required.",
            "invalid_choice": "Choose admin or viewer.",
        },
    )

    def clean_login(self):
        login = self.cleaned_data["login"]
        if User.objects.filter(username=login).exists():
            raise forms.ValidationError("A user already has this login.")
        return login

    def clean(self):
        cleaned = super().clean()
        password = cleaned.get("password")
        if password is None:
            return cleaned
        user = User(
            username=cleaned.get("login") or "",
            first_name=cleaned.get("name") or "",
        )
        try:
            validate_password(password, user=user)
        except ValidationError as exc:
            self.add_error("password", exc)
        return cleaned
