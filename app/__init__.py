"""Application factory.

On start-up the app: loads config.py, creates any missing tables with db.create_all(),
checks that every route's API code is defined in config.API_CODES (and every code there
has a route), and starts the in-app job scheduler.
"""
import logging

from flask import Flask, g, request
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm.exc import StaleDataError
from werkzeug.exceptions import HTTPException

from config import get_config

_dispatch_hook_registered = False


def create_app(config_name=None, start_scheduler=True):
    cfg = get_config(config_name)
    app = Flask(__name__)
    from app.core.json import ApiJSONProvider
    app.json = ApiJSONProvider(app)
    app.config.from_object(cfg)
    app.config["SQLALCHEMY_DATABASE_URI"] = cfg.database_uri()
    app.config.setdefault("RATELIMIT_ENABLED", not app.config.get("TESTING", False))
    logging.basicConfig(level=app.config["LOG_LEVEL"])
    logging.getLogger("apscheduler").setLevel(logging.WARNING)

    from app.extensions import cors, db, limiter
    db.init_app(app)
    limiter.init_app(app)
    cors.init_app(app, resources={r"/api/*": {"origins": app.config["CORS_ORIGINS"]}},
                  expose_headers=["Content-Disposition"])

    from app import models  # noqa: F401  (import the models so create_all sees every table)
    with app.app_context():
        db.create_all()
        db.engine.dispose()  # don't hand open connections to forked gunicorn workers (--preload)

    global _dispatch_hook_registered
    if not _dispatch_hook_registered:
        from app.core.audit import register_audit_hook
        from app.services.notifications import register_dispatch_hook
        register_dispatch_hook()
        register_audit_hook(db.session)
        _dispatch_hook_registered = True

    _register_request_hooks(app)
    _register_error_handlers(app)
    _register_blueprints(app)

    from app.core.api import bind_registry_to_routes
    bind_registry_to_routes(app)

    if start_scheduler:
        from app.core.scheduler import start_scheduler as _start
        _start(app)
    return app


def _register_request_hooks(app):
    from app.core.activity import activity, write_activity_row
    from app.extensions import db
    from app.services.notifications import flush_dispatch_queue

    prefix = app.config["API_PREFIX"]

    @app.before_request
    def _begin():
        for key in ("current_user", "current_session", "actor", "api_code", "_activity_error", "_activity"):
            g.pop(key, None)
        if request.path.startswith(prefix):
            activity.begin()

    @app.after_request
    def _finish(response):
        if not request.path.startswith(prefix):
            return response
        try:
            flush_dispatch_queue()
        except Exception:
            app.logger.exception("Notification dispatch failed")
            db.session.rollback()
        write_activity_row(response)
        response.headers["X-Request-Id"] = getattr(g, "request_id", "") or ""
        if getattr(g, "api_code", None):
            response.headers["X-Api-Code"] = g.api_code
        return response


def _register_error_handlers(app):
    from app.core.activity import record_error
    from app.core.errors import AppError
    from app.core.responses import error_response
    from app.extensions import db

    def _fail(code, message, status, details=None):
        db.session.rollback()
        record_error(code, message)
        return error_response(code, message, status, details)

    @app.errorhandler(AppError)
    def _app_error(exc):
        return _fail(exc.error_code, exc.message, exc.status_code, exc.details)

    @app.errorhandler(StaleDataError)
    def _stale(exc):
        return _fail("CONCURRENT_UPDATE", "This record was changed by someone else. Reload and try again.", 409)

    @app.errorhandler(IntegrityError)
    def _integrity(exc):
        app.logger.warning("Integrity error: %s", exc.orig)
        return _fail("CONFLICT", "This conflicts with an existing record", 409,
                     {"constraint": getattr(getattr(exc.orig, "diag", None), "constraint_name", None)})

    @app.errorhandler(HTTPException)
    def _http(exc):
        code = {404: "NOT_FOUND", 405: "METHOD_NOT_ALLOWED", 413: "PAYLOAD_TOO_LARGE",
                429: "RATE_LIMITED"}.get(exc.code, "HTTP_ERROR")
        return _fail(code, exc.description or exc.name, exc.code)

    @app.errorhandler(Exception)
    def _unhandled(exc):
        app.logger.exception("Unhandled error")
        return _fail("INTERNAL_ERROR", "Something went wrong. The error has been logged.", 500)


def _register_blueprints(app):
    from app.api import BLUEPRINTS
    prefix = app.config["API_PREFIX"]
    for bp in BLUEPRINTS:
        app.register_blueprint(bp, url_prefix=prefix + (bp.url_prefix or ""))
