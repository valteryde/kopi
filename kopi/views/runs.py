from django.shortcuts import render
from django.urls import path


def runs(request):
    return render(request, "runs.jinja")


urlpatterns = [
    path("runs/", runs, name="runs"),
]
