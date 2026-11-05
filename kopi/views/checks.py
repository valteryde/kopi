"""The answer a Test button gets back.

Each screen posts the values that are sitting in its form, so the check runs
before anything is saved. The reply is a fragment the browser swaps into the
inspect panel. Nothing here writes a row, and no line repeats a secret: a
source that needs a password says so in as many words.
"""

from django.shortcuts import render

from kopi.checks import Report


def render_check(request, report: Report):
    """The report as a fragment. A bad line is marked, and the button tone follows."""
    response = render(
        request,
        "checks.jinja",
        {"sections": report.sections(), "passed": report.passed},
    )
    response["Cache-Control"] = "no-store"
    return response


def saved(model, value) -> object | None:
    """The row a form names in a hidden field, or None when the form is a new one."""
    text = str(value or "").strip()
    if not text.isdigit():
        return None
    return model.objects.filter(pk=int(text)).first()
