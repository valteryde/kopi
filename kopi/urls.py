from django.urls import include, path

urlpatterns = [
    path("", include("kopi.views.runs")),
]
