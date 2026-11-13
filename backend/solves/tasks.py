from ops.queue import every_five_minutes, one_at_a_time_across_the_hospital, requeue_stalled
from solves import running


@one_at_a_time_across_the_hospital
def run_solve(solve_id: str) -> None:
    """Never fails on the queue: a failed solve is the row's to say, and it retries there, keeping its place in line."""
    running.run(solve_id)


@every_five_minutes
def recover_lost_jobs(timestamp: int) -> None:
    """A worker that dies mid-solve leaves its job holding the hospital-wide lock, so no solve would run again. Five minutes
    because a solve may wait that long after a crash, and a job row is kept for each run."""
    requeue_stalled()
    running.end_what_the_queue_lost()
