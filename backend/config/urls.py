from django.conf import settings
from django.http import FileResponse, Http404, HttpRequest
from django.urls import path, re_path

from config.api import api
from ops.metrics import metrics


def spa(request: HttpRequest, path: str = "") -> FileResponse:
    """Every path that is neither the API nor a file of the SPA is a client-side route."""
    index = settings.SPA_DIR / "index.html"
    if not index.is_file():
        raise Http404("the SPA is not built here; in development Vite serves it")
    response = FileResponse(index.open("rb"), content_type="text/html")
    # Asked for again on every visit, so a new deploy's page and its assets are what loads.
    response["Cache-Control"] = "no-cache"
    return response


urlpatterns = [
    path("api/", api.urls),
    path("metrics", metrics),
    re_path(r"^(?!api/)(?P<path>.*)$", spa),
]
