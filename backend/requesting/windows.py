"""A request window's life on the hospital's clock. Its dates decide whether it is open: `state_today` reads them for every page,
so nothing waits on the worker. What the dates make happen, telling the nurses it opened and the manager it closed, happens once,
in `catch_up`, which the hourly job runs for every unit and each write runs first for the unit it touches: whichever comes first
does it, under the window's row lock, and the other finds it done."""
from __future__ import annotations

from uuid import UUID

from django.db.models import QuerySet
from django.utils import timezone

from notifications import sending
from nurses import membership
from requesting.models import RequestState, RequestWindow, WindowState


def state_today(window: RequestWindow) -> WindowState:
    """Open from the first moment of its open date to the last of its close date, hospital time, unless closed early."""
    today = membership.today()
    if window.closed_at is not None or today > window.closes_on:
        return WindowState.CLOSED
    return WindowState.OPEN if today >= window.opens_on else WindowState.SCHEDULED


def catch_up(windows: QuerySet[RequestWindow]) -> list[RequestWindow]:
    """Brings the stored state of these windows to what their dates say, telling whoever that tells. A window whose whole span
    passed unseen, with the worker down, closes without telling the nurses it opened. Returns them as they now stand, locked
    until the caller's transaction ends."""
    rows = list(windows.select_for_update(of=("self",)).select_related("unit__manager").order_by("pk"))
    for window in rows:
        if window.state == WindowState.CLOSED:
            continue
        now = state_today(window)
        if now == WindowState.OPEN and window.state == WindowState.SCHEDULED:
            window.state, window.opened_at = WindowState.OPEN, timezone.now()
            window.save(update_fields=["state", "opened_at"])
            sending.window_opened(window)
        elif now == WindowState.CLOSED:
            # A window that never opened holds no request, so its manager is told nothing either.
            window.state, window.closed_at = WindowState.CLOSED, window.closed_at or timezone.now()
            window.save(update_fields=["state", "closed_at"])
            if window.requests.filter(state=RequestState.PENDING).exists():
                sending.window_closed(window)
    return rows


def not_closed(unit_id: UUID | None = None) -> QuerySet[RequestWindow]:
    windows = RequestWindow.objects.exclude(state=WindowState.CLOSED)
    return windows.filter(unit_id=unit_id) if unit_id is not None else windows
