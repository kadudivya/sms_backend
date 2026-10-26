"""Single-row user activity logging.

Each API request produces exactly ONE row in ``user_activity``. During the
request, code records the major steps with::

    from app.core.activity import activity
    activity.step("Validated input")
    activity.step("Created student profile", entity="student_profile", entity_id=sp.id)
    activity.change("migration_request", req.id, "status", old, new)

The row is written after the response is built, on its own connection, so it
is kept even when the request failed and its transaction was rolled back.
"""
import json
import time
import uuid
from datetime import datetime, timezone

from flask import current_app, g, has_request_context, request



def serialize_value(value):
    """Make a value storable in the JSON steps/changes columns."""
    import enum
    from datetime import date, time
    from decimal import Decimal
    if isinstance(value, enum.Enum):
        return value.value
    if isinstance(value, (datetime, date, time)):
        return value.isoformat()
    if isinstance(value, Decimal):
        return float(value)
    return value


def _mask(value, sensitive):
    if isinstance(value, dict):
        return {k: ("***" if k.lower() in sensitive else _mask(v, sensitive)) for k, v in value.items()}
    if isinstance(value, list):
        return [_mask(v, sensitive) for v in value]
    return value


class _ActivityRecorder:
    """Proxy bound to the current request (lives on flask.g)."""

    def _state(self):
        if not has_request_context():
            return None
        return getattr(g, "_activity", None)

    def begin(self):
        g._activity = {
            "request_id": str(uuid.uuid4()),
            "started": time.monotonic(),
            "started_at": datetime.now(timezone.utc),
            "steps": [],
            "entity_type": None,
            "entity_id": None,
            "university_id": None,
            "college_id": None,
            "changes": [],
        }
        g.request_id = g._activity["request_id"]

    def step(self, description, entity=None, entity_id=None, **detail):
        state = self._state()
        if state is None:
            return
        item = {
            "at": datetime.now(timezone.utc).isoformat(),
            "elapsed_ms": int((time.monotonic() - state["started"]) * 1000),
            "step": description,
        }
        if entity:
            item["entity"] = entity
            item["entity_id"] = entity_id
            if state["entity_type"] is None:
                state["entity_type"], state["entity_id"] = entity, entity_id
        if detail:
            item["detail"] = {k: serialize_value(v) for k, v in detail.items()}
        state["steps"].append(item)

    def change(self, entity, entity_id, field, old, new):
        state = self._state()
        if state is None:
            return
        state["changes"].append({
            "entity": entity, "entity_id": entity_id, "field": field,
            "old": serialize_value(old), "new": serialize_value(new),
        })

    def set_entity(self, entity, entity_id):
        state = self._state()
        if state is not None:
            state["entity_type"], state["entity_id"] = entity, entity_id

    def set_institution(self, institution):
        """Tag this request's activity row with a University or College."""
        from app.core.institutions import ref
        state = self._state()
        if state is not None and institution is not None:
            state.update(ref(institution))


activity = _ActivityRecorder()


def _request_payload(sensitive):
    payload = {}
    if request.args:
        payload["query"] = _mask(request.args.to_dict(flat=True), sensitive)
    if request.is_json:
        body = request.get_json(silent=True)
        if body is not None:
            payload["body"] = _mask(body, sensitive)
    elif request.form:
        payload["form"] = _mask(request.form.to_dict(flat=True), sensitive)
    if request.files:
        payload["files"] = [
            {"field": k, "filename": f.filename, "mimetype": f.mimetype}
            for k, f in request.files.items(multi=True)
        ]
    raw = json.dumps(payload, default=str)
    if len(raw) > 20000:  # keep rows small
        return {"truncated": True, "size": len(raw)}
    return payload


def write_activity_row(response=None, error=None):
    """Persist the request's single activity row on a separate connection."""
    state = getattr(g, "_activity", None)
    api_code = getattr(g, "api_code", None)
    if state is None or api_code is None:
        return
    from app.extensions import db
    from app.models import UserActivity

    cfg = current_app.config
    sensitive = {s.lower() for s in cfg["SENSITIVE_FIELDS"]}
    user = getattr(g, "current_user", None)
    session = getattr(g, "current_session", None)
    status_code = response.status_code if response is not None else 500
    failed = status_code >= 400
    err = getattr(g, "_activity_error", None) or {}
    now = datetime.now(timezone.utc)
    steps = list(state["steps"])
    steps.append({
        "at": now.isoformat(),
        "elapsed_ms": int((time.monotonic() - state["started"]) * 1000),
        "step": "Request failed" if failed else "Request completed",
        "detail": {"http_status": status_code},
    })
    row = {
        "request_id": state["request_id"],
        "api_code": api_code,
        "http_method": request.method,
        "path": request.path[:500],
        "user_id": user.id if user is not None else None,
        "session_id": session.id if session is not None else None,
        "university_id": state["university_id"],
        "college_id": state["college_id"],
        "ip_address": (request.headers.get("X-Forwarded-For", request.remote_addr) or "")[:100],
        "user_agent": (request.headers.get("User-Agent") or "")[:500],
        "request_payload": _request_payload(sensitive),
        "steps": steps,
        "changes": state["changes"] or None,
        "entity_type": state["entity_type"],
        "entity_id": str(state["entity_id"]) if state["entity_id"] is not None else None,
        "outcome": "FAILURE" if failed else "SUCCESS",
        "http_status": status_code,
        "error_code": err.get("code"),
        "error_message": (err.get("message") or "")[:1000] or None,
        "duration_ms": int((time.monotonic() - state["started"]) * 1000),
        "created_by": user.id if user is not None else None,
        "updated_by": user.id if user is not None else None,
        "created_at": state["started_at"],
        "updated_at": now,
    }
    try:
        with db.engine.begin() as conn:
            conn.execute(UserActivity.__table__.insert().values(**row))
    except Exception:  # never let logging break the response
        current_app.logger.exception("Could not write user_activity row for %s", api_code)


def record_error(code, message):
    if has_request_context():
        g._activity_error = {"code": code, "message": message}
