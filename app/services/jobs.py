"""Scheduled jobs. Each returns a small summary dict. They run inside the app on the
intervals in config.JOB_SCHEDULE_MINUTES (app/core/scheduler.py). Every job commits its own work."""
from datetime import timedelta

from flask import current_app

from app.core.utils import ensure_aware, today_ist, utcnow
from app.enums import (InterviewStatus, LetterType, MigrationStatus as S, MigrationType, NotificationEvent,
                       Permission)
from app.extensions import db
from app.models import Document, Interview, MigrationRequest, MigrationWindow, Notification, UserActivity
from app.services import seats
from app.services.directory import platform_admins, responsible_users
from app.services.notifications import flush_dispatch_queue, notify
from app.core.records import active, deactivate


def _finish(result):
    db.session.commit()
    flush_dispatch_queue()
    return result


def expire_offers():
    from app.services.letters import cancel_letters
    from app.services.migration import transition
    expired = 0
    for req in active(MigrationRequest).filter(MigrationRequest.request_status == S.PROVISIONAL_OFFER,
                                                MigrationRequest.offer_expires_at < utcnow()).all():
        transition(req, S.OFFER_EXPIRED, "Offer not accepted in time")
        seats.release_seat(req, transition)
        cancel_letters(req, [LetterType.PROVISIONAL_OFFER])
        expired += 1
    return _finish({"expired": expired})


def offer_reminders():
    hours = current_app.config["OFFER_EXPIRY_REMINDER_HOURS"]
    sent = 0
    for req in active(MigrationRequest).filter(MigrationRequest.request_status == S.PROVISIONAL_OFFER,
                                                MigrationRequest.offer_reminder_sent_at.is_(None),
                                                MigrationRequest.offer_expires_at < utcnow() + timedelta(hours=hours)).all():
        notify(req.student.user_id, NotificationEvent.OFFER_EXPIRING,
               {"reference": req.reference_no, "expires_on": req.offer_expires_at.date().isoformat()},
               idempotency_key=f"offer-expiring-{req.id}")
        req.offer_reminder_sent_at = utcnow()
        sent += 1
    return _finish({"reminded": sent})


def noc_escalation_check():
    """No NOC decision in time -> the source university decides (9.1)."""
    escalated = 0
    for req in active(MigrationRequest).filter(MigrationRequest.request_status == S.SUBMITTED,
                                                MigrationRequest.noc_escalated_at.is_(None),
                                                MigrationRequest.noc_due_at < utcnow()).all():
        req.noc_escalated_at = utcnow()
        target = req.source_university
        users = responsible_users(target, Permission.DECIDE_NOC) if target is not None else platform_admins()
        for u in users:
            notify(u, NotificationEvent.NOC_ESCALATED, {"reference": req.reference_no,
                                                        "source": req.source_college.name},
                   idempotency_key=f"noc-escalated-{req.id}")
        escalated += 1
    return _finish({"escalated": escalated})


def release_overdue_check():
    """Reminders at day 7 and 12 after acceptance, escalation after the deadline (9.1)."""
    reminders = current_app.config["RELEASE_REMINDER_DAYS"]
    reminded = escalated = 0
    for req in active(MigrationRequest).filter(MigrationRequest.request_status == S.OFFER_ACCEPTED).all():
        days = (utcnow() - ensure_aware(req.accepted_at)).days
        sent = list(req.release_reminders_sent or [])
        for d in reminders:
            if days >= d and d not in sent:
                ctx = {"reference": req.reference_no, "due": req.release_due_at.date().isoformat()}
                for u in responsible_users(req.source_college, Permission.ISSUE_RELEASE):
                    notify(u, NotificationEvent.RELEASE_REQUESTED, ctx, idempotency_key=f"release-{req.id}-{d}")
                if req.migration_type == MigrationType.CROSS_UNIVERSITY and req.source_university_id:
                    for u in responsible_users(req.source_university, Permission.ISSUE_RELEASE):
                        notify(u, NotificationEvent.RELEASE_REQUESTED, ctx, idempotency_key=f"release-u-{req.id}-{d}")
                sent.append(d)
                reminded += 1
        req.release_reminders_sent = sent
        if ensure_aware(req.release_due_at) < utcnow() and req.release_escalated_at is None:
            req.release_escalated_at = utcnow()
            for pa in platform_admins():
                notify(pa, NotificationEvent.RELEASE_OVERDUE, {"reference": req.reference_no,
                                                               "due": req.release_due_at.date().isoformat()},
                       idempotency_key=f"release-overdue-{req.id}")
            escalated += 1
    return _finish({"reminded": reminded, "escalated": escalated})


def originals_deadline_check():
    reminded = 0
    for req in active(MigrationRequest).filter(MigrationRequest.request_status == S.ADMITTED_ORIGINALS_PENDING,
                                                MigrationRequest.originals_due_at < utcnow() + timedelta(days=3)).all():
        notify(req.student.user_id, NotificationEvent.ORIGINALS_DEADLINE,
               {"destination": req.destination_college.name, "due": req.originals_due_at.date().isoformat()},
               idempotency_key=f"originals-{req.id}")
        reminded += 1
    return _finish({"reminded": reminded})


def merit_rounds():
    from app.services.migration import transition
    results = {}
    for window in active(MigrationWindow).filter(MigrationWindow.closes_on < today_ist(),
                                                  MigrationWindow.merit_round_completed_at.is_(None)).all():
        results[window.id] = seats.run_merit_round(window, transition)
    return _finish({"windows": results})


def interview_reminders():
    sent = 0
    for hours in current_app.config["INTERVIEW_REMINDER_HOURS"]:
        horizon = utcnow() + timedelta(hours=hours)
        for iv in active(Interview).filter(Interview.interview_status == InterviewStatus.SCHEDULED,
                                            Interview.scheduled_start <= horizon,
                                            Interview.scheduled_start > utcnow()).all():
            if hours in (iv.reminders_sent or []):
                continue
            req = db.session.get(MigrationRequest, iv.request_id)
            from app.services.interviews import _ctx
            ctx = _ctx(req, iv)
            notify(req.student.user_id, NotificationEvent.INTERVIEW_REMINDER, ctx,
                   idempotency_key=f"iv-{iv.id}-{hours}")
            for p in iv.panel:
                notify(p.user_id, NotificationEvent.INTERVIEW_REMINDER, ctx, idempotency_key=f"iv-{iv.id}-{hours}-p")
            iv.reminders_sent = list(iv.reminders_sent or []) + [hours]
            sent += 1
    return _finish({"reminded": sent})


def retry_notifications():
    """Retry sends that failed (RETRYING and due), and pick up rows left QUEUED by a restart."""
    from app.services.notifications import deliver
    from app.enums import NotificationState
    now = utcnow()
    due = Notification.query.filter(db.or_(
        db.and_(Notification.delivery_state == NotificationState.RETRYING, Notification.next_attempt_at <= now),
        db.and_(Notification.delivery_state == NotificationState.QUEUED,
                Notification.created_at <= now - timedelta(minutes=5)))).order_by(Notification.id).limit(500).all()
    sent = sum(1 for n in due if deliver(n.id))
    return _finish({"tried": len(due), "sent": sent})


def retention_purge():
    """Delete files past retention (9.2 M14-F14); activity logs kept at least a year."""
    from app.core.storage import get_storage
    cfg = current_app.config
    storage = get_storage()
    cutoff_not_admitted = utcnow() - timedelta(days=cfg["RETENTION_NOT_ADMITTED_DAYS"])
    cutoff_admitted = utcnow() - timedelta(days=cfg["RETENTION_ADMITTED_DAYS"])
    purged = 0
    closed = MigrationRequest.query.filter(MigrationRequest.closed_at.isnot(None)).all()
    for req in closed:
        cutoff = cutoff_admitted if req.admitted_at else cutoff_not_admitted
        if ensure_aware(req.closed_at) > cutoff:
            continue
        for doc in active(Document).filter_by(owner_id=req.id).filter(Document.owner_type == "MIGRATION_REQUEST"):
            storage.delete(doc.storage_key)
            deactivate(doc)
            purged += 1
    log_cutoff = utcnow() - timedelta(days=max(cfg["ACTIVITY_LOG_RETENTION_DAYS"], 365))
    old_logs = UserActivity.query.filter(UserActivity.created_at < log_cutoff).delete(synchronize_session=False)
    stale = Notification.query.filter(Notification.created_at < log_cutoff).delete(synchronize_session=False)
    return _finish({"files_purged": purged, "activity_rows_deleted": old_logs, "notifications_deleted": stale})


JOBS = {
    "expire_offers": expire_offers,
    "offer_reminders": offer_reminders,
    "noc_escalation_check": noc_escalation_check,
    "release_overdue_check": release_overdue_check,
    "originals_deadline_check": originals_deadline_check,
    "merit_rounds": merit_rounds,
    "interview_reminders": interview_reminders,
    "retry_notifications": retry_notifications,
    "retention_purge": retention_purge,
}
