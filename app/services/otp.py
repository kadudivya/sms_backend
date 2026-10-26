from datetime import timedelta

from flask import current_app

from app.core.errors import ValidationError
from app.core.security import constant_time_equals, generate_otp, hash_otp
from app.core.utils import ensure_aware, utcnow
from app.enums import NotificationChannel, NotificationEvent, OtpPurpose
from app.extensions import db
from app.models import OtpCode
from app.services.notifications import notify, notify_email_address
from app.core.records import active, deactivate


def issue_otp(user, purpose, target=None, send_to_email=None):
    """Create a fresh OTP (older ones for the same purpose are retired)."""
    purpose = OtpPurpose(purpose)
    for old in active(OtpCode).filter_by(user_id=user.id, purpose=purpose).all():
        deactivate(old)
    code = generate_otp()
    minutes = int(current_app.config["OTP_EXPIRES"].total_seconds() // 60)
    target = target or user.email
    db.session.add(OtpCode(user_id=user.id, purpose=purpose, target=target, code_hash=hash_otp(code),
                           expires_at=utcnow() + timedelta(minutes=minutes)))
    ctx = {"otp": code, "minutes": minutes}
    if send_to_email:
        notify_email_address(send_to_email, NotificationEvent.OTP, ctx)
    else:
        notify(user, NotificationEvent.OTP, ctx, channels=[NotificationChannel.EMAIL])
    return code


def verify_otp(user, purpose, code):
    purpose = OtpPurpose(purpose)
    row = (active(OtpCode).filter_by(user_id=user.id, purpose=purpose)
           .order_by(OtpCode.created_at.desc()).first())
    if row is None:
        raise ValidationError("No active code. Request a new one.", error_code="OTP_NOT_FOUND")
    if ensure_aware(row.expires_at) < utcnow():
        deactivate(row)
        db.session.commit()
        raise ValidationError("The code has expired. Request a new one.", error_code="OTP_EXPIRED")
    if row.attempts >= current_app.config["OTP_MAX_ATTEMPTS"]:
        deactivate(row)
        db.session.commit()
        raise ValidationError("Too many wrong attempts. Request a new code.", error_code="OTP_LOCKED")
    if not constant_time_equals(row.code_hash, hash_otp(str(code or ""))):
        row.attempts += 1
        db.session.commit()
        raise ValidationError("The code is incorrect", error_code="OTP_INVALID")
    row.consumed_at = utcnow()
    deactivate(row)
    return True


def debug_payload(code):
    return {"debug_otp": code} if current_app.config["EXPOSE_DEBUG_OTP"] else {}
