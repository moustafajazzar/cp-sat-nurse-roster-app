"""No model field has a fixed date or time as its default. Django's check `fields.W161` warns of one whenever the clock is
within ten seconds of it (in UTC, whatever `TIME_ZONE` says), and that warning in a `manage.py` process's output broke
`test_logs.py` near 00:30 UTC (the close time's default)."""
from datetime import UTC, date, datetime, time
from unittest import mock

import pytest
from django.apps import apps
from django.core import checks


def at(moment: time):
    """Django's clock pinned to `moment` UTC, what the check compares a time default against."""
    now = datetime.combine(date(2026, 11, 13), moment, tzinfo=UTC)
    return mock.patch("django.utils.timezone.now", return_value=now)


@pytest.mark.parametrize("moment", [time(0, 30), time(2, 0)], ids=["close time", "backup time"])
def test_the_checks_raise_no_fixed_default_warning_at_a_default_time(moment):
    with at(moment):
        warnings = checks.run_checks()

    assert [warning.obj for warning in warnings if warning.id == "fields.W161"] == []


def test_no_field_has_a_fixed_date_or_time_default():
    fixed = [f"{model.__name__}.{field.name}" for model in apps.get_models() for field in model._meta.get_fields()
             if isinstance(getattr(field, "default", None), (date, time))]  # datetime is a date

    assert fixed == []
