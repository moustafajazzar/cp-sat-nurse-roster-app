"""Notifications come from rosters and solves, so these tests use the roster tests' scene. Pushes go to a scripted push service."""
import base64
import json
import os
from dataclasses import dataclass

import http_ece
import pytest
import requests
from requests.structures import CaseInsensitiveDict
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ec

from notifications import pushes, pushing

from rosters.tests.conftest import before_june, catalogue, er, mona, solver  # noqa: F401



def _b64(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode()


class Device:
    """A browser's side of a subscription: its own key pair and secret, so a test reads what was sent to it as the browser
    would, after decrypting."""

    def __init__(self, endpoint: str):
        self.endpoint = endpoint
        self._private = ec.generate_private_key(ec.SECP256R1())
        self._auth = os.urandom(16)
        point = self._private.public_key().public_bytes(serialization.Encoding.X962,
                                                         serialization.PublicFormat.UncompressedPoint)
        self.keys = {"p256dh": _b64(point), "auth": _b64(self._auth)}

    def read(self, body: bytes) -> dict:
        return json.loads(http_ece.decrypt(body, private_key=self._private, auth_secret=self._auth, version="aes128gcm"))


@dataclass
class Sent:
    endpoint: str
    headers: CaseInsensitiveDict
    body: bytes


class ScriptedPushService:
    """Stands in for every push service at the HTTP seam: it records each request the sender makes (address, headers, encrypted
    body) and answers what the test scripted for that address, in turn, the last answer repeating; 201 when nothing is scripted.
    An answer is a status, an exception to raise, as an address that cannot be reached does, or a function
    that acts meanwhile and returns or raises one of those."""

    def __init__(self):
        self.sent: list[Sent] = []
        self._answers: dict[str, list] = {}

    def answer(self, endpoint: str, *answers) -> None:
        self._answers[endpoint] = list(answers)

    def post(self, url, data=None, headers=None, timeout=None, **_):
        assert timeout, "every send has a timeout"
        self.sent.append(Sent(url, CaseInsensitiveDict(headers or {}), data))
        script = self._answers.get(url, [201])
        answer = script.pop(0) if len(script) > 1 else script[0]
        if callable(answer) and not isinstance(answer, BaseException):
            answer = answer()  # what happens meanwhile, then the answer
        if isinstance(answer, BaseException):
            raise answer
        response = requests.Response()
        response.status_code, response._content, response.url = answer, b"", url
        return response

    def to(self, endpoint: str) -> list[Sent]:
        return [sent for sent in self.sent if sent.endpoint == endpoint]


@pytest.fixture
def push_service(monkeypatch) -> ScriptedPushService:
    """The scripted push services, with each try again at once: the real waits are minutes, and `run_the_queue` runs until no
    job waits."""
    scripted = ScriptedPushService()
    monkeypatch.setattr(pushing, "push_service", scripted)
    monkeypatch.setattr(pushes, "RETRY_IN", (0, 0, 0))
    return scripted
