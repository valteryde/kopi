"""Create a source. Viewers are refused. The form is goal 4."""

from django.http import HttpResponseBadRequest
from django.urls import path
from django.views.decorators.http import require_POST

from kopi.access import require_admin


@require_admin
@require_POST
def new_source(request):
    return HttpResponseBadRequest("The source form is not available yet.")


urlpatterns = [
    path("sources/new/", new_source, name="source-new"),
]
