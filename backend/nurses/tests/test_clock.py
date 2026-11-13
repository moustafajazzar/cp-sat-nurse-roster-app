"""The hospital's date, `membership.today`, and the browser checks' clock that moves it. The clock is a file of a
number of days, named by `E2E_CLOCK_FILE`, which only e2e/compose.e2e.yaml sets: an installation's own compose file passes no
such variable to any service (deploy/tests/operations.sh checks it), and nothing over HTTP reads or writes the file."""
import datetime as dt
import importlib

import pytest
from django.utils import timezone

from nurses import membership


@pytest.fixture
def clock(tmp_path):
    return tmp_path / "days"


def test_off_by_default(monkeypatch):
    monkeypatch.delenv("E2E_CLOCK_FILE", raising=False)
    import config.settings
    assert importlib.reload(config.settings).E2E_CLOCK_FILE is None


def test_the_setting_comes_from_the_environment_only(monkeypatch, clock):
    monkeypatch.setenv("E2E_CLOCK_FILE", str(clock))
    import config.settings
    try:
        assert importlib.reload(config.settings).E2E_CLOCK_FILE == str(clock)
    finally:
        monkeypatch.delenv("E2E_CLOCK_FILE")
        importlib.reload(config.settings)


def test_a_file_is_ignored_while_the_setting_is_off(settings, clock):
    settings.E2E_CLOCK_FILE = None
    clock.write_text("9")
    assert membership.today() == timezone.localdate()


def test_the_clock_moves_the_hospitals_date_by_its_days(settings, clock):
    settings.E2E_CLOCK_FILE = str(clock)
    clock.write_text("3\n")
    assert membership.today() == timezone.localdate() + dt.timedelta(days=3)


def test_each_read_sees_the_file_as_it_is_now(settings, clock):
    """Gunicorn's workers and the queue's worker each read it, so nothing is kept between reads."""
    settings.E2E_CLOCK_FILE = str(clock)
    clock.write_text("1")
    assert membership.today() == timezone.localdate() + dt.timedelta(days=1)
    clock.write_text("0")
    assert membership.today() == timezone.localdate()


@pytest.mark.parametrize("content", [None, "", "  \n"])
def test_no_file_or_an_empty_one_is_the_real_date(settings, clock, content):
    settings.E2E_CLOCK_FILE = str(clock)
    if content is not None:
        clock.write_text(content)
    assert membership.today() == timezone.localdate()


def test_a_file_that_is_not_a_number_of_days_fails_loudly(settings, clock):
    settings.E2E_CLOCK_FILE = str(clock)
    clock.write_text("tomorrow")
    with pytest.raises(ValueError):
        membership.today()
