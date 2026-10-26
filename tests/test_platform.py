"""Start-up checks, API codes from config, seed script, in-app jobs and notification retries."""
from datetime import timedelta

import pytest

import config
from app.core.api import API_REGISTRY, UnknownApiCode, api
from app.core.utils import utcnow
from app.enums import NotificationChannel, NotificationEvent, NotificationState, UserType
from app.extensions import db
from app.models import LetterTemplate, Notification, User


def test_every_api_code_comes_from_config(app, api):
    assert set(API_REGISTRY) == set(config.API_CODES)
    assert all(e["path"] for e in API_REGISTRY.values())
    _, body = api.get("/system/api-codes", expect=200)
    by_code = {r["code"]: r for r in body["data"]}
    assert by_code["CREATE_STUDENT"]["description"] == config.API_CODES["CREATE_STUDENT"]
    resp, _ = api.post("/students", json={})
    assert resp.headers["X-Api-Code"] == "CREATE_STUDENT"


def test_unknown_or_duplicate_codes_are_refused():
    with pytest.raises(UnknownApiCode):
        api("NOT_IN_CONFIG")
    with pytest.raises(Exception):
        api("CREATE_STUDENT")  # already used by a route
    with pytest.raises(ValueError):
        config._unique_codes([("A_CODE", "x"), ("A_CODE", "y")])


def test_tables_are_created_on_start(app):
    from sqlalchemy import inspect
    tables = set(inspect(db.engine).get_table_names())
    assert {"users", "universities", "colleges", "states", "districts", "migration_requests", "interviews", "user_activity"} <= tables
    from app import create_app
    create_app("testing", start_scheduler=False)  # a second start is harmless (create_all is checkfirst)


def test_seed_script(app, monkeypatch, capsys):
    from scripts import seed_data
    seed_data.main(["--env", "testing", "--generate-passwords",
                    "--admin", "ops1@platform.in", "Ops One", "--admin", "ops2@platform.in", "Ops Two"])
    db.session.remove()
    assert User.query.filter_by(user_type=UserType.PLATFORM_ADMIN).count() == 2
    assert LetterTemplate.query.count() > 0
    out = capsys.readouterr().out
    assert "temporary password" in out and "WARNING" not in out
    seed_data.main(["--env", "testing", "--admin", "ops1@platform.in", "Ops One"])  # re-run is safe
    assert "exists   ops1@platform.in" in capsys.readouterr().out


def test_job_runs_once_under_the_advisory_lock(app):
    from sqlalchemy import text
    from app.core.scheduler import _lock_key, run_job_once
    assert run_job_once(app, "expire_offers") is not None
    with db.engine.connect() as other:  # another process holds the lock: this tick is skipped
        other.execute(text("SELECT pg_advisory_lock(:k)"), {"k": _lock_key("expire_offers")})
        assert run_job_once(app, "expire_offers") is None
        other.execute(text("SELECT pg_advisory_unlock(:k)"), {"k": _lock_key("expire_offers")})


def test_failed_notifications_are_retried_by_the_job(app, monkeypatch):
    from app.services import channels
    from app.services.jobs import retry_notifications
    from app.services.notifications import deliver
    u = User(email="n@x.in", full_name="N", user_type=UserType.STAFF)
    db.session.add(u)
    db.session.commit()
    n = Notification(user_id=u.id, event=NotificationEvent.GENERIC, channel=NotificationChannel.EMAIL,
                     recipient=u.email, subject="s", body="b", payload={})
    db.session.add(n)
    db.session.commit()

    class Boom:
        def send(self, row):
            raise channels.ChannelError("smtp down")
    monkeypatch.setattr("app.services.notifications.channel_for", lambda c: Boom())
    assert deliver(n.id) is False
    assert n.delivery_state == NotificationState.RETRYING and n.next_attempt_at is not None
    assert retry_notifications()["tried"] == 0  # not due yet
    n.next_attempt_at = utcnow() - timedelta(seconds=1)
    db.session.commit()
    monkeypatch.setattr("app.services.notifications.channel_for", lambda c: type("Ok", (), {"send": lambda s, r: None})())
    assert retry_notifications() == {"tried": 1, "sent": 1}
    assert db.session.get(Notification, n.id).delivery_state == NotificationState.SENT


def test_background_threads_send_after_commit(app):
    """With NOTIFICATIONS_SEND_ASYNC on, rows are delivered by the in-app thread pool."""
    from app.core import background
    u = User(email="t@x.in", full_name="T", user_type=UserType.STAFF)
    db.session.add(u)
    db.session.commit()
    n = Notification(user_id=u.id, event=NotificationEvent.GENERIC, channel=NotificationChannel.IN_APP,
                     recipient=u.email, subject="s", body="b", payload={})
    db.session.add(n)
    db.session.commit()
    background.submit_notifications(app, [n.id])
    background._pool(app).shutdown(wait=True)  # wait for the thread to finish
    background._executor = None
    db.session.remove()
    assert db.session.get(Notification, n.id).delivery_state == NotificationState.SENT
