"""The one module that knows the Web Push protocol and its library (pywebpush, py-vapid), so either can be replaced without
touching the events, the pages or the worker. The rest of the app asks it to send a kind to a subscription and gets
back an outcome in its own words: sent, gone, refused, unreachable. Tests replace `push_service`, the HTTP side, with a scripted
stand-in, so what reaches it is the real request: its address, headers and encrypted body."""
from __future__ import annotations

import json

import requests
from django.conf import settings
from py_vapid import Vapid
from pywebpush import WebPushException, webpush

from notifications.models import PushOutcome

TIME_TO_LIVE = 24 * 60 * 60
"""A push not delivered within a day is dropped by the push service: the bell keeps the notification."""
TIMEOUT_SECONDS = 10


def _service() -> requests.Session:
    session = requests.Session()
    session.max_redirects = 0  # a push service never redirects, and the allow-list holds only for the address given
    return session


push_service: requests.Session = _service()


def _subject() -> str:
    """Who to contact about this installation, which a push service may ask of the key's owner. Apple refuses one that is
    neither a URL nor `mailto:`. The site's own address when it has a domain; else a placeholder that names no one."""
    return f"https://{settings.DOMAIN}" if settings.DOMAIN else "mailto:push@nurse-roster.invalid"


def outcome_of(status: int) -> PushOutcome:
    if 200 <= status < 300:
        return PushOutcome.SENT
    if status in (404, 410):
        return PushOutcome.GONE
    if status == 429 or status >= 500:
        return PushOutcome.UNREACHABLE
    return PushOutcome.REFUSED


def send(*, endpoint: str, p256dh: str, auth: str, kind: str, private_key: str) -> PushOutcome:
    """Sends the kind code alone, encrypted for the subscription, with a day to live, normal urgency and the kind as topic, so
    an undelivered push of the same kind is replaced at the push service. Never raises."""
    try:
        response = webpush(
            {"endpoint": endpoint, "keys": {"p256dh": p256dh, "auth": auth}},
            data=json.dumps({"kind": kind}),
            vapid_private_key=Vapid.from_pem(private_key.encode()),
            vapid_claims={"sub": _subject()},  # a new dict each time: the library writes the audience into it
            ttl=TIME_TO_LIVE,
            headers={"Urgency": "normal", "Topic": kind},
            timeout=TIMEOUT_SECONDS,
            requests_session=push_service,
        )
        return outcome_of(response.status_code)
    except WebPushException as error:
        return outcome_of(error.response.status_code) if error.response is not None else PushOutcome.REFUSED
    except requests.TooManyRedirects:
        return PushOutcome.REFUSED
    except (requests.RequestException, OSError):
        return PushOutcome.UNREACHABLE
    except (ValueError, TypeError):  # keys the browser gave that cannot be encrypted to
        return PushOutcome.REFUSED
    except Exception:  # noqa: BLE001 - whatever else the library raises: one device's error never stops the job
        return PushOutcome.REFUSED
