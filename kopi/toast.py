"""Session-based toast notifications.

Call ``set_toast`` before a redirect to flash a short message on the next
page load.  The base template renders the toast container and the CSS
auto-dismisses it.
"""

_KEY = "_toast"


def set_toast(request, text, icon="check"):
    """Store a one-shot toast in the session."""
    request.session[_KEY] = {"text": text, "icon": icon}


def pop_toast(request):
    """Return and remove the toast dict, or *None*."""
    return request.session.pop(_KEY, None)


def context(request):
    """Jinja2 context processor — pops the toast so it appears once."""
    return {"toast": pop_toast(request)}
