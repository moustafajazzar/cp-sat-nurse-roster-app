"""Pushes on a device: the installation's key pair, and the four calls a device makes. Nothing is sent here."""
import base64
from datetime import UTC, datetime

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ec
from django.core.management import call_command
from django.test import Client
from django.utils import timezone

from accounts.models import Role, User
from audit.models import AuditEntry
from conftest import PASSWORD, post
from notifications.models import PushKeyPair, PushSubscription

pytestmark = pytest.mark.django_db

PUSH = "/api/notifications/push"
PHONE = "https://fcm.googleapis.com/fcm/send/phone-1"
LAPTOP = "https://updates.push.services.mozilla.com/wpush/v2/laptop-1"
KEYS = {"p256dh": "BNcRdreALRFXTkOOUHK1EtK2wtaz5Ry4YfYCA_0QTpQtUbVlUls0VJXg7A8u-Ts1XbjhazAkj7I99e8QcYP7DkM",
        "auth": "tBHItJI5svbpez7KI4CCXg"}


def subscribed(endpoint=PHONE, **extra) -> dict:
    return {"endpoint": endpoint, "keys": KEYS} | extra


def state(client, endpoint=None) -> dict:
    response = post(client, f"{PUSH}/state", {"endpoint": endpoint} if endpoint else {})
    assert response.status_code == 200, response.content
    return response.json()


def turn_on(client, endpoint=PHONE, **extra) -> dict:
    response = post(client, f"{PUSH}/on", subscribed(endpoint, **extra))
    assert response.status_code == 200, response.content
    return response.json()


def turn_off(client, endpoint=PHONE) -> dict:
    response = post(client, f"{PUSH}/off", {"endpoint": endpoint})
    assert response.status_code == 200, response.content
    return response.json()


def turn_off_everywhere(client) -> dict:
    response = post(client, f"{PUSH}/off-everywhere", {})
    assert response.status_code == 200, response.content
    return response.json()


def entries(action=None):
    found = AuditEntry.objects.filter(action__startswith="push.").order_by("id")
    return list(found.filter(action=action) if action else found)


@pytest.fixture
def amal(client_of):
    return client_of("amal", Role.STAFF_NURSE)


@pytest.fixture
def badr(client_of):
    return client_of("badr", Role.STAFF_NURSE)


class TestTheKeyPair:
    def test_first_start_makes_one_and_says_so(self, monkeypatch, capsys):
        monkeypatch.setenv("INITIAL_ADMIN_PASSWORD", "one-time-password")

        call_command("bootstrap")

        assert "Made the installation's push key pair." in capsys.readouterr().out

    def test_a_later_start_keeps_it(self, monkeypatch, amal, capsys):
        monkeypatch.setenv("INITIAL_ADMIN_PASSWORD", "one-time-password")
        call_command("bootstrap")
        first = state(amal)["public_key"]
        capsys.readouterr()

        call_command("bootstrap")

        assert state(amal)["public_key"] == first
        assert "push key pair" not in capsys.readouterr().out

    def test_the_public_key_is_a_p256_point_a_browser_subscribes_with(self, amal):
        key = state(amal)["public_key"]

        point = base64.urlsafe_b64decode(key + "=" * (-len(key) % 4))

        assert "=" not in key and len(point) == 65 and point[0] == 4
        ec.EllipticCurvePublicKey.from_encoded_point(ec.SECP256R1(), point)

    def test_the_private_key_matches_it(self, amal):
        key = state(amal)["public_key"]

        private = serialization.load_pem_private_key(PushKeyPair.objects.get().private_key.encode(), password=None)

        point = private.public_key().public_bytes(serialization.Encoding.X962, serialization.PublicFormat.UncompressedPoint)
        assert base64.urlsafe_b64encode(point).rstrip(b"=").decode() == key


class TestRotation:
    def test_it_replaces_the_pair_and_turns_no_device_off(self, amal):
        before = turn_on(amal)["public_key"]

        call_command("rotate_push_keys")

        after = state(amal, PHONE)
        assert after["public_key"] != before
        assert after["this_device"] is True and after["devices"] == 1

    def test_it_says_when_the_new_pair_was_made(self, amal):
        state(amal)
        PushKeyPair.objects.update(created_at=datetime(2026, 1, 1, tzinfo=UTC))

        call_command("rotate_push_keys")

        assert PushKeyPair.objects.get().created_at > datetime(2026, 11, 1, tzinfo=UTC)

    def test_it_is_audited_from_the_server(self, amal):
        state(amal)

        call_command("rotate_push_keys")

        (entry,) = entries("push.keys_rotated")
        assert entry.actor is None and entry.after == {"from": "the server"}

    def test_it_works_on_an_installation_with_no_pair_yet(self):
        call_command("rotate_push_keys")

        assert len(entries("push.keys_rotated")) == 1


class TestState:
    def test_a_device_with_no_subscription(self, amal):
        answer = state(amal)

        assert answer["this_device"] is False and answer["devices"] == 0 and answer["public_key"]

    def test_a_browser_subscription_the_server_does_not_hold(self, amal):
        assert state(amal, PHONE)["this_device"] is False

    def test_counts_every_device_of_the_user_and_nobody_elses(self, amal, badr):
        turn_on(amal, PHONE)
        turn_on(amal, LAPTOP)
        turn_on(badr, "https://fcm.googleapis.com/fcm/send/badrs")

        answer = state(amal, LAPTOP)

        assert answer["this_device"] is True and answer["devices"] == 2

    def test_another_users_address_is_not_this_device(self, amal, badr):
        turn_on(badr, PHONE)

        assert state(amal, PHONE) | {"public_key": ""} == {"public_key": "", "this_device": False, "devices": 0}


class TestTurningOn:
    def test_a_new_device(self, amal):
        answer = turn_on(amal)

        assert answer["this_device"] is True and answer["devices"] == 1

    def test_it_is_audited_by_the_user_with_nothing_of_the_device(self, amal):
        turn_on(amal)

        (entry,) = entries()
        assert entry.action == "push.turned_on" and entry.actor.username == "amal" and entry.target == "amal"
        assert entry.before is None and entry.after is None and entry.ip is None

    def test_again_for_the_same_address_changes_nothing_and_writes_nothing(self, amal):
        turn_on(amal)

        answer = turn_on(amal)

        assert answer["devices"] == 1 and len(entries()) == 1

    def test_an_address_another_user_holds_moves_to_this_one(self, amal, badr):
        turn_on(badr)

        answer = turn_on(amal)

        assert answer["this_device"] is True and answer["devices"] == 1
        assert state(badr, PHONE)["this_device"] is False and state(badr)["devices"] == 0
        assert [(entry.action, entry.target) for entry in entries()] == [("push.turned_on", "badr"), ("push.turned_on", "amal")]

    def test_a_replaced_address_takes_the_old_ones_place_and_writes_nothing(self, amal):
        turn_on(amal, PHONE)

        answer = turn_on(amal, LAPTOP, replaces=PHONE)

        assert answer["this_device"] is True and answer["devices"] == 1
        assert state(amal, PHONE)["this_device"] is False
        assert len(entries()) == 1

    def test_renewing_an_address_no_longer_held_turns_nothing_on(self, amal):
        """Turned off on every device from another one: the renewal that follows, at this device's start or in its worker,
        must not undo it."""
        turn_on(amal, PHONE)
        turn_off_everywhere(amal)

        answer = turn_on(amal, LAPTOP, replaces=PHONE)

        assert answer["this_device"] is False and answer["devices"] == 0
        assert [entry.action for entry in entries()] == ["push.turned_on", "push.turned_off"]

    def test_replacing_another_users_address_leaves_it_theirs(self, amal, badr):
        turn_on(badr, PHONE)

        turn_on(amal, LAPTOP, replaces=PHONE)

        assert state(badr, PHONE)["this_device"] is True and state(amal)["devices"] == 0

    def test_a_replacement_already_held_ends_the_old_address(self, amal):
        turn_on(amal, PHONE)
        turn_on(amal, LAPTOP)

        answer = turn_on(amal, LAPTOP, replaces=PHONE)

        assert answer["devices"] == 1 and len(entries()) == 2

    @pytest.mark.parametrize("body", [
        subscribed("http://fcm.googleapis.com/fcm/send/x"),
        subscribed("not an address"),
        subscribed("https://" + "a" * 2000),
        {"endpoint": PHONE, "keys": {"p256dh": "not base64url!", "auth": KEYS["auth"]}},
        {"endpoint": PHONE, "keys": {"auth": KEYS["auth"]}},
        {"endpoint": PHONE},
    ])
    def test_what_is_not_a_subscription_is_refused(self, amal, body):
        assert post(amal, f"{PUSH}/on", body).status_code == 422
        assert state(amal)["devices"] == 0

    def test_a_key_longer_than_its_column_is_refused_and_nothing_is_kept(self, amal):
        """The API's limits are the model's, so a key that cannot be stored is a field error, not a database one."""
        response = post(amal, f"{PUSH}/on", {"endpoint": PHONE, "keys": {"p256dh": KEYS["p256dh"], "auth": "a" * 101}})

        assert response.status_code == 422, response.content
        [error] = response.json()["detail"]
        assert error["loc"][-1] == "auth"
        assert state(amal)["devices"] == 0

    def test_keys_as_long_as_their_columns_are_kept(self, amal):
        assert turn_on(amal, PHONE, keys={"p256dh": "b" * 200, "auth": "a" * 100})["devices"] == 1


def device(n: int) -> str:
    return f"https://fcm.googleapis.com/fcm/send/device-{n}"


def held(client) -> list[str]:
    """Which of `device(n)` the client's user holds, by n."""
    return [n for n in range(1, 14) if state(client, device(n))["this_device"]]


class TestTenDevices:
    """At most ten push subscriptions a user: an eleventh device ends the earliest turned on, and nothing refuses
    the new one."""

    def test_ten_are_held(self, amal):
        for n in range(1, 11):
            answer = turn_on(amal, device(n))

        assert answer["devices"] == 10 and held(amal) == list(range(1, 11))

    def test_an_eleventh_ends_the_earliest_turned_on_with_no_entry_of_its_own(self, amal):
        for n in range(1, 12):
            answer = turn_on(amal, device(n))

        assert answer["this_device"] is True and answer["devices"] == 10
        assert held(amal) == list(range(2, 12))
        assert [entry.action for entry in entries()] == ["push.turned_on"] * 11

    def test_the_earliest_is_by_when_it_was_turned_on_not_by_address(self, amal):
        for n in range(1, 11):
            turn_on(amal, device(n))
        PushSubscription.objects.filter(endpoint=device(1)).update(turned_on_at=timezone.now())

        turn_on(amal, device(11))

        assert held(amal) == [1] + list(range(3, 12))

    def test_more_than_ten_held_from_before_are_ended_down_to_ten(self, amal):
        user = User.objects.get(username="amal")
        for n in range(1, 13):
            PushSubscription.objects.create(user=user, endpoint=device(n), **KEYS)

        answer = turn_on(amal, device(13))

        assert answer["devices"] == 10 and held(amal) == list(range(4, 14))

    def test_a_renewal_at_ten_ends_none(self, amal):
        for n in range(1, 11):
            turn_on(amal, device(n))

        answer = turn_on(amal, device(13), replaces=device(5))

        assert answer["devices"] == 10 and held(amal) == [1, 2, 3, 4, 6, 7, 8, 9, 10, 13]

    def test_turning_on_a_held_address_again_ends_none(self, amal):
        for n in range(1, 11):
            turn_on(amal, device(n))

        answer = turn_on(amal, device(1))

        assert answer["devices"] == 10 and held(amal) == list(range(1, 11))

    def test_an_address_moving_from_another_user_counts(self, amal, badr):
        """A shared device taken over is one of the taker's ten, so moving an address cannot get round the limit."""
        for n in range(1, 11):
            turn_on(amal, device(n))
        turn_on(badr, device(11))

        answer = turn_on(amal, device(11))

        assert answer["devices"] == 10 and held(amal) == list(range(2, 12))
        assert state(badr)["devices"] == 0

    def test_each_user_has_ten_of_their_own(self, amal, badr):
        for n in range(1, 11):
            turn_on(amal, device(n))
        turn_on(badr, PHONE)

        assert state(amal)["devices"] == 10 and state(badr)["devices"] == 1


class TestTurningOff:
    def test_this_device(self, amal):
        turn_on(amal, PHONE)
        turn_on(amal, LAPTOP)

        answer = turn_off(amal, PHONE)

        assert answer["this_device"] is False and answer["devices"] == 1
        assert state(amal, LAPTOP)["this_device"] is True

    def test_it_is_audited_by_the_user_with_nothing_of_the_device(self, amal):
        turn_on(amal)

        turn_off(amal)

        entry = entries()[-1]
        assert entry.action == "push.turned_off" and entry.actor.username == "amal" and entry.target == "amal"
        assert entry.before is None and entry.after is None and entry.ip is None

    def test_an_address_not_held_answers_the_same_and_writes_nothing(self, amal):
        turn_on(amal, LAPTOP)

        answer = turn_off(amal, PHONE)

        assert answer | {"public_key": ""} == {"public_key": "", "this_device": False, "devices": 1}
        assert len(entries()) == 1

    def test_another_users_address_stays_theirs_and_the_answer_says_nothing_of_it(self, amal, badr):
        turn_on(badr, PHONE)

        answer = turn_off(amal, PHONE)

        assert answer | {"public_key": ""} == {"public_key": "", "this_device": False, "devices": 0}
        assert state(badr, PHONE)["this_device"] is True
        assert [entry.action for entry in entries()] == ["push.turned_on"]

    def test_every_device_from_any_one(self, amal, badr):
        turn_on(amal, PHONE)
        turn_on(amal, LAPTOP)
        turn_on(badr, "https://fcm.googleapis.com/fcm/send/badrs")

        answer = turn_off_everywhere(amal)

        assert answer["devices"] == 0 and state(amal, LAPTOP)["this_device"] is False
        assert state(badr)["devices"] == 1
        assert [entry.action for entry in entries() if entry.target == "amal"].count("push.turned_off") == 1

    def test_every_device_when_there_is_none_writes_nothing(self, amal):
        assert turn_off_everywhere(amal)["devices"] == 0
        assert entries() == []

    def test_during_a_forced_password_change_so_log_out_can(self, client_of):
        client = client_of("fresh", Role.STAFF_NURSE)
        turn_on(client)
        User.objects.filter(username="fresh").update(must_change_password=True)

        assert post(client, f"{PUSH}/state", {}).status_code == 403
        assert turn_off(client)["devices"] == 0
        assert turn_off_everywhere(client)["devices"] == 0


class TestLogOut:
    def test_turning_this_device_off_then_logging_out_leaves_the_others_on(self, amal):
        turn_on(amal, PHONE)
        turn_on(amal, LAPTOP)

        turn_off(amal, PHONE)
        assert post(amal, "/api/auth/logout", {}).status_code == 204

        laptop = Client()
        assert post(laptop, "/api/auth/login", {"username": "amal", "password": PASSWORD}).status_code == 200
        assert state(laptop, LAPTOP)["devices"] == 1 and state(laptop, PHONE)["this_device"] is False

    def test_logging_out_alone_turns_no_device_off(self, amal):
        """The page turns this device off first; a session that ends by itself turns off none."""
        turn_on(amal)

        post(amal, "/api/auth/logout", {})

        laptop = Client()
        post(laptop, "/api/auth/login", {"username": "amal", "password": PASSWORD})
        assert state(laptop, PHONE)["this_device"] is True


class TestSignedOut:
    @pytest.mark.parametrize(("path", "body"), [
        ("state", {}), ("on", subscribed()), ("off", {"endpoint": PHONE}), ("off-everywhere", {}),
    ])
    def test_each_call_is_a_401(self, path, body):
        assert post(Client(), f"{PUSH}/{path}", body).status_code == 401
