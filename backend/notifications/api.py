"""Every user's own notifications: the bell asks how many are unread, and the Notifications page lists them and marks them read.
Nothing here takes a user, so nobody reads another's, the System Admin included."""
from __future__ import annotations

import datetime as dt
from dataclasses import asdict
from typing import Literal
from uuid import UUID

from django.http import HttpRequest
from django.shortcuts import get_object_or_404
from django.utils import timezone
from ninja import Field, Router, Schema

from accounts.authz import SignedIn
from accounts.sessions import not_activity
from notifications.models import Notification
from notifications.reading import Names, link_of, text_of

router = Router(tags=["notifications"])
SHOWN = 50


class Polled(SignedIn):
    """For what a timer asks: it is not the user being there, whatever the answer, a refusal included, so it never moves the
    twelve-hour idle limit."""

    def authenticate(self, request: HttpRequest, key: str | None):
        not_activity(request)
        return super().authenticate(request, key)


class Unread(Schema):
    unread: int


class NotificationLink(Schema):
    page: Literal["roster", "my_roster", "unit_roster", "my_month", "requests", "swaps", "unit_swaps", "covers", "unit_covers",
                  "rostering_attributes"]
    month: str
    unit_id: UUID | None = None
    cover_id: UUID | None = Field(None, description="The cover a manager's `unit_covers` link shows.")


class NotificationRow(Schema):
    id: UUID
    event: str
    text: str
    link: NotificationLink | None = Field(description="The reader's own page for it. Null when they no longer have one.")
    created_at: dt.datetime
    read: bool


class NotificationList(Schema):
    unread: int
    items: list[NotificationRow] = Field(description=f"The latest {SHOWN}, newest first.")


def _unread(request: HttpRequest) -> int:
    return Notification.objects.filter(user=request.user, read_at__isnull=True).count()


@router.get("/unread", response=Unread, auth=Polled(), operation_id="countUnreadNotifications")
def unread(request: HttpRequest) -> Unread:
    """What the bell polls. A test holds that it is not activity."""
    return Unread(unread=_unread(request))


@router.get("", response=NotificationList, operation_id="listNotifications")
def list_notifications(request: HttpRequest) -> NotificationList:
    notes = list(Notification.objects.filter(user=request.user)[:SHOWN])
    names = Names(notes)
    rows = []
    for note in notes:
        link = link_of(note, request.user, names)
        rows.append(NotificationRow(id=note.pk, event=note.event, text=text_of(note, names),
                                    link=NotificationLink(**asdict(link)) if link else None,
                                    created_at=timezone.localtime(note.created_at), read=note.read_at is not None))
    return NotificationList(unread=_unread(request), items=rows)


@router.post("/{notification_id}/read", response={204: None}, operation_id="markNotificationRead")
def mark_read(request: HttpRequest, notification_id: UUID):
    """A 404 for another user's, the same as for none."""
    note = get_object_or_404(Notification, pk=notification_id, user=request.user)
    if note.read_at is None:
        note.read_at = timezone.now()
        note.save(update_fields=["read_at"])
    return 204, None


@router.post("/read-all", response={204: None}, operation_id="markAllNotificationsRead")
def mark_all_read(request: HttpRequest):
    Notification.objects.filter(user=request.user, read_at__isnull=True).update(read_at=timezone.now())
    return 204, None
