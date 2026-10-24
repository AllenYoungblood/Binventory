"""Scheduled overnight tagging."""
from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger

from . import ml

scheduler = BackgroundScheduler(daemon=True)
_schedule_hour = None      # None = disabled
_schedule_min = 0

def schedule_hour():
    return _schedule_hour

def schedule_min():
    return _schedule_min

def apply_schedule(hour, minute):
    """Set or clear the nightly tagging schedule."""
    global _schedule_hour, _schedule_min
    scheduler.remove_all_jobs()
    if hour is None:
        _schedule_hour = None
        print("[Scheduler] Schedule cleared")
    else:
        _schedule_hour = hour
        _schedule_min  = minute
        scheduler.add_job(
            ml.enqueue_all_untagged,
            CronTrigger(hour=hour, minute=minute),
            id="nightly_tag",
            replace_existing=True,
        )
        print(f"[Scheduler] Set to run daily at {hour:02d}:{minute:02d}")
