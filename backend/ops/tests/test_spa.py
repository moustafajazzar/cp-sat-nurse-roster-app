from django.test import Client


def test_client_side_routes_get_the_spa(settings, tmp_path):
    (tmp_path / "index.html").write_text("<!doctype html><title>SPA</title>")
    settings.SPA_DIR = tmp_path

    r = Client().get("/some/client/route")

    assert r.status_code == 200 and b"<title>SPA</title>" in b"".join(r.streaming_content)


def test_unknown_api_paths_are_not_the_spa(settings, tmp_path):
    (tmp_path / "index.html").write_text("spa")
    settings.SPA_DIR = tmp_path

    assert Client().get("/api/nope").status_code == 404


def test_a_missing_build_is_a_404_not_a_crash(settings, tmp_path):
    settings.SPA_DIR = tmp_path

    assert Client().get("/").status_code == 404


# The SPA is installable, and a new deploy is never hidden behind a cached page. Every file a browser fetches by a
# fixed name answers `no-cache`, so it asks again each time (a 304 when nothing changed); only the build's hashed assets are
# cached for good. The service worker caches nothing itself (frontend/public/sw.js).


def a_build(settings, tmp_path):
    """A built SPA in a directory of its own, as the image has it, served by WhiteNoise the way the settings say."""
    (tmp_path / "assets").mkdir()
    (tmp_path / "assets" / "index-abc123.js").write_text("console.log(1)")
    (tmp_path / "index.html").write_text("<!doctype html><title>SPA</title>")
    (tmp_path / "sw.js").write_text("self.addEventListener('install', () => self.skipWaiting())")
    (tmp_path / "manifest.webmanifest").write_text('{"name": "Nurse roster"}')
    (tmp_path / "icon-192.png").write_bytes(b"\x89PNG")
    settings.SPA_DIR = tmp_path
    settings.WHITENOISE_ROOT = tmp_path
    return Client()  # after the settings: WhiteNoise reads them when the client builds its handler


def test_the_page_itself_is_never_cached(settings, tmp_path):
    client = a_build(settings, tmp_path)

    for path in ("/", "/my-roster", "/swaps?month=2026-12"):
        assert client.get(path)["Cache-Control"] == "no-cache", path


def test_the_files_with_fixed_names_are_asked_for_again_each_time(settings, tmp_path):
    client = a_build(settings, tmp_path)

    for path in ("/sw.js", "/manifest.webmanifest", "/index.html", "/icon-192.png"):
        response = client.get(path)
        assert response.status_code == 200 and response["Cache-Control"] == "no-cache", path


def test_the_hashed_assets_are_cached_for_good(settings, tmp_path):
    client = a_build(settings, tmp_path)

    assert "immutable" in client.get("/assets/index-abc123.js")["Cache-Control"]


def test_the_worker_and_the_manifest_have_their_media_types(settings, tmp_path):
    client = a_build(settings, tmp_path)

    assert client.get("/sw.js")["Content-Type"].startswith("text/javascript")
    assert client.get("/manifest.webmanifest")["Content-Type"] == "application/manifest+json"


# No page of the app may be framed, by another site or a same-site one. Django says so on every answer it
# makes, WhiteNoise's files included, and Caddy adds `frame-ancestors 'none'` in front of them all (deploy/Caddyfile).


def test_nothing_it_answers_may_be_framed(settings, tmp_path):
    client = a_build(settings, tmp_path)

    for path in ("/", "/unit-roster?unit=u&month=2026-06", "/index.html", "/sw.js", "/assets/index-abc123.js", "/api/nope"):
        assert client.get(path)["X-Frame-Options"] == "DENY", path


def test_not_even_an_error(settings, tmp_path):
    settings.SPA_DIR = tmp_path

    response = Client().get("/")

    assert response.status_code == 404 and response["X-Frame-Options"] == "DENY"
