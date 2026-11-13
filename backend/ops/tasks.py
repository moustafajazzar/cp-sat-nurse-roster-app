"""The installation's own jobs. Each periodic job takes a lock under its own name."""
from ops import backups, queue
from ops.queue import daily, every_five_minutes


@every_five_minutes
def backup(timestamp: int) -> None:
    """Takes the nightly backup once the System Admin's backup time has passed on the hospital's clock, then removes those past
    the days kept. The schedule is fixed at import and in UTC, so this asks every five minutes and decides itself: a backup
    comes at most five minutes after its time, and a worker that was down catches up once. It can run for minutes, so its
    lock (`backup`) is what keeps it to one slot of the worker's four and never beside itself."""
    backups.back_up_if_due()


@daily(at_hour_utc=22, at_minute=10)
def remove_ended_jobs(timestamp: int) -> None:
    """The queue keeps a row for every run of every job. A day missed is caught up by the next."""
    queue.remove_ended_jobs()
