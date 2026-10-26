"""JWT + session authentication and the Actor (who is calling, what they may do)."""
from datetime import datetime, timedelta, timezone

import jwt
from flask import current_app, g, request

from app.core.errors import AuthenticationError, PermissionDenied
from app.core.permissions import (APPROVAL_PERMISSIONS, COLLEGE_ADMIN_PERMISSIONS,
                                  COLLEGE_USER_GRANTABLE, UNIVERSITY_ADMIN_PERMISSIONS,
                                  UNIVERSITY_ON_COLLEGE, UNIVERSITY_USER_GRANTABLE, expand)
from app.enums import (AccountState, ApprovalStatus, MembershipRole,
                       Permission, RecordStatus, Role, SessionEndReason, UserType)
from app.extensions import db
from app.core.institutions import parent_university_id, points_at
from app.core.records import active, deactivate, is_active


def membership_institution(m):
    """The University or College a membership belongs to."""
    return m.university if m.university_id else m.college


def _now():
    return datetime.now(timezone.utc)


# ------------------------------------------------------------------ tokens
def issue_token(user, session, token_type="access"):
    cfg = current_app.config
    lifetime = (cfg["JWT_ACCESS_TOKEN_EXPIRES"] if token_type == "access"
                else cfg["JWT_PRE_AUTH_TOKEN_EXPIRES"])
    now = _now()
    payload = {
        "sub": str(user.id),
        "sid": session.uuid,
        "typ": token_type,
        "iat": int(now.timestamp()),
        "exp": int((now + lifetime).timestamp()),
    }
    return jwt.encode(payload, cfg["JWT_SECRET_KEY"], algorithm=cfg["JWT_ALGORITHM"])


def decode_token(token, expected_type="access"):
    cfg = current_app.config
    try:
        payload = jwt.decode(token, cfg["JWT_SECRET_KEY"], algorithms=[cfg["JWT_ALGORITHM"]])
    except jwt.ExpiredSignatureError as exc:
        raise AuthenticationError("Token has expired", error_code="TOKEN_EXPIRED") from exc
    except jwt.InvalidTokenError as exc:
        raise AuthenticationError("Token is invalid", error_code="TOKEN_INVALID") from exc
    if payload.get("typ") != expected_type:
        raise AuthenticationError("Wrong token type", error_code="TOKEN_INVALID")
    return payload


def _bearer():
    header = request.headers.get("Authorization", "")
    if not header.lower().startswith("bearer "):
        raise AuthenticationError("Missing bearer token", error_code="TOKEN_MISSING")
    return header.split(" ", 1)[1].strip()


def end_session(session, reason):
    if session.status == RecordStatus.ACTIVE:
        session.logout_at = _now()
        session.end_reason = reason
        deactivate(session)


def end_all_sessions(user, reason, except_session_id=None):
    from app.models import UserSession
    q = active(UserSession).filter(UserSession.user_id == user.id)
    if except_session_id:
        q = q.filter(UserSession.id != except_session_id)
    for s in q.all():
        end_session(s, reason)


def load_session_from_token(token_type="access", allow_pending=False):
    """Validate token + session row; returns (user, session)."""
    from app.models import User, UserSession

    payload = decode_token(_bearer(), token_type)
    session = UserSession.query.filter_by(uuid=payload["sid"]).first()
    if session is None or session.status != RecordStatus.ACTIVE:
        raise AuthenticationError("Session has ended. Please log in again.",
                                  error_code="SESSION_ENDED")
    idle = current_app.config["SESSION_IDLE_TIMEOUT"]
    if session.last_seen_at and _now() - session.last_seen_at > idle:
        end_session(session, SessionEndReason.EXPIRED)
        db.session.commit()
        raise AuthenticationError("Session expired after inactivity", error_code="SESSION_EXPIRED")
    user = db.session.get(User, int(payload["sub"]))
    if user is None or user.id != session.user_id or not is_active(user):
        raise AuthenticationError("Account not available", error_code="ACCOUNT_UNAVAILABLE")
    allowed = {AccountState.ACTIVE}
    if allow_pending:
        allowed |= {AccountState.PENDING_APPROVAL}
    if user.account_state not in allowed:
        raise AuthenticationError(f"Account is {user.account_state.value.lower()}",
                                  error_code="ACCOUNT_NOT_ACTIVE")
    if token_type == "access" and not session.two_factor_passed and Actor(user).requires_two_factor():
        raise AuthenticationError("Two-factor verification required", error_code="TWO_FACTOR_REQUIRED")
    # throttle last_seen writes to once a minute
    if not session.last_seen_at or _now() - session.last_seen_at > timedelta(minutes=1):
        session.last_seen_at = _now()
        db.session.commit()
    return user, session


# ------------------------------------------------------------------- actor
class Actor:
    """Permissions are read from the database on every request (INT-11)."""

    def __init__(self, user):
        self.user = user
        self._memberships = None

    @property
    def is_platform_admin(self):
        return self.user.user_type == UserType.PLATFORM_ADMIN

    @property
    def is_student(self):
        return self.user.user_type == UserType.STUDENT

    @property
    def memberships(self):
        if self._memberships is None:
            self._memberships = [
                m for m in self.user.memberships
                if m.status == RecordStatus.ACTIVE
                and m.approval_status == ApprovalStatus.APPROVED
                and is_active(membership_institution(m))
            ]
        return self._memberships

    def membership_permissions(self, m):
        uni = m.university_id is not None
        if m.role == MembershipRole.ADMIN:
            return set(UNIVERSITY_ADMIN_PERMISSIONS if uni else COLLEGE_ADMIN_PERMISSIONS)
        grantable = UNIVERSITY_USER_GRANTABLE if uni else COLLEGE_USER_GRANTABLE
        return expand(m.bundles, m.permissions) & set(grantable)

    def roles(self):
        if self.is_platform_admin:
            return {Role.PLATFORM_ADMIN}
        if self.is_student:
            return {Role.STUDENT}
        out = set()
        for m in self.memberships:
            uni = m.university_id is not None
            if m.role == MembershipRole.ADMIN:
                out.add(Role.UNIVERSITY_ADMIN if uni else Role.COLLEGE_ADMIN)
            else:
                out.add(Role.UNIVERSITY_USER if uni else Role.COLLEGE_USER)
        return out

    def university_ids(self):
        return {m.university_id for m in self.memberships if m.university_id}

    def college_ids(self):
        return {m.college_id for m in self.memberships if m.college_id}

    def institutions(self):
        """The University / College rows the user works at."""
        return [membership_institution(m) for m in self.memberships]

    def membership_for(self, institution):
        for m in self.memberships:
            if points_at(m, institution):
                return m
        return None

    def admin_of(self, institution):
        m = self.membership_for(institution)
        return m is not None and m.role == MembershipRole.ADMIN

    def has_any(self, permission):
        """Holds the permission somewhere (coarse check used by the decorator)."""
        if self.is_platform_admin:
            return True
        permission = Permission(permission)
        return any(permission in self.membership_permissions(m) for m in self.memberships)

    def can(self, permission, institution):
        """Holds ``permission`` for ``institution`` (a University or College row).

        University members also hold UNIVERSITY_ON_COLLEGE permissions on their colleges.
        """
        if self.is_platform_admin:
            return True
        if institution is None:
            return False
        if isinstance(institution, int):
            raise TypeError("Actor.can() needs a University or College row, not an id")
        permission = Permission(permission)
        parent_id = parent_university_id(institution)
        for m in self.memberships:
            if points_at(m, institution) and permission in self.membership_permissions(m):
                return True
            if (parent_id and m.university_id == parent_id and permission in UNIVERSITY_ON_COLLEGE
                    and permission in self.membership_permissions(m)):
                return True
        return False

    def require(self, permission, institution):
        if not self.can(permission, institution):
            raise PermissionDenied(f"You do not have {Permission(permission).value} for this institution")

    def can_view_institution(self, institution):
        if self.is_platform_admin:
            return True
        parent_id = parent_university_id(institution)
        for m in self.memberships:
            if points_at(m, institution) or (parent_id and m.university_id == parent_id):
                return True
        return False

    def require_view(self, institution):
        if not self.can_view_institution(institution):
            raise PermissionDenied("This record belongs to another institution")

    def all_permissions(self):
        if self.is_platform_admin:
            return set(Permission)
        out = set()
        for m in self.memberships:
            out |= self.membership_permissions(m)
        return out

    def requires_two_factor(self):
        if self.user.two_factor_enabled:
            return True
        if not current_app.config["TWO_FACTOR_REQUIRED_FOR_ADMINS"]:
            return False
        if self.is_platform_admin:
            return True
        if any(m.role == MembershipRole.ADMIN for m in self.memberships):
            return True
        return bool(self.all_permissions() & APPROVAL_PERMISSIONS)


def current_actor():
    actor = getattr(g, "actor", None)
    if actor is None:
        raise AuthenticationError("Not authenticated")
    return actor


def current_user():
    return current_actor().user
