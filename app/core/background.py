"""Send notifications in background threads inside the app.

After a request commits, new notification rows are handed to a small thread pool,
so emails, SMS and WhatsApp messages never slow the response. Failed sends are
retried by the ``retry_notifications`` scheduled job on NOTIFICATION_RETRY_DELAYS_SECONDS.
"""
import logging
from concurrent.futures import ThreadPoolExecutor

log = logging.getLogger("app.background")
_executor = None


def _pool(app):
    global _executor
    if _executor is None:
        _executor = ThreadPoolExecutor(max_workers=app.config["NOTIFICATION_WORKER_THREADS"],
                                       thread_name_prefix="notify")
    return _executor


def _deliver_in_context(app, ids):
    from app.extensions import db
    from app.services.notifications import deliver, flush_dispatch_queue
    with app.app_context():
        try:
            for nid in ids:
                try:
                    deliver(nid)
                except Exception:  # noqa: BLE001 - keep going; the retry job picks it up
                    log.exception("notification %s failed", nid)
                    db.session.rollback()
            flush_dispatch_queue()  # fallbacks created while delivering
        finally:
            db.session.remove()


def submit_notifications(app, ids):
    if ids:
        _pool(app).submit(_deliver_in_context, app, list(ids))
