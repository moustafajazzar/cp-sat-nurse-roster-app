from django.contrib.sessions.backends.db import SessionStore

from accounts.login import remove_old_failures
from ops.queue import daily


@daily(at_hour_utc=22, at_minute=40)
def clear_expired_sessions(timestamp: int) -> None:
    """What Django's `clearsessions` does: a session past its twelve idle hours is no use to anyone."""
    SessionStore.clear_expired()


@daily(at_hour_utc=22, at_minute=50)
def remove_old_login_failures(timestamp: int) -> None:
    """A name's count of failed logins a day after its last failure, unless locked now, and an address's ended throttle
    window: no one's record. Last of the retention jobs, ahead of a nightly backup at 02:00 in Riyadh. A day
    missed is caught up by the next."""
    remove_old_failures()
