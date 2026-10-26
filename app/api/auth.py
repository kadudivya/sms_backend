from flask import Blueprint, current_app, g
from marshmallow import Schema, fields, validate

from app.core.api import api
from app.core.responses import ok
from app.core.validation import load
from app.enums import NotificationChannel
from app.extensions import db, limiter
from app.services import auth_service
from app.core.records import is_student_user

bp = Blueprint("auth", __name__, url_prefix="/auth")


class LoginSchema(Schema):
    email = fields.Email(required=True)
    password = fields.String(required=True, validate=validate.Length(min=1, max=200))


class OtpSchema(Schema):
    otp = fields.String(required=True, validate=validate.Regexp(r"^\d{4,8}$"))


class EmailOtpSchema(OtpSchema):
    email = fields.Email(required=True)


class EmailSchema(Schema):
    email = fields.Email(required=True)


class ChangePasswordSchema(Schema):
    current_password = fields.String(required=True)
    new_password = fields.String(required=True)
    confirm_password = fields.String(required=True)


class ResetPasswordSchema(Schema):
    token = fields.String(required=True)
    new_password = fields.String(required=True)
    confirm_password = fields.String(required=True)


class PreferencesSchema(Schema):
    preferred_channel = fields.String(validate=validate.OneOf(NotificationChannel.values()))
    whatsapp_opt_in = fields.Boolean()
    language = fields.String(validate=validate.OneOf(["en", "hi"]))
    two_factor_enabled = fields.Boolean()
    mobile = fields.String(validate=validate.Regexp(r"^(\+91)?[6-9]\d{9}$"))


def _passwords_match(data, new_key="new_password"):
    from app.core.errors import ValidationError
    if data[new_key] != data["confirm_password"]:
        raise ValidationError("New password and confirmation do not match",
                              details={"fields": {"confirm_password": ["Does not match"]}})


@bp.post("/login")
@limiter.limit(lambda: current_app.config["RATELIMIT_LOGIN"])
@api("LOGIN", auth=False)
def login():
    data = load(LoginSchema)
    return ok(auth_service.login(data["email"], data["password"]), "Login processed")


@bp.post("/login/verify-otp")
@api("VERIFY_LOGIN_OTP", token_type="pre_auth",
     allow_pending=True)
def verify_login_otp():
    data = load(OtpSchema)
    return ok(auth_service.verify_login_otp(g.current_user, g.current_session, data["otp"]), "Logged in")


@bp.post("/login/resend-otp")
@api("RESEND_LOGIN_OTP", token_type="pre_auth", allow_pending=True)
def resend_login_otp():
    return ok(auth_service.resend_login_otp(g.current_user, g.current_session), "Code sent")


@bp.post("/logout")
@api("LOGOUT", allow_pending=True)
def logout():
    auth_service.logout(g.current_user, g.current_session)
    return ok(None, "Logged out")


@bp.get("/me")
@api("GET_CURRENT_USER", allow_pending=True)
def me():
    return ok(auth_service.user_summary(g.current_user))


@bp.post("/change-password")
@api("CHANGE_PASSWORD", allow_pending=True)
def change_password():
    data = load(ChangePasswordSchema)
    _passwords_match(data)
    auth_service.change_password(g.current_user, g.current_session, data["current_password"],
                                 data["new_password"])
    return ok(None, "Password changed")


@bp.post("/forgot-password")
@limiter.limit("10 per hour")
@api("FORGOT_PASSWORD", auth=False)
def forgot_password():
    data = load(EmailSchema)
    debug = auth_service.forgot_password(data["email"])
    return ok(debug or None, "If the email is registered, a reset link has been sent")


@bp.post("/reset-password")
@api("RESET_PASSWORD", auth=False)
def reset_password():
    data = load(ResetPasswordSchema)
    _passwords_match(data)
    auth_service.reset_password(data["token"], data["new_password"])
    return ok(None, "Password reset. Please log in.")


@bp.post("/verify-email")
@api("VERIFY_EMAIL", auth=False)
def verify_email():
    data = load(EmailOtpSchema)
    user = auth_service.verify_email(data["email"], data["otp"])
    return ok({"account_state": user.account_state.value}, "Email verified")


@bp.post("/resend-email-otp")
@limiter.limit("10 per hour")
@api("RESEND_EMAIL_OTP", auth=False)
def resend_email_otp():
    data = load(EmailSchema)
    return ok(auth_service.resend_email_otp(data["email"]) or None,
              "If verification is pending, a new code has been sent")


@bp.patch("/me/preferences")
@api("UPDATE_MY_PREFERENCES",
     allow_pending=True)
def update_preferences():
    from app.core.activity import activity
    from app.enums import NotificationChannel as NC
    data = load(PreferencesSchema)
    user = g.current_user
    for key, value in data.items():
        if key == "preferred_channel":
            value = NC(value)
        if key == "two_factor_enabled" and not value and g.actor.requires_two_factor() and not is_student_user(user):
            continue  # mandatory for admins and approvers
        activity.change("user", user.id, key, getattr(user, key), value)
        setattr(user, key, value)
    db.session.commit()
    return ok(auth_service.user_summary(user), "Preferences saved")


@bp.get("/sessions")
@api("LIST_MY_SESSIONS", allow_pending=True)
def my_sessions():
    from app.models import UserSession
    rows = (UserSession.query.filter_by(user_id=g.current_user.id)
            .order_by(UserSession.login_at.desc()).limit(20).all())
    return ok([{**s.to_dict(), "is_current": s.id == g.current_session.id} for s in rows])
