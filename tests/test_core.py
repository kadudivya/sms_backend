"""Core platform rules: soft-delete hook, single activity row, sessions, API codes."""
from app.core.api import API_REGISTRY
from app.enums import RecordStatus
from app.extensions import db
from app.models import UserActivity, UserSession

from .conftest import PASSWORD, login, make_platform_admin
from app.core.records import activate, deactivate


def test_inactive_fills_deleted_at_and_active_clears_it(app):
    user = make_platform_admin()
    assert user.status == RecordStatus.ACTIVE and user.deleted_at is None
    deactivate(user)
    db.session.commit()
    assert user.deleted_at is not None
    activate(user)
    db.session.commit()
    assert user.deleted_at is None


def test_every_api_code_is_unique_and_upper_snake(app):
    for code, entry in API_REGISTRY.items():
        assert code == code.upper() and " " not in code
        assert entry["path"], f"{code} is not routed"


def test_one_activity_row_per_request_with_steps(api):
    make_platform_admin()
    login(api, "pa@platform.in")
    rows = UserActivity.query.order_by(UserActivity.id).all()
    codes = [r.api_code for r in rows]
    assert codes == ["LOGIN", "VERIFY_LOGIN_OTP"]
    login_row = rows[0]
    steps = [s["step"] for s in login_row.steps]
    assert steps[0] == "Request received" and "Password verified" in steps and steps[-1] == "Request completed"
    assert login_row.request_payload["body"]["password"] == "***"
    assert login_row.created_by is None or isinstance(login_row.created_by, int)
    assert rows[1].created_by == rows[1].user_id and rows[1].updated_at >= rows[1].created_at


def test_failed_request_still_logged(api):
    api.post("/auth/login", json={"email": "nobody@x.in", "password": "x"}, expect=401)
    row = UserActivity.query.one()
    assert row.outcome.value == "FAILURE" and row.error_code == "INVALID_CREDENTIALS"
    assert row.steps[-1]["step"] == "Request failed"


def test_response_envelope_has_api_code(api):
    _, body = api.get("/states", expect=200)
    assert body["api_code"] == "LIST_STATES" and body["success"] is True and body["request_id"]


def test_new_login_ends_previous_session(api):
    make_platform_admin()
    first = login(api, "pa@platform.in")
    second = login(api, "pa@platform.in")
    api.get("/auth/me", token=second, expect=200)
    _, body = api.get("/auth/me", token=first, expect=401)
    assert body["errors"][0]["code"] == "SESSION_ENDED"
    ended = UserSession.query.filter_by(status=RecordStatus.INACTIVE).all()
    assert ended and all(s.deleted_at is not None for s in ended)


def test_lockout_after_three_failures(api):
    make_platform_admin()
    for _ in range(3):
        api.post("/auth/login", json={"email": "pa@platform.in", "password": "wrong"}, expect=401)
    _, body = api.post("/auth/login", json={"email": "pa@platform.in", "password": PASSWORD}, expect=401)
    assert body["errors"][0]["code"] == "ACCOUNT_TEMPORARILY_LOCKED"


def test_password_reset_flow(api):
    make_platform_admin()
    _, body = api.post("/auth/forgot-password", json={"email": "pa@platform.in"}, expect=200)
    token = body["data"]["debug_token"]
    api.post("/auth/reset-password", json={"token": token, "new_password": "N3w!Password",
                                           "confirm_password": "N3w!Password"}, expect=200)
    login(api, "pa@platform.in", "N3w!Password")
    _, body = api.post("/auth/reset-password", json={"token": token, "new_password": "N3w!Password2",
                                                     "confirm_password": "N3w!Password2"}, expect=422)


def test_unknown_route_is_enveloped(api):
    resp, body = api.get("/does-not-exist")
    assert resp.status_code == 404 and body["success"] is False


def test_created_by_is_filled_and_dates_are_iso(api):
    from app.models import Invitation, User
    from .conftest import login, make_platform_admin
    from .conftest import seed_geo, seed_master_institution
    pa = make_platform_admin()
    _, pune, _ = seed_geo()
    seed_master_institution("U-0009", "Test University", "UNIVERSITY", district=pune)
    tok = login(api, "pa@platform.in")
    api.post("/onboarding/universities", token=tok, expect=201,
             json={"aishe_code": "U-0009", "admin_email": "reg@tu.ac.in", "admin_name": "Reg Istrar"})
    inv = Invitation.query.filter_by(email="reg@tu.ac.in").one()
    assert inv.created_by_id == pa.id and inv.updated_by_id == pa.id
    _, body = api.get("/auth/me", token=tok, expect=200)
    assert "T" in body["data"]["created_at"] and body["data"]["status"] == "ACTIVE"
    assert "password_hash" not in body["data"]
    assert User.query.get(pa.id).to_dict()["user_type"] == "PLATFORM_ADMIN"  # enums as .value
