from django.urls import include, path

urlpatterns = [
    path("", include("kopi.views.accounts")),
    path("", include("kopi.views.runs")),
    path("", include("kopi.views.sources")),
]
