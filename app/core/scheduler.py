"""Scheduled jobs run inside the app (no separate worker process).

APScheduler starts with the app when SCHEDULER_ENABLED is true. Intervals are in
config.JOB_SCHEDULE_MINUTES. When the app runs as several processes (gunicorn -w 4),
every process has a scheduler, but each job run first takes a PostgreSQL advisory lock,
so a job only ever runs in one process at a time; the others skip that tick.
"""
import logging
import os
import zlib

from sqlalchemy import text

log = logging.getLogger("app.scheduler")
_scheduler = None


def _lock_key(name):
    return zlib.crc32(f"sms-job:{name}".encode())


def run_job_once(app, name):
    """Run one job if no other process is running it. Returns the job's summary or None."""
    from app.extensions import db
    from app.services.jobs import JOBS
    with app.app_context():
        conn = db.engine.connect()
        try:
            got = conn.execute(text("SELECT pg_try_advisory_lock(:k)"), {"k": _lock_key(name)}).scalar()
            if not got:
                return None
            try:
                result = JOBS[name]()
                log.info("job %s: %s", name, result)
                return result
            except Exception:  # noqa: BLE001 - a failing job must not stop the scheduler
                log.exception("job %s failed", name)
                db.session.rollback()
                return None
            finally:
                db.session.remove()
                conn.execute(text("SELECT pg_advisory_unlock(:k)"), {"k": _lock_key(name)})
        finally:
            conn.close()


def start_scheduler(app):
    global _scheduler
    if _scheduler is not None or not app.config.get("SCHEDULER_ENABLED"):
        return None
    # The Flask debug reloader starts the app twice; only the child process serves requests.
    if app.debug and os.environ.get("WERKZEUG_RUN_MAIN") != "true" and os.environ.get("FLASK_RUN_FROM_CLI"):
        return None
    from apscheduler.schedulers.background import BackgroundScheduler
    from app.services.jobs import JOBS
    scheduler = BackgroundScheduler(timezone="UTC", job_defaults={"coalesce": True, "max_instances": 1})
    for name, minutes in app.config["JOB_SCHEDULE_MINUTES"].items():
        if name not in JOBS:
            raise RuntimeError(f"JOB_SCHEDULE_MINUTES names an unknown job: {name}")
        scheduler.add_job(run_job_once, "interval", minutes=minutes, args=[app, name], id=name,
                          misfire_grace_time=max(60, minutes * 30))
    scheduler.start()
    _scheduler = scheduler
    log.info("scheduler started with %d jobs", len(app.config["JOB_SCHEDULE_MINUTES"]))
    return scheduler
