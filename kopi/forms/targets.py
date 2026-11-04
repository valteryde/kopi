"""S3 target form. Credentials are encrypted by the view, not stored here."""

from django import forms

from kopi.models import Target


class TargetForm(forms.Form):
    name = forms.CharField(max_length=200, error_messages={"required": "Name is required."})
    endpoint = forms.CharField(max_length=500, required=False)
    region = forms.CharField(max_length=64, required=False)
    bucket = forms.CharField(max_length=63, error_messages={"required": "Bucket is required."})
    addressing = forms.ChoiceField(
        choices=(("virtual", "Virtual-hosted"), ("path", "Path-style")),
        error_messages={
            "required": "Addressing is required.",
            "invalid_choice": "Choose path-style or virtual-hosted.",
        },
    )
    access_key = forms.CharField(required=False, strip=False)
    secret = forms.CharField(required=False, strip=False)

    def __init__(self, *args, target: Target | None = None, **kwargs):
        self.target = target
        super().__init__(*args, **kwargs)

    def clean_name(self):
        name = self.cleaned_data["name"]
        others = Target.objects.filter(name=name)
        if self.target is not None and self.target.pk is not None:
            others = others.exclude(pk=self.target.pk)
        if others.exists():
            raise forms.ValidationError("A target already has this name.")
        return name

    def clean_access_key(self):
        return _required_secret(self, "access_key", "Access key is required.")

    def clean_secret(self):
        return _required_secret(self, "secret", "Secret is required.")


def _required_secret(form: TargetForm, field: str, message: str) -> str:
    value = form.cleaned_data.get(field) or ""
    if form.target is None and value == "":
        raise forms.ValidationError(message)
    return value
