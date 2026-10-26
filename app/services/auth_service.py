"""Login, two-factor, logout, password change/reset, email verification (M01)."""
from datetime import timedelta

from flask import current_app, request
from sqlalchemy import func

from app.core.activity import activity
from app.core.auth import membership_institution, Actor, end_all_sessions, end_session, issue_token
from app.core.errors import AppError, AuthenticationError, ValidationError
from app.core.security import (generate_token, hash_password, hash_token, validate_password_policy,
                               verify_password)
from app.core.utils import ensure_aware, utcnow
from app.enums import (AccountState, ApprovalStatus, NotificationEvent, OtpPurpose, SessionEndReason,
                       TokenPurpose, UserType)
from app.extensions import db
from app.models import IpBlock, OneTimeToken, User, UserActivity, UserSession
from app.services import otp as otp_service
from app.services.notifications import notify
from app.core.records import active, deactivate, is_active, user_public_dict

GENERIC_LOGIN_ERROR = "Email or password is incorrect"


def _client_ip():
    return (request.headers.get("X-Forwarded-For", request.remote_addr) or "").split(",")[0].strip()


def _check_ip_block(ip):
    block = (active(IpBlock).filter(IpBlock.ip_address == ip, IpBlock.blocked_until > utcnow())
             .first())
    if block:
        raise AppError("Too many failed logins from your network. Try again later.",
                       error_code="IP_BLOCKED", status_code=429)


def _maybe_block_ip(ip):
    cfg = current_app.config
    since = utcnow() - cfg["SUSPICIOUS_WINDOW"]
    failures = (db.session.query(func.count(UserActivity.id))
                .filter(UserActivity.api_code == "LOGIN", UserActivity.outcome == "FAILURE",
                        UserActivity.ip_address == ip, UserActivity.created_at >= since).scalar())
    if failures + 1 >= cfg["SUSPICIOUS_FAILED_LOGINS_PER_IP"]:
        db.session.add(IpBlock(ip_address=ip, blocked_until=utcnow() + cfg["SUSPICIOUS_IP_BLOCK"],
                               reason="Repeated failed logins"))
        activity.step("Blocked IP after repeated failures", ip=ip)


def _register_failure(user):
    cfg = current_app.config
    now = utcnow()
    user.failed_login_count += 1
    activity.step("Password incorrect", entity="user", entity_id=user.id,
                  failed_count=user.failed_login_count)
    if user.failed_login_count >= cfg["MAX_FAILED_LOGIN_ATTEMPTS"]:
        window = ensure_aware(user.lockout_window_started_at)
        if window is None or now - window > timedelta(days=1):
            user.lockout_window_started_at = now
            user.lockout_count = 0
        user.lockout_count += 1
        user.failed_login_count = 0
        if user.lockout_count >= cfg["MAX_LOCKOUTS_PER_DAY"]:
            user.account_state = AccountState.LOCKED  # until password reset
            user.locked_until = None
            activity.step("Account locked until password reset", entity="user", entity_id=user.id)
            notify(user, NotificationEvent.ACCOUNT_LOCKED, {"until": "you reset your password"})
        else:
            user.locked_until = now + cfg["LOGIN_LOCKOUT_DURATION"]
            activity.step("Account temporarily locked", entity="user", entity_id=user.id,
                          until=user.locked_until)
            notify(user, NotificationEvent.ACCOUNT_LOCKED, {"until": user.locked_until.isoformat()})


def _new_session(user, two_factor_passed):
    session = UserSession(user_id=user.id, login_at=utcnow(), last_seen_at=utcnow(),
                          ip_address=_client_ip()[:100],
                          user_agent=(request.headers.get("User-Agent") or "")[:500],
                          two_factor_passed=two_factor_passed)
    db.session.add(session)
    db.session.flush()
    return session


def _complete_login(user, session):
    if current_app.config["SINGLE_ACTIVE_SESSION"]:
        end_all_sessions(user, SessionEndReason.NEW_LOGIN, except_session_id=session.id)
        activity.step("Ended previous sessions (single active session)")
    session.two_factor_passed = True
    user.last_login_at = utcnow()
    token = issue_token(user, session, "access")
    activity.step("Session started", entity="user_session", entity_id=session.id)
    return {"access_token": token, "token_type": "Bearer",
            "expires_in": int(current_app.config["JWT_ACCESS_TOKEN_EXPIRES"].total_seconds()),
            "two_factor_required": False, "user": user_summary(user)}


def login(email, password):
    ip = _client_ip()
    _check_ip_block(ip)
    user = User.query.filter(func.lower(User.email) == email.strip().lower()).first()
    if user is None or not is_active(user):
        _maybe_block_ip(ip)
        db.session.commit()
        raise AuthenticationError(GENERIC_LOGIN_ERROR, error_code="INVALID_CREDENTIALS")
    activity.set_entity("user", user.id)
    if user.account_state == AccountState.LOCKED:
        raise AuthenticationError("Account is locked. Reset your password to unlock it.",
                                  error_code="ACCOUNT_LOCKED")
    if user.locked_until and ensure_aware(user.locked_until) > utcnow():
        raise AuthenticationError("Account is temporarily locked. Try again later.",
                                  error_code="ACCOUNT_TEMPORARILY_LOCKED",
                                  details={"locked_until": user.locked_until.isoformat()})
    if not verify_password(user.password_hash, password):
        _register_failure(user)
        _maybe_block_ip(ip)
        db.session.commit()
        raise AuthenticationError(GENERIC_LOGIN_ERROR, error_code="INVALID_CREDENTIALS")
    activity.step("Password verified", entity="user", entity_id=user.id)
    if user.account_state == AccountState.PENDING_VERIFICATION:
        raise AuthenticationError("Verify your email address first", error_code="EMAIL_NOT_VERIFIED")
    if user.account_state in (AccountState.REJECTED, AccountState.SUSPENDED):
        raise AuthenticationError(f"Account is {user.account_state.value.lower()}",
                                  error_code="ACCOUNT_NOT_ACTIVE")
    user.failed_login_count = 0
    user.locked_until = None

    actor = Actor(user)
    if user.account_state == AccountState.ACTIVE and actor.requires_two_factor():
        session = _new_session(user, two_factor_passed=False)
        code = otp_service.issue_otp(user, OtpPurpose.LOGIN_2FA)
        activity.step("Two-factor code sent", entity="user_session", entity_id=session.id)
        db.session.commit()
        return {"two_factor_required": True,
                "pre_auth_token": issue_token(user, session, "pre_auth"),
                **otp_service.debug_payload(code)}
    session = _new_session(user, two_factor_passed=True)
    result = _complete_login(user, session)
    db.session.commit()
    return result


def verify_login_otp(user, session, code):
    otp_service.verify_otp(user, OtpPurpose.LOGIN_2FA, code)
    activity.step("Two-factor code verified")
    result = _complete_login(user, session)
    db.session.commit()
    return result


def resend_login_otp(user, session):
    code = otp_service.issue_otp(user, OtpPurpose.LOGIN_2FA)
    db.session.commit()
    return otp_service.debug_payload(code)


def logout(user, session):
    end_session(session, SessionEndReason.LOGOUT)
    activity.step("Session ended", entity="user_session", entity_id=session.id)
    db.session.commit()


def change_password(user, session, current, new):
    if not verify_password(user.password_hash, current):
        raise ValidationError("Current password is incorrect", error_code="WRONG_PASSWORD")
    _set_password(user, new)
    end_all_sessions(user, SessionEndReason.PASSWORD_CHANGED, except_session_id=session.id)
    notify(user, NotificationEvent.PASSWORD_CHANGED, {"when": utcnow().isoformat()})
    activity.step("Password changed; other sessions ended", entity="user", entity_id=user.id)
    db.session.commit()


def _set_password(user, new):
    validate_password_policy(new, [user.password_hash] + list(user.password_history or []))
    history = ([user.password_hash] if user.password_hash else []) + list(user.password_history or [])
    user.password_history = history[: current_app.config["PASSWORD_HISTORY_COUNT"]]
    user.password_hash = hash_password(new)
    user.password_changed_at = utcnow()


def forgot_password(email):
    """Same response whether or not the email exists (M01-F08)."""
    user = User.query.filter(func.lower(User.email) == email.strip().lower()).first()
    debug = {}
    if user and is_active(user):
        for old in active(OneTimeToken).filter_by(user_id=user.id, purpose=TokenPurpose.PASSWORD_RESET):
            deactivate(old)
        token = generate_token()
        expires = current_app.config["PASSWORD_RESET_TOKEN_EXPIRES"]
        db.session.add(OneTimeToken(user_id=user.id, purpose=TokenPurpose.PASSWORD_RESET,
                                    token_hash=hash_token(token), expires_at=utcnow() + expires))
        link = f"{current_app.config['FRONTEND_BASE_URL']}/reset-password?token={token}"
        notify(user, NotificationEvent.PASSWORD_RESET,
               {"link": link, "minutes": int(expires.total_seconds() // 60)}, link=link)
        activity.step("Reset link issued", entity="user", entity_id=user.id)
        if current_app.config["EXPOSE_DEBUG_OTP"]:
            debug = {"debug_token": token}
    db.session.commit()
    return debug


def reset_password(token, new):
    row = active(OneTimeToken).filter_by(token_hash=hash_token(token or ""),
                                          purpose=TokenPurpose.PASSWORD_RESET).first()
    if row is None or ensure_aware(row.expires_at) < utcnow():
        raise ValidationError("This reset link is invalid or has expired", error_code="TOKEN_INVALID")
    user = db.session.get(User, row.user_id)
    _set_password(user, new)
    row.used_at = utcnow()
    deactivate(row)
    user.failed_login_count = 0
    user.locked_until = None
    user.lockout_count = 0
    if user.account_state == AccountState.LOCKED:
        user.account_state = AccountState.ACTIVE
    end_all_sessions(user, SessionEndReason.PASSWORD_RESET)
    notify(user, NotificationEvent.PASSWORD_CHANGED, {"when": utcnow().isoformat()})
    activity.step("Password reset; all sessions ended", entity="user", entity_id=user.id)
    db.session.commit()


def state_after_email_verification(user):
    if user.user_type == UserType.STUDENT or user.user_type == UserType.PLATFORM_ADMIN:
        return AccountState.ACTIVE
    approved = any(m.approval_status == ApprovalStatus.APPROVED and is_active(m)
                   for m in user.memberships)
    return AccountState.ACTIVE if approved else AccountState.PENDING_APPROVAL


def verify_email(email, code):
    user = User.query.filter(func.lower(User.email) == email.strip().lower()).first()
    if user is None:
        raise ValidationError("The code is incorrect", error_code="OTP_INVALID")
    if user.email_verified_at:
        return user
    otp_service.verify_otp(user, OtpPurpose.EMAIL_VERIFICATION, code)
    user.email_verified_at = utcnow()
    user.account_state = state_after_email_verification(user)
    activity.step("Email verified", entity="user", entity_id=user.id, account_state=user.account_state)
    db.session.commit()
    return user


def resend_email_otp(email):
    user = User.query.filter(func.lower(User.email) == email.strip().lower()).first()
    if user is None or user.email_verified_at:
        return {}
    code = otp_service.issue_otp(user, OtpPurpose.EMAIL_VERIFICATION)
    db.session.commit()
    return otp_service.debug_payload(code)


def user_summary(user):
    actor = Actor(user)
    memberships = [{
        "membership_id": m.id,
        "institution_kind": "UNIVERSITY" if m.university_id else "COLLEGE",
        "institution_id": m.university_id or m.college_id,
        "institution_name": membership_institution(m).name,
        # UNIVERSITY, or the college row's type: COLLEGE / STANDALONE / PM_VIDYALAXMI / R&D
        "institution_type": "UNIVERSITY" if m.university_id else m.college.institution_type,
        "parent_university_id": None if m.university_id else m.college.university_id,
        "role": m.role.value,
        "is_primary": m.is_primary,
        "approval_status": m.approval_status.value,
        "permissions": sorted(p.value for p in actor.membership_permissions(m))
        if m.approval_status == ApprovalStatus.APPROVED else [],
    } for m in user.memberships if is_active(m)]
    return {
        **user_public_dict(user),
        "roles": sorted(r.value for r in actor.roles()),
        "memberships": memberships,
        "two_factor_required": actor.requires_two_factor(),
    }
