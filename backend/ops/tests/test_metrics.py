"""The Prometheus endpoint: what a scrape on the internal network reads, and that nothing through Caddy does.
Caddy refuses the path itself (deploy/Caddyfile, proved by `deploy/tests/operations.sh`); the application also refuses any
request Caddy passed on, which always carries `X-Forwarded-For`."""
import datetime as dt
import re

import httpx
import pytest
from django.db import connection
from django.test import Client
from django.utils import timezone

from config.settings import allowed_hosts
from notifications.models import PushAttempt
from ops import backups

pytestmark = pytest.mark.django_db

SAMPLE = re.compile(r"^(?P<name>[a-z_]+)(?P<labels>\{[^}]*\})? (?P<value>\S+)$")


@pytest.fixture(autouse=True)
def solver_answers(monkeypatch, settings):
    settings.SOLVER_URL = "http://solver.test"
    monkeypatch.setattr(httpx, "get", lambda url, headers, timeout: httpx.Response(
        200, json={"version": "0.1.0", "workers": 8}, request=httpx.Request("GET", url)))


def scrape(client=None, **headers):
    return (client or Client()).get("/metrics", headers=headers or None)


def samples(response) -> dict[str, float]:
    found = {}
    for line in response.content.decode().splitlines():
        if line and not line.startswith("#"):
            match = SAMPLE.match(line)
            assert match, line
            found[match["name"] + (match["labels"] or "")] = float(match["value"])
    return found


def test_it_answers_in_prometheus_text_format_with_no_sign_in():
    r = scrape()

    assert r.status_code == 200
    assert r["Content-Type"] == "text/plain; version=0.0.4; charset=utf-8"
    assert r["Cache-Control"] == "no-store"


def test_every_sample_has_its_help_and_type():
    text = scrape().content.decode()
    names = {SAMPLE.match(line)["name"] for line in text.splitlines() if line and not line.startswith("#")}

    for name in names:
        assert f"# HELP {name} " in text and f"# TYPE {name} gauge" in text


def test_what_the_health_page_reads(settings):
    with connection.cursor() as cursor:
        cursor.execute("INSERT INTO procrastinate_jobs (queue_name, task_name, args, status) VALUES "
                       "('default', 'run_solve', '{}', 'todo'), ('default', 'backup', '{}', 'failed')")
    (settings.BACKUPS_DIR / backups.name_for(dt.datetime(2026, 11, 5, 23, tzinfo=dt.UTC))).write_bytes(b"x" * 300)
    (settings.BACKUPS_DIR / backups.name_for(dt.datetime(2026, 11, 4, 23, tzinfo=dt.UTC))).write_bytes(b"x" * 200)

    found = samples(scrape())

    assert found["nurse_roster_database_up"] == 1 and found["nurse_roster_solver_up"] == 1
    assert (found['nurse_roster_queue_jobs{status="waiting"}'], found['nurse_roster_queue_jobs{status="running"}'],
            found['nurse_roster_queue_jobs{status="failed"}'], found["nurse_roster_queue_workers"]) == (1, 0, 1, 0)
    assert found['nurse_roster_solves{state="queued"}'] == 0 and found['nurse_roster_solves{state="failed"}'] == 0
    assert found["nurse_roster_database_size_bytes"] > 0
    assert re.search(r"^nurse_roster_database_size_bytes \d+$", scrape().content.decode(), re.M)  # whole bytes, never 7.6e+06
    assert found["nurse_roster_disk_free_bytes"] > 0
    assert (found["nurse_roster_backups"], found["nurse_roster_backups_size_bytes"]) == (2, 500)
    assert found["nurse_roster_last_backup_timestamp_seconds"] == dt.datetime(2026, 11, 5, 23, tzinfo=dt.UTC).timestamp()


def test_pushes_sent_and_failed_in_the_last_24_hours_by_attempt():
    for outcome in ["sent", "sent", "sent", "unreachable", "refused"]:
        PushAttempt.objects.create(outcome=outcome)
    old = PushAttempt.objects.create(outcome="gone")
    PushAttempt.objects.filter(pk=old.pk).update(at=timezone.now() - dt.timedelta(hours=25))

    found = samples(scrape())

    assert (found['nurse_roster_push_attempts{result="sent"}'], found['nurse_roster_push_attempts{result="failed"}']) == (3, 2)


def test_with_no_push_yet_both_counts_are_zero():
    found = samples(scrape())

    assert (found['nurse_roster_push_attempts{result="sent"}'], found['nurse_roster_push_attempts{result="failed"}']) == (0, 0)


def test_with_no_backup_yet_there_is_no_last_backup():
    assert "nurse_roster_last_backup_timestamp_seconds" not in samples(scrape())


def test_a_solver_down_reads_as_zero(monkeypatch):
    def down(url, headers, timeout):
        raise httpx.ConnectError("no route")
    monkeypatch.setattr(httpx, "get", down)

    assert samples(scrape())["nurse_roster_solver_up"] == 0


def test_nothing_caddy_passed_on_is_answered():
    assert scrape(X_FORWARDED_FOR="203.0.113.9").status_code == 404


def test_a_scraper_on_the_internal_network_names_the_web_service(settings):
    settings.ALLOWED_HOSTS = allowed_hosts("hospital.example")

    assert Client().get("/metrics", HTTP_HOST="web:8000").status_code == 200


def test_the_api_has_no_metrics_and_the_spa_does_not_take_the_path(settings, tmp_path):
    settings.SPA_DIR = tmp_path
    (tmp_path / "index.html").write_text("<html>")

    assert b"<html>" not in scrape().content
    assert Client().get("/api/metrics").status_code == 404
