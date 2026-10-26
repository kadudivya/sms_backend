"""Create and deliver notifications (7.4, 9.2 M17-F09/F11, INT-10).

* Every event creates an IN_APP row and an EMAIL row; SMS or WhatsApp is added
  when it is the user's preferred channel and enabled.
* Rows are sent after the database commit, in background threads (app/core/background.py).
* Failures retry on the configured delays; a failed SMS/WhatsApp falls back to
  email. Workflow status never depends on delivery.
"""
from datetime import timedelta

from flask import current_app
from sqlalchemy import event

from app.core.utils import utcnow
from app.enums import NotificationChannel, NotificationEvent, NotificationState
from app.extensions import db
from app.models import Notification, User
from app.services.channels import ChannelError, channel_for
from app.services.notification_templates import render

_PENDING_KEY = "pending_notification_ids"


def _channels_for(user):
    enabled = current_app.config["NOTIFICATION_CHANNELS_ENABLED"]
    chans = []
    if enabled.get("IN_APP"):
        chans.append(NotificationChannel.IN_APP)
    if enabled.get("EMAIL"):
        chans.append(NotificationChannel.EMAIL)
    pref = user.preferred_channel
    if pref == NotificationChannel.SMS and enabled.get("SMS") and user.mobile:
        chans.append(NotificationChannel.SMS)
    if (pref == NotificationChannel.WHATSAPP and enabled.get("WHATSAPP") and user.mobile
            and user.whatsapp_opt_in):
        chans.append(NotificationChannel.WHATSAPP)
    return chans


def _recipient(user, channel):
    if channel in (NotificationChannel.SMS, NotificationChannel.WHATSAPP):
        return user.mobile
    if channel == NotificationChannel.IN_APP:
        return str(user.id)
    return user.email


def notify(user, event_type, context=None, link=None, idempotency_key=None, channels=None):
    """Queue a notification for ``user``. Call inside the business transaction."""
    if user is None:
        return []
    if isinstance(user, int):
        user = db.session.get(User, user)
    event_type = NotificationEvent(event_type)
    subject, body = render(event_type, context, user.language)
    rows = []
    for channel in channels or _channels_for(user):
        key = f"{idempotency_key}:{channel.value}:{user.id}" if idempotency_key else None
        if key and Notification.query.filter_by(idempotency_key=key).first():
            continue  # M17-F10 duplicate event
        row = Notification(user_id=user.id, event=event_type, channel=channel,
                           recipient=_recipient(user, channel), subject=subject, body=body,
                           link=link, payload={k: str(v) for k, v in (context or {}).items()
                                               if k not in ("otp",)},
                           idempotency_key=key)
        db.session.add(row)
        rows.append(row)
    db.session.flush()
    db.session.info.setdefault(_PENDING_KEY, []).extend(r.id for r in rows)
    return rows


def notify_email_address(email, event_type, context=None, link=None):
    """For recipients without an account yet (invitations)."""
    subject, body = render(NotificationEvent(event_type), context)
    row = Notification(user_id=None, event=NotificationEvent(event_type),
                       channel=NotificationChannel.EMAIL, recipient=email, subject=subject,
                       body=body, link=link, payload={})
    db.session.add(row)
    db.session.flush()
    db.session.info.setdefault(_PENDING_KEY, []).append(row.id)
    return row


def deliver(notification_id):
    """Attempt delivery once. Returns True on success. Used by the background threads,
    the retry job and synchronous mode (tests)."""
    row = db.session.get(Notification, notification_id)
    if row is None or row.delivery_state == NotificationState.SENT:
        return True
    row.attempts += 1
    try:
        channel_for(row.channel.value).send(row)
    except ChannelError as exc:
        row.last_error = str(exc)[:1000]
        retries = current_app.config["NOTIFICATION_RETRY_DELAYS_SECONDS"]
        if row.attempts > len(retries):
            row.delivery_state = NotificationState.FAILED
            row.next_attempt_at = None
            _fallback(row)
        else:
            row.delivery_state = NotificationState.RETRYING
            row.next_attempt_at = utcnow() + timedelta(seconds=retries[row.attempts - 1])
        db.session.commit()
        return False
    row.delivery_state = NotificationState.SENT
    row.sent_at = utcnow()
    row.next_attempt_at = None
    db.session.commit()
    return True


def _fallback(row):
    fallback = current_app.config["NOTIFICATION_FALLBACK_CHANNEL"]
    if row.channel.value == fallback or row.user_id is None:
        return
    user = db.session.get(User, row.user_id)
    fb = Notification(user_id=row.user_id, event=row.event, channel=NotificationChannel(fallback),
                      recipient=user.email, subject=row.subject, body=row.body, link=row.link,
                      payload=row.payload, fallback_of_id=row.id)
    db.session.add(fb)
    db.session.flush()
    db.session.info.setdefault(_PENDING_KEY, []).append(fb.id)


def dispatch_pending(ids):
    if not ids:
        return
    if current_app.config["NOTIFICATIONS_SEND_ASYNC"]:
        from app.core.background import submit_notifications
        submit_notifications(current_app._get_current_object(), ids)
    else:
        for nid in ids:
            deliver(nid)


def register_dispatch_hook():
    @event.listens_for(db.session, "after_commit")
    def _after_commit(session):
        ids = session.info.pop(_PENDING_KEY, [])
        if ids:
            # run outside the committing transaction
            session.info.setdefault("_dispatch_now", []).extend(ids)

    @event.listens_for(db.session, "after_rollback")
    def _after_rollback(session):
        session.info.pop(_PENDING_KEY, None)


def flush_dispatch_queue():
    ids = db.session.info.pop("_dispatch_now", [])
    while ids:
        dispatch_pending(ids)
        ids = db.session.info.pop("_dispatch_now", [])
