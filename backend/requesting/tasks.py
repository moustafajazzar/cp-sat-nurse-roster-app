from django.db import transaction

from ops.queue import hourly
from requesting.windows import catch_up, not_closed


@hourly(at_minute=10)
def open_and_close_request_windows(timestamp: int) -> None:
    """Opens each window whose open date has come, telling its nurses, and closes each whose close date has passed, telling
    its manager of requests waiting. Every write about a unit's windows does the same for that unit first, so a worker that
    is down delays only who is told, never what the pages say or what a nurse may do."""
    with transaction.atomic():
        catch_up(not_closed())
