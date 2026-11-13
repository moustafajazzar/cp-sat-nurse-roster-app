"""Which notifications are pushed, and as what: the six that have a deadline, each as a kind code and nothing else.
The service worker (`frontend/public/sw.js`) holds each kind's words and the page a tap opens. A feature with a new deadline adds
its kind here and its words there, and nothing else."""
from __future__ import annotations

from django.db.models import TextChoices

from notifications.models import Event


class PushKind(TextChoices):
    SWAP_TO_ANSWER = "swap_to_answer"
    SWAP_TO_DECIDE = "swap_to_decide"
    REQUESTS_OPEN = "requests_open"
    REQUESTS_TO_DECIDE = "requests_to_decide"
    ROSTER_TO_APPROVE = "roster_to_approve"
    ROSTER_SENT_BACK = "roster_sent_back"


# A swap's other steps, a roster approved or changed, a solve's end, a window closed early, a decision, a month closed and a
# cover's steps stay on the bell alone.
_BY_EVENT = {
    Event.WINDOW_OPENED: PushKind.REQUESTS_OPEN,
    Event.WINDOW_CLOSED: PushKind.REQUESTS_TO_DECIDE,  # the manager's, sent only when requests wait
    Event.ROSTER_SUBMITTED: PushKind.ROSTER_TO_APPROVE,
    Event.ROSTER_REJECTED: PushKind.ROSTER_SENT_BACK,
}
_BY_SWAP_STEP = {"proposed": PushKind.SWAP_TO_ANSWER, "awaiting": PushKind.SWAP_TO_DECIDE}


def kind_of(event: str, payload: dict) -> PushKind | None:
    if event == Event.SWAP_STEP:
        return _BY_SWAP_STEP.get(payload.get("step"))
    return _BY_EVENT.get(event)
