from django.db import transaction

from ops.queue import hourly
from swaps.expiring import expire_passed


@hourly(at_minute=15)
def expire_swaps(timestamp: int) -> None:
    """Expires each waiting swap whose earliest date has passed, on the hospital's clock, telling everyone in it. Every page reads
    the dates itself and every write about a swap does the same for it first, so a worker that is down delays only who is told."""
    with transaction.atomic():
        expire_passed()
