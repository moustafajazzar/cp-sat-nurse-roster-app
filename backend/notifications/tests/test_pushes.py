"""Pushes sent: which change is pushed to whom, what reaches the push service, and what its answers do. The changes are
made through the API, the job is the real worker's, and the push services are scripted at their HTTP seam
(`conftest.ScriptedPushService`), so what a test reads is what a browser would decrypt."""
import datetime as dt

import pytest
import requests
from django.db import transaction
from procrastinate.contrib.django.models import ProcrastinateJob

from accounts.models import Role, User
from conftest import post
from notifications import sending
from notifications.models import PushAttempt, PushSubscription
from notifications.tests.conftest import Device
from notifications.tests.test_events import as_user
from nurses.models import NurseProfile, Placement
from requesting.tasks import open_and_close_request_windows
from requesting.tests.conftest import asked, opened
from rosters.tests.test_review import approve, approved, carla, dina, ok, reject, solved, submit  # noqa: F401
from solves.tests.conftest import empty_queue, run_the_queue  # noqa: F401
from swaps.tests.conftest import SOUND_SWAP, acted, decided, on_day, proposed  # noqa: F401
from units.models import Unit

pytestmark = [pytest.mark.django_db(transaction=True), pytest.mark.usefixtures("empty_queue")]

PUSH = "/api/notifications/push"
DAY = 24 * 60 * 60


@pytest.fixture(autouse=True)
def _scene(before_june, solver, push_service, mona, er, carla, dina):
    """ER's four nurses, its manager Mona, Carla the CNO and Dina the Deputy CNO."""


def phone(name: str) -> Device:
    return Device(f"https://fcm.googleapis.com/fcm/send/{name}-phone")


def turned_on(client, device: Device) -> Device:
    response = post(client, f"{PUSH}/on", {"endpoint": device.endpoint, "keys": device.keys})
    assert response.status_code == 200, response.content
    return device


def on(username: str) -> Device:
    """`username` signed in on a phone of their own with pushes on."""
    return turned_on(as_user(username), phone(username))


def kinds_to(push_service, device: Device) -> list[str]:
    """Every push the device was sent, as it reads them."""
    return [device.read(sent.body)["kind"] for sent in push_service.to(device.endpoint)]


def everyone_on(*usernames) -> dict[str, Device]:
    return {username: on(username) for username in usernames}


def pushed(push_service, devices: dict[str, Device]) -> dict[str, list[str]]:
    """Runs the queue, then what reached each device; those sent nothing left out."""
    run_the_queue()
    found = {username: kinds_to(push_service, device) for username, device in devices.items()}
    return {username: kinds for username, kinds in found.items() if kinds}


def subscription(device: Device) -> PushSubscription | None:
    return PushSubscription.objects.filter(endpoint=device.endpoint).first()


SAMPLE = ("Amal", "Badr", "Dana", "Emad", "mona", "carla", "dina")


class TestWhatIsPushedToWhom:
    def test_a_swap_proposed_reaches_each_colleague_in_it_and_nobody_else(self, mona, er, solver, carla, push_service):
        approved(mona, carla, er, solver)
        devices = everyone_on(*SAMPLE)

        proposed(as_user("Badr"), *SOUND_SWAP)

        assert pushed(push_service, devices) == {"Emad": ["swap_to_answer"]}

    def test_a_swap_every_colleague_accepted_reaches_the_manager_alone(self, mona, er, solver, carla, push_service):
        approved(mona, carla, er, solver)
        swap = proposed(as_user("Badr"), *SOUND_SWAP)
        run_the_queue()
        devices = everyone_on(*SAMPLE)

        acted(as_user("Emad"), swap["id"], "accept")

        assert pushed(push_service, devices) == {"mona": ["swap_to_decide"]}

    def test_a_window_opened_reaches_the_staff_nurses_of_its_month(self, mona, er, push_service):
        devices = everyone_on(*SAMPLE)

        opened(mona, er)

        assert pushed(push_service, devices) == {name: ["requests_open"] for name in ("Amal", "Badr", "Dana", "Emad")}

    def test_a_window_closed_with_requests_waiting_reaches_the_manager(self, mona, er, push_service, on_day):
        opened(mona, er)
        asked(as_user("Amal"), 3)
        run_the_queue()
        devices = everyone_on(*SAMPLE)

        on_day(2026, 5, 26)
        open_and_close_request_windows(timestamp=0)

        assert pushed(push_service, devices) == {"mona": ["requests_to_decide"]}

    def test_a_roster_submitted_reaches_the_cno_and_the_deputy(self, mona, er, solver, push_service, carla, dina):
        solved(mona, er, solver)
        devices = everyone_on(*SAMPLE)

        ok(submit(mona, er))

        assert pushed(push_service, devices) == {"carla": ["roster_to_approve"], "dina": ["roster_to_approve"]}

    def test_a_roster_rejected_reaches_its_manager(self, mona, er, solver, push_service, dina):
        solved(mona, er, solver)
        ok(submit(mona, er))
        run_the_queue()
        devices = everyone_on(*SAMPLE)

        ok(reject(dina, er, "Too many nights."))

        assert pushed(push_service, devices) == {"mona": ["roster_sent_back"]}

    def test_every_other_change_is_on_the_bell_alone(self, mona, er, solver, push_service, carla):
        """A solve's end, an approval, a swap accepted by one and approved, the roster changed: only the two swap steps with a
        deadline are pushed, each sent while it still waited."""
        devices = everyone_on(*SAMPLE)
        approved(mona, carla, er, solver)
        swap = proposed(as_user("Badr"), *SOUND_SWAP)
        run_the_queue()
        acted(as_user("Emad"), swap["id"], "accept")  # Badr is told of the acceptance, Mona that it awaits her
        run_the_queue()
        decided(mona, er, swap["id"], "approve")  # the swap's people and the month's nurses are told

        assert pushed(push_service, devices) == {"Emad": ["swap_to_answer"], "mona": ["swap_to_decide"]}

    def test_a_change_rolled_back_pushes_nothing(self, mona, er, push_service):
        devices = everyone_on(*SAMPLE)
        unit = Unit.objects.get(pk=er.pk)

        with pytest.raises(RuntimeError), transaction.atomic():
            sending.roster_rejected(unit, dt.date(2026, 6, 1), User.objects.get(username="dina"))
            raise RuntimeError("the change failed after telling")

        assert pushed(push_service, devices) == {}

    def test_one_job_tells_forty_phones_once_each(self, mona, er, make_user, push_service):
        devices = everyone_on("Amal", "Badr", "Dana", "Emad")
        for number in range(36):
            user = make_user(f"N{number:02d}", Role.STAFF_NURSE)
            nurse = NurseProfile.objects.create(user=user, gender="F", start_date=dt.date(2025, 1, 1))
            Placement.objects.create(nurse=nurse, unit=er, from_month=dt.date(2026, 1, 1))
            devices[user.username] = on(user.username)

        opened(mona, er)

        assert pushed(push_service, devices) == {name: ["requests_open"] for name in devices}
        assert len(push_service.sent) == 40

    def test_a_manager_with_a_nurse_profile_gets_the_managers_kinds_and_the_bells_rule(self, mona, er, solver, push_service,
                                                                                         dina):
        """A manager's cells are never swapped and a manager makes no requests (section 3), so no nurse's kind is theirs on the
        bell, and none is pushed: the push follows the bell."""
        nurse = NurseProfile.objects.create(user=User.objects.get(username="mona"), gender="F", start_date=dt.date(2025, 1, 1))
        Placement.objects.create(nurse=nurse, unit=er, from_month=dt.date(2026, 1, 1))
        devices = everyone_on("mona")

        opened(mona, er)
        solved(mona, er, solver)
        ok(submit(mona, er))
        ok(reject(dina, er, "Too many nights."))

        assert pushed(push_service, devices) == {"mona": ["roster_sent_back"]}

    def test_the_system_admin_is_pushed_nothing(self, mona, er, solver, push_service, carla, dina, signed_in):
        admin = signed_in(Role.SYSTEM_ADMIN, username="sysadmin")
        devices = {"sysadmin": turned_on(admin, phone("sysadmin"))}

        opened(mona, er)
        solved(mona, er, solver)
        ok(submit(mona, er))
        ok(reject(dina, er, "Too many nights."))

        assert pushed(push_service, devices) == {}



class TestWhatReachesThePushService:
    def test_the_kind_code_alone_with_a_day_to_live_normal_urgency_and_the_kind_as_topic(self, mona, er, push_service):
        amal = on("Amal")

        opened(mona, er)
        run_the_queue()

        [sent] = push_service.to(amal.endpoint)
        assert amal.read(sent.body) == {"kind": "requests_open"}
        assert (sent.headers["TTL"], sent.headers["Urgency"], sent.headers["Topic"]) == (str(DAY), "normal", "requests_open")
        assert sent.headers["Content-Encoding"] == "aes128gcm"
        assert sent.headers["Authorization"].startswith("vapid t=")

    def test_the_installations_own_key_signs_it(self, mona, er, push_service):
        amal = on("Amal")
        public_key = ok(post(as_user("Amal"), f"{PUSH}/state", {}))["public_key"]

        opened(mona, er)
        run_the_queue()

        [sent] = push_service.to(amal.endpoint)
        assert sent.headers["Authorization"].endswith(f"k={public_key}")


class TestStillWaits:
    """Before each send the job reads the thing again, as the pages read it: one that waits no more is sent nothing, and that is
    no failure of the subscription's and is tried again never."""

    def nothing_sent(self, push_service, *devices) -> None:
        run_the_queue()
        assert [push_service.to(device.endpoint) for device in devices] == [[] for _ in devices]
        assert all(subscription(device).failures_in_a_row == 0 for device in devices)
        assert PushAttempt.objects.count() == 0

    def test_a_swap_answered_before_the_push_left(self, mona, er, solver, carla, push_service):
        approved(mona, carla, er, solver)
        run_the_queue()
        emad = on("Emad")

        swap = proposed(as_user("Badr"), *SOUND_SWAP)
        acted(as_user("Emad"), swap["id"], "reject")

        self.nothing_sent(push_service, emad)

    def test_a_swap_whose_first_date_has_passed(self, mona, er, solver, carla, push_service, on_day):
        approved(mona, carla, er, solver)
        run_the_queue()
        emad = on("Emad")

        on_day(2026, 6, 1)
        proposed(as_user("Badr"), *SOUND_SWAP)
        on_day(2026, 6, 10)  # its date, the 9th, passed before the job ran; the hourly job has not stored it yet

        self.nothing_sent(push_service, emad)

    def test_a_swap_decided_before_the_push_left(self, mona, er, solver, carla, push_service):
        approved(mona, carla, er, solver)
        swap = proposed(as_user("Badr"), *SOUND_SWAP)
        run_the_queue()
        manager = on("mona")

        acted(as_user("Emad"), swap["id"], "accept")
        decided(mona, er, swap["id"], "approve")

        self.nothing_sent(push_service, manager)

    def test_a_window_closed_early_before_the_push_left(self, mona, er, push_service):
        amal = on("Amal")

        window = opened(mona, er)
        ok(post(mona, f"/api/units/{er.pk}/request-windows/{window['id']}/close", {}))

        self.nothing_sent(push_service, amal)

    def test_a_window_whose_requests_were_all_decided_before_the_push_left(self, mona, er, push_service, on_day):
        opened(mona, er)
        request = asked(as_user("Amal"), 3)
        run_the_queue()
        manager = on("mona")

        on_day(2026, 5, 26)
        open_and_close_request_windows(timestamp=0)
        ok(post(mona, f"/api/units/{er.pk}/requests/{request['id']}/accept", {"reason": ""}))

        self.nothing_sent(push_service, manager)

    def test_a_roster_decided_before_the_push_left(self, mona, er, solver, carla, push_service):
        solved(mona, er, solver)
        approvers = [on("carla"), on("dina")]

        ok(submit(mona, er))
        ok(approve(carla, er))

        self.nothing_sent(push_service, *approvers)

    def test_a_roster_sent_back_and_submitted_again_before_the_push_left(self, mona, er, solver, dina, push_service):
        solved(mona, er, solver)
        ok(submit(mona, er))
        run_the_queue()
        manager = on("mona")

        ok(reject(dina, er, "Too many nights."))
        ok(submit(mona, er))

        run_the_queue()
        assert kinds_to(push_service, manager) == []


class TestStillHasAPage:
    def test_a_manager_taken_off_the_unit_before_the_push_left_is_sent_nothing(self, mona, er, solver, carla, push_service):
        approved(mona, carla, er, solver)
        swap = proposed(as_user("Badr"), *SOUND_SWAP)
        run_the_queue()
        manager = on("mona")

        acted(as_user("Emad"), swap["id"], "accept")
        Unit.objects.filter(pk=er.pk).update(manager=None)

        run_the_queue()
        assert kinds_to(push_service, manager) == []

    def test_a_reader_deactivated_before_the_push_left_is_sent_nothing(self, mona, er, push_service):
        amal = on("Amal")

        opened(mona, er)
        User.objects.filter(username="Amal").update(is_active=False)

        run_the_queue()
        assert kinds_to(push_service, amal) == []


class TestWhatThePushServiceAnswers:
    @pytest.mark.parametrize("status", [404, 410])
    def test_an_address_gone_ends_the_subscription_at_once(self, mona, er, push_service, status):
        amal = on("Amal")
        push_service.answer(amal.endpoint, status)

        opened(mona, er)
        run_the_queue()

        assert len(push_service.to(amal.endpoint)) == 1
        assert subscription(amal) is None
        assert ok(post(as_user("Amal"), f"{PUSH}/state", {"endpoint": amal.endpoint}))["this_device"] is False

    @pytest.mark.parametrize("status", [400, 401, 403, 413])
    def test_a_refusal_counts_one_failure_and_is_not_tried_again(self, mona, er, push_service, status):
        amal = on("Amal")
        push_service.answer(amal.endpoint, status)

        opened(mona, er)
        run_the_queue()

        assert len(push_service.to(amal.endpoint)) == 1
        assert subscription(amal).failures_in_a_row == 1

    def test_the_tenth_failure_in_a_row_ends_it(self, mona, er, push_service):
        amal, badr = on("Amal"), on("Badr")
        PushSubscription.objects.filter(endpoint__in=[amal.endpoint, badr.endpoint]).update(failures_in_a_row=8)
        push_service.answer(amal.endpoint, 403)
        push_service.answer(badr.endpoint, 403)
        PushSubscription.objects.filter(endpoint=badr.endpoint).update(failures_in_a_row=9)

        opened(mona, er)
        run_the_queue()

        assert subscription(amal).failures_in_a_row == 9
        assert subscription(badr) is None

    def test_a_success_resets_the_count_and_dates_it(self, mona, er, push_service):
        amal = on("Amal")
        PushSubscription.objects.filter(endpoint=amal.endpoint).update(failures_in_a_row=9)

        opened(mona, er)
        run_the_queue()

        row = subscription(amal)
        assert row.failures_in_a_row == 0 and row.last_success_at is not None

    def test_an_end_writes_no_audit_entry(self, mona, er, push_service):
        from audit.models import AuditEntry
        amal = on("Amal")
        push_service.answer(amal.endpoint, 410)

        opened(mona, er)
        run_the_queue()

        assert list(AuditEntry.objects.filter(action__startswith="push.").values_list("action", flat=True)) == ["push.turned_on"]


class TestTryingAgain:
    def test_only_the_subscription_that_could_not_be_reached_is_tried_again(self, mona, er, push_service):
        """The phone's push service is down for one try; the laptop's answered. A ten-minute outage costs the phone one failure
        a push, however many tries it took."""
        amal = on("Amal")
        laptop = turned_on(as_user("Amal"), Device("https://updates.push.services.mozilla.com/wpush/v2/amals-laptop"))
        push_service.answer(amal.endpoint, requests.ConnectionError("no route"), 201)

        opened(mona, er)
        run_the_queue()

        assert [amal.read(sent.body)["kind"] for sent in push_service.to(amal.endpoint)] == ["requests_open"] * 2
        assert len(push_service.to(laptop.endpoint)) == 1
        assert subscription(amal).failures_in_a_row == 0
        assert sorted(PushAttempt.objects.values_list("outcome", flat=True)) == ["sent", "sent", "unreachable"]

    @pytest.mark.parametrize("answer", [429, 500, 503, requests.Timeout("too slow")])
    def test_three_times_more_then_one_failure(self, mona, er, push_service, answer):
        amal = on("Amal")
        push_service.answer(amal.endpoint, answer)

        opened(mona, er)
        run_the_queue()

        assert len(push_service.to(amal.endpoint)) == 4
        assert subscription(amal).failures_in_a_row == 1
        assert list(PushAttempt.objects.values_list("outcome", flat=True)) == ["unreachable"] * 4

    def test_the_health_page_counts_every_try_not_the_one_job(self, mona, er, push_service, signed_in):
        """One window opened, one push job: Amal's phone is tried four times in vain and her laptop is reached once."""
        amal = on("Amal")
        turned_on(as_user("Amal"), Device("https://updates.push.services.mozilla.com/wpush/v2/amals-laptop"))
        push_service.answer(amal.endpoint, 503)

        opened(mona, er)
        run_the_queue()

        health = signed_in(username="sysadmin").get("/api/ops/health").json()
        assert health["push"] == {"sent": 1, "failed": 4, "last_failure": "unreachable"}

    def test_a_try_again_reaches_no_subscription_turned_on_since(self, mona, er, push_service):
        amal = on("Amal")
        laptop = Device("https://updates.push.services.mozilla.com/wpush/v2/amals-laptop")

        def down_while_the_laptop_turns_on():
            PushSubscription.objects.create(user=User.objects.get(username="Amal"), endpoint=laptop.endpoint, **laptop.keys)
            raise requests.ConnectionError("no route")

        push_service.answer(amal.endpoint, down_while_the_laptop_turns_on, 201)

        opened(mona, er)
        run_the_queue()

        assert len(push_service.to(amal.endpoint)) == 2
        assert push_service.to(laptop.endpoint) == []

    def test_a_try_again_is_not_sent_to_an_address_moved_to_another_user(self, mona, er, solver, carla, push_service):
        """The address belongs to whoever last subscribed it: Badr signed in on Emad's old phone between the tries, and the swap
        to answer is Emad's."""
        approved(mona, carla, er, solver)
        run_the_queue()
        emad = on("Emad")

        def down_while_badr_takes_the_phone():
            PushSubscription.objects.filter(endpoint=emad.endpoint).update(user=User.objects.get(username="Badr"))
            raise requests.ConnectionError("no route")

        push_service.answer(emad.endpoint, down_while_badr_takes_the_phone, 201)

        proposed(as_user("Badr"), *SOUND_SWAP)
        run_the_queue()

        assert len(push_service.to(emad.endpoint)) == 1
        assert PushSubscription.objects.get(endpoint=emad.endpoint).failures_in_a_row == 0

    def test_a_try_again_waiting_for_its_time_holds_back_no_new_push(self, mona, er, solver, push_service, monkeypatch):
        from notifications import pushes
        monkeypatch.setattr(pushes, "RETRY_IN", (3600, 3600, 3600))
        amal, manager = on("Amal"), on("mona")
        push_service.answer(amal.endpoint, requests.ConnectionError("no route"))
        opened(mona, er)
        run_the_queue(leave_waiting=True)  # Amal's try again waits an hour

        solved(mona, er, solver)
        ok(submit(mona, er))
        ok(reject(as_user("dina"), er, "Too many nights."))
        run_the_queue(leave_waiting=True)

        assert kinds_to(push_service, manager) == ["roster_sent_back"]
        assert len(push_service.to(amal.endpoint)) == 1


class Clock:
    """The push job's clock, moved by hand: a send takes the time a test gives it."""

    def __init__(self):
        self.seconds = 0.0

    def __call__(self) -> float:
        return self.seconds

    def taking(self, seconds: float, answer=201):
        """A scripted answer that takes `seconds` before it comes."""
        def slow():
            self.seconds += seconds
            if isinstance(answer, BaseException):
                raise answer
            return answer
        return slow


@pytest.fixture
def clock(monkeypatch) -> Clock:
    from notifications import pushes
    moved = Clock()
    monkeypatch.setattr(pushes, "clock", moved)
    return moved


NURSES = ("Amal", "Badr", "Dana", "Emad")


class TestTheBudget:
    """A push job starts no new send once it has run 30 seconds, and hands the sends it has not made to a new push job, behind
    any push already waiting: no job holds the others back by more than about 30 seconds and one send."""

    def test_a_job_past_its_budget_hands_on_and_the_sends_are_made_after_a_push_already_waiting(
            self, mona, er, solver, push_service, clock):
        solved(mona, er, solver)
        nurses = everyone_on(*NURSES)
        approvers = everyone_on("carla", "dina")
        for device in nurses.values():
            push_service.answer(device.endpoint, clock.taking(31))
        opened(mona, er)  # one job for the four nurses
        ok(submit(mona, er))  # then one for the approvers, waiting behind it

        run_the_queue()

        order = [sent.endpoint for sent in push_service.sent]
        first = order[0]
        assert first in {device.endpoint for device in nurses.values()}
        assert set(order[1:3]) == {device.endpoint for device in approvers.values()}
        assert sorted(order[3:]) == sorted(device.endpoint for device in nurses.values() if device.endpoint != first)
        assert pushed(push_service, nurses) == {username: ["requests_open"] for username in NURSES}
        assert list(PushAttempt.objects.values_list("outcome", flat=True)) == ["sent"] * 6

    def test_a_job_within_its_budget_makes_every_send(self, mona, er, push_service, clock):
        nurses = everyone_on(*NURSES)
        for device in nurses.values():
            push_service.answer(device.endpoint, clock.taking(9))

        opened(mona, er)
        run_the_queue()

        assert pushed(push_service, nurses) == {username: ["requests_open"] for username in NURSES}
        assert ProcrastinateJob.objects.filter(task_name="notifications.tasks.push").count() == 1

    def test_a_send_handed_on_keeps_its_try_number(self, mona, er, push_service, clock):
        """Both phones are down: the first try reaches neither, the second runs out of time after Amal's, and Badr's second try
        is handed on. Each is still sent four times and counts one failure, as a phone that is down always does."""
        amal, badr = on("Amal"), on("Badr")
        down = requests.ConnectionError("no route")
        for device in (amal, badr):
            push_service.answer(device.endpoint, down, clock.taking(31, down))

        opened(mona, er)
        run_the_queue()

        assert len(push_service.to(amal.endpoint)) == 4 and len(push_service.to(badr.endpoint)) == 4
        assert subscription(amal).failures_in_a_row == 1 and subscription(badr).failures_in_a_row == 1
        assert list(PushAttempt.objects.values_list("outcome", flat=True)) == ["unreachable"] * 8

    def test_a_handed_on_send_to_a_thing_that_no_longer_waits_is_not_made(self, mona, er, push_service, clock):
        """The next job reads again that the thing still waits, as a try again does."""
        amal, badr = on("Amal"), on("Badr")
        window = {}

        def slow_while_the_window_is_removed():
            clock.seconds += 31
            from requesting.models import RequestWindow
            RequestWindow.objects.filter(pk=window["id"]).delete()
            return 201

        for device in (amal, badr):
            push_service.answer(device.endpoint, slow_while_the_window_is_removed)

        window.update(opened(mona, er))
        run_the_queue()

        assert len(push_service.sent) == 1


class TestAnUnforeseenError:
    def test_a_library_exception_of_an_unforeseen_kind_is_a_refusal_and_the_job_goes_on(self, mona, er, push_service):
        class Unforeseen(Exception):
            pass

        amal = on("Amal")
        laptop = turned_on(as_user("Amal"), Device("https://updates.push.services.mozilla.com/wpush/v2/amals-laptop"))
        push_service.answer(amal.endpoint, Unforeseen("the library did not see this coming"))

        opened(mona, er)
        run_the_queue()

        assert len(push_service.to(amal.endpoint)) == 1 and len(push_service.to(laptop.endpoint)) == 1
        assert subscription(amal).failures_in_a_row == 1
        assert sorted(PushAttempt.objects.values_list("outcome", flat=True)) == ["refused", "sent"]


class TestOnlyThePushServicesHosts:
    """The worker posts only to a browser push service: an address elsewhere is refused
    when it is turned on, and one held from before is ended at the next send, unsent."""

    @pytest.mark.parametrize("endpoint", [
        "https://fcm.googleapis.com/fcm/send/abc",
        "https://FCM.googleapis.com/fcm/send/abc",
        "https://updates.push.services.mozilla.com/wpush/v2/abc",
        "https://push.services.mozilla.com/wpush/v1/abc",
        "https://web.push.apple.com/QKC1Muic0H7",
        "https://wns2-par02p.notify.windows.com/w/?token=abc",
        "https://fcm.googleapis.com:443/fcm/send/abc",
    ])
    def test_a_push_services_address_is_turned_on(self, signed_in, endpoint):
        client = signed_in(Role.STAFF_NURSE, username="zaid")

        response = post(client, f"{PUSH}/on", {"endpoint": endpoint, "keys": Device(endpoint).keys})

        assert response.status_code == 200, response.content
        assert response.json()["devices"] == 1

    @pytest.mark.parametrize("endpoint", [
        "https://evil.example/fcm/send/abc",
        "https://fcm.googleapis.com.evil.example/fcm/send/abc",
        "https://evilfcm.googleapis.com/fcm/send/abc",
        "https://push.apple.com/abc",
        "https://evil.example/https://web.push.apple.com/abc",
        "https://user@fcm.googleapis.com/fcm/send/abc",
        "https://fcm.googleapis.com:8443/fcm/send/abc",
        "https://fcm.googleapis.com./fcm/send/abc",
        "https://169.254.169.254/latest/meta-data",
        "https://db:5432/",
        "https://solver:8080/solve",
        "https://localhost/api",
    ])
    def test_any_other_address_is_refused_as_a_field_error_and_nothing_is_kept(self, signed_in, endpoint):
        client = signed_in(Role.STAFF_NURSE, username="zaid")

        response = post(client, f"{PUSH}/on", {"endpoint": endpoint, "keys": Device(endpoint).keys})

        assert response.status_code == 422, response.content
        [error] = response.json()["detail"]
        assert error["loc"][-1] == "endpoint"
        assert "not a browser push service" in error["msg"]
        assert not PushSubscription.objects.exists()

    def test_turning_off_or_reading_such_an_address_still_answers(self, signed_in):
        client = signed_in(Role.STAFF_NURSE, username="zaid")

        assert ok(post(client, f"{PUSH}/state", {"endpoint": "https://evil.example/x"}))["this_device"] is False
        assert ok(post(client, f"{PUSH}/off", {"endpoint": "https://evil.example/x"}))["devices"] == 0

    def test_one_held_from_before_is_ended_at_the_next_send_unsent(self, mona, er, push_service):
        amal = on("Amal")
        elsewhere = Device("https://evil.example/collect")
        PushSubscription.objects.create(user=User.objects.get(username="Amal"), endpoint=elsewhere.endpoint, **elsewhere.keys)

        opened(mona, er)
        run_the_queue()

        assert push_service.to(elsewhere.endpoint) == []
        assert subscription(elsewhere) is None
        assert len(push_service.to(amal.endpoint)) == 1
        assert PushAttempt.objects.count() == 1
