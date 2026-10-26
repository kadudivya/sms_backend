from datetime import datetime, time

from flask import Blueprint, current_app, g, request
from marshmallow import Schema, fields, validate
from sqlalchemy import func

from app.api.common import arg_int
from app.core.activity import activity
from app.core.api import API_REGISTRY, api
from app.core.auth import end_all_sessions
from app.core.errors import BusinessRuleViolation, PermissionDenied
from app.core.responses import ok, paginated
from app.core.utils import IST, utcnow
from app.core.validation import get_or_404, load
from app.enums import (AccountState, ActivityOutcome, NotificationState, Permission, Role,
                       SessionEndReason, UserType)
from app.extensions import db
from app.models import (InstitutionMembership, IpBlock, Notification, User, UserActivity,
                        UserSession)
from app.services.auth_service import user_summary
from app.services.onboarding import ensure_min_platform_admins
from app.core.records import activate, active, deactivate, user_public_dict

bp = Blueprint("admin", __name__, url_prefix="")
PA = [Role.PLATFORM_ADMIN]


class ReasonSchema(Schema):
    reason = fields.String(validate=validate.Length(max=500))


@bp.get("/health")
@api("HEALTH_CHECK", auth=False)
def health():
    db.session.execute(db.text("SELECT 1"))
    return ok({"status": "ok", "env": current_app.config["APP_ENV"], "time": utcnow().isoformat()})


@bp.get("/system/api-codes")
@api("LIST_API_CODES", auth=False)
def api_codes():
    rows = sorted(({k: v for k, v in e.items() if k != "function"} for e in API_REGISTRY.values()),
                  key=lambda e: (e["module"], e["code"]))
    return ok(rows, meta={"total": len(rows)})


@bp.get("/system/settings")
@api("GET_SYSTEM_SETTINGS", roles=PA)
def settings():
    hidden = ("SECRET", "PASSWORD", "TOKEN", "KEY", "DATABASE_URI", "AUTH")
    out = {}
    for k, v in current_app.config.items():
        if not k.isupper() or any(h in k for h in hidden):
            continue
        out[k] = v if isinstance(v, (str, int, float, bool, list, dict, type(None))) else str(v)
    return ok(out)


# ------------------------------------------------------------------ users
@bp.get("/users")
@api("LIST_USERS", roles=PA)
def list_users():
    q = User.query
    if request.args.get("user_type"):
        q = q.filter(User.user_type == UserType(request.args["user_type"]))
    if request.args.get("account_state"):
        q = q.filter(User.account_state == AccountState(request.args["account_state"]))
    if request.args.get("q"):
        term = f"%{request.args['q'].lower()}%"
        q = q.filter(db.or_(func.lower(User.email).like(term), func.lower(User.full_name).like(term),
                            User.mobile.like(term)))
    return paginated(q.order_by(User.created_at.desc()), lambda u: user_public_dict(u))


@bp.get("/users/<int:user_id>")
@api("GET_USER", roles=PA)
def get_user(user_id):
    user = get_or_404(User, user_id, label="User")
    sessions = UserSession.query.filter_by(user_id=user.id).order_by(UserSession.login_at.desc()).limit(10).all()
    return ok({**user_summary(user), "recent_sessions": [s.to_dict() for s in sessions]})


def _set_user_active(user, active, reason):
    if user.id == g.current_user.id:
        raise PermissionDenied("You cannot change your own account here")
    if not active and user.user_type == UserType.PLATFORM_ADMIN:
        ensure_min_platform_admins(user.id)
    if active:
        activate(user)
    else:
        deactivate(user)
        end_all_sessions(user, SessionEndReason.DEACTIVATED)
    activity.step("User " + ("activated" if active else "deactivated"), entity="user", entity_id=user.id,
                  reason=reason)
    db.session.commit()


@bp.post("/users/<int:user_id>/deactivate")
@api("DEACTIVATE_USER", roles=PA)
def deactivate_user(user_id):
    user = get_or_404(User, user_id, label="User")
    _set_user_active(user, False, load(ReasonSchema).get("reason"))
    return ok(user_public_dict(user), "User deactivated")


@bp.post("/users/<int:user_id>/activate")
@api("ACTIVATE_USER", roles=PA)
def activate_user(user_id):
    user = get_or_404(User, user_id, label="User")
    _set_user_active(user, True, load(ReasonSchema).get("reason"))
    return ok(user_public_dict(user), "User activated")


@bp.post("/users/<int:user_id>/force-logout")
@api("FORCE_LOGOUT_USER", roles=PA)
def force_logout(user_id):
    user = get_or_404(User, user_id, label="User")
    end_all_sessions(user, SessionEndReason.FORCED)
    db.session.commit()
    return ok(None, "Sessions ended")


@bp.post("/users/<int:user_id>/lock")
@api("LOCK_USER", roles=PA)
def lock_user(user_id):
    user = get_or_404(User, user_id, label="User")
    if user.id == g.current_user.id:
        raise PermissionDenied("You cannot lock yourself")
    user.account_state = AccountState.LOCKED
    end_all_sessions(user, SessionEndReason.FORCED)
    db.session.commit()
    return ok(user_public_dict(user), "Account locked")


@bp.post("/users/<int:user_id>/unlock")
@api("UNLOCK_USER", roles=PA)
def unlock_user(user_id):
    user = get_or_404(User, user_id, label="User")
    if user.account_state != AccountState.LOCKED and not user.locked_until:
        raise BusinessRuleViolation("The account is not locked")
    user.account_state = AccountState.ACTIVE if user.email_verified_at else AccountState.PENDING_VERIFICATION
    user.locked_until, user.failed_login_count, user.lockout_count = None, 0, 0
    db.session.commit()
    return ok(user_public_dict(user), "Account unlocked")


# --------------------------------------------------------------- activity
def _activity_scope(q):
    if g.actor.is_platform_admin:
        return q
    from app.core.institutions import ref_filter_many, scope_for
    universities, colleges = scope_for(g.actor, Permission.VIEW_AUDIT_LOGS)
    if not (universities or colleges):
        raise PermissionDenied("You cannot view audit logs")
    users = db.session.query(InstitutionMembership.user_id).filter(
        ref_filter_many(InstitutionMembership, universities, colleges))
    return q.filter(db.or_(ref_filter_many(UserActivity, universities, colleges), UserActivity.user_id.in_(users)))


@bp.get("/user-activity")
@api("LIST_USER_ACTIVITY",
     permissions=[Permission.VIEW_AUDIT_LOGS])
def list_activity():
    q = _activity_scope(UserActivity.query)
    if request.args.get("api_code"):
        q = q.filter(UserActivity.api_code == request.args["api_code"].upper())
    if arg_int("user_id"):
        q = q.filter(UserActivity.user_id == arg_int("user_id"))
    if request.args.get("outcome"):
        q = q.filter(UserActivity.outcome == ActivityOutcome(request.args["outcome"]))
    if request.args.get("entity_type"):
        q = q.filter(UserActivity.entity_type == request.args["entity_type"])
    if request.args.get("entity_id"):
        q = q.filter(UserActivity.entity_id == request.args["entity_id"])
    if request.args.get("date_from"):
        q = q.filter(UserActivity.created_at >= datetime.combine(
            datetime.fromisoformat(request.args["date_from"]).date(), time.min, tzinfo=IST))
    if request.args.get("date_to"):
        q = q.filter(UserActivity.created_at <= datetime.combine(
            datetime.fromisoformat(request.args["date_to"]).date(), time.max, tzinfo=IST))
    return paginated(q.order_by(UserActivity.created_at.desc()),
                     lambda a: a.to_dict(exclude=("request_payload", "steps", "changes")))


@bp.get("/user-activity/<int:row_id>")
@api("GET_USER_ACTIVITY", permissions=[Permission.VIEW_AUDIT_LOGS])
def get_activity(row_id):
    row = _activity_scope(UserActivity.query).filter(UserActivity.id == row_id).first()
    if row is None:
        from app.core.errors import NotFound
        raise NotFound("Activity row not found")
    return ok(row.to_dict())


# ----------------------------------------------------------- integrations
@bp.get("/integrations/failures")
@api("LIST_INTEGRATION_FAILURES", roles=PA)
def integration_failures():
    notes = (Notification.query.filter(Notification.delivery_state == NotificationState.FAILED)
             .order_by(Notification.updated_at.desc()).limit(200).all())
    return ok({"notifications": [n.to_dict(exclude=("payload", "body")) for n in notes]})


@bp.post("/notifications/<int:notification_id>/retry")
@api("RETRY_NOTIFICATION", roles=PA)
def retry_notification(notification_id):
    from app.services.notifications import dispatch_pending
    n = get_or_404(Notification, notification_id, label="Notification")
    n.delivery_state, n.attempts = NotificationState.QUEUED, 0
    db.session.commit()
    dispatch_pending([n.id])
    return ok(n.to_dict(exclude=("payload",)), "Retry queued")


@bp.get("/ip-blocks")
@api("LIST_IP_BLOCKS", roles=PA)
def ip_blocks():
    q = active(IpBlock).filter(IpBlock.blocked_until > utcnow()).order_by(IpBlock.created_at.desc())
    return paginated(q, lambda b: b.to_dict())


@bp.post("/ip-blocks/<int:block_id>/remove")
@api("REMOVE_IP_BLOCK", roles=PA)
def remove_ip_block(block_id):
    block = get_or_404(IpBlock, block_id, label="IP block")
    deactivate(block)
    db.session.commit()
    return ok(None, "IP block removed")
