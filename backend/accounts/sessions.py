"""What counts as being there. `SESSION_SAVE_EVERY_REQUEST` pushes the twelve-hour idle limit forward on every signed-in
request, which would let an open tab polling a solve keep a session alive for good."""
from __future__ import annotations

from django.contrib.sessions.middleware import SessionMiddleware
from django.http import HttpRequest, HttpResponse


def not_activity(request: HttpRequest) -> None:
    """Call it from a view a timer calls, once it has passed authentication."""
    request.session_is_not_activity = True


class IdleAwareSessionMiddleware(SessionMiddleware):
    def process_response(self, request: HttpRequest, response: HttpResponse) -> HttpResponse:
        if getattr(request, "session_is_not_activity", False):
            return response
        return super().process_response(request, response)
