"""Who may call what. An endpoint or router names its roles here:

    Router(auth=SignedIn(Role.SYSTEM_ADMIN))

A signed-in user who has not yet changed a forced password may call nothing else until they do.
"""
from __future__ import annotations

from django.http import HttpRequest
from ninja.errors import HttpError
from ninja.security import SessionAuth

from accounts.sessions import not_activity


class SignedIn(SessionAuth):
    """401 when not signed in, 403 for the wrong role, and CSRF on every unsafe method."""

    def __init__(self, *roles: str, during_forced_change: bool = False):
        super().__init__()
        self.roles = roles
        self.during_forced_change = during_forced_change

    def authenticate(self, request: HttpRequest, key: str | None):
        user = request.user
        if not user.is_authenticated:
            return None
        if user.must_change_password and not self.during_forced_change:
            raise HttpError(403, "Change your password first.")
        if self.roles and user.role not in self.roles:
            raise HttpError(403, "Your role may not do this.")
        return user


CROSS_SITE = "This file opens only from the app's own pages, not from a link on another site."


class SignedInForAFile(SignedIn):
    """A route that answers with a file: a plain GET, which a page on another site can make a signed-in user's browser take,
    since the session cookie is `SameSite=Lax`. Refused with 403 when the browser says the request was
    started by another site (`Sec-Fetch-Site: cross-site`), before the session's user and role are read, so that the answer is the
    same whoever is signed in: it says nothing of the visitor's role, nothing is logged, and the idle limit does not move. The app's own links are `same-origin`; a request
    with no such header, from an older browser or a script, is served as before."""

    def authenticate(self, request: HttpRequest, key: str | None):
        if request.headers.get("Sec-Fetch-Site") == "cross-site":
            not_activity(request)  # Nor does it keep the session alive: the user did not ask for it.
            raise HttpError(403, CROSS_SITE)
        return super().authenticate(request, key)


def for_a_file(auth: SignedIn) -> SignedInForAFile:
    """The same roles, for a route that answers with a file."""
    return SignedInForAFile(*auth.roles, during_forced_change=auth.during_forced_change)
