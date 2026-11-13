from django.db import transaction

from ops.queue import hourly
from covers.expiring import expire_passed


@hourly(at_minute=20)
def expire_covers(timestamp: int) -> None:
    """Stores each waiting cover whose date has passed, on the hospital's clock, as expired. Every page reads the date itself and
    every write about a cover does the same for it first, so a worker that is down delays nothing anyone reads."""
    with transaction.atomic():
        expire_passed()
