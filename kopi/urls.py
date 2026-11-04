from django.urls import include, path

urlpatterns = [
    path("", include("kopi.views.accounts")),
    path("", include("kopi.views.runs")),
    path("", include("kopi.views.audit")),
    path("", include("kopi.views.sources")),
    path("", include("kopi.views.targets")),
    path("", include("kopi.views.links")),
    path("", include("kopi.views.users")),
]
