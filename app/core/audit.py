"""Fill created_by_id / updated_by_id on every insert and update with the signed-in user.

This is a database-session hook in app/core, not in the models: the models stay plain.
Outside a request (scripts, scheduled jobs) the columns are left as they are.
"""
from flask import g, has_request_context
from sqlalchemy import event


def _current_user_id():
    if has_request_context():
        user = getattr(g, "current_user", None)
        if user is not None:
            return user.id
    return None


def register_audit_hook(session):
    @event.listens_for(session, "before_flush")
    def _stamp(sess, flush_context, instances):
        user_id = _current_user_id()
        if user_id is None:
            return
        for row in sess.new:
            if hasattr(row, "created_by_id") and row.created_by_id is None:
                row.created_by_id = user_id
            if hasattr(row, "updated_by_id"):
                row.updated_by_id = user_id
        for row in sess.dirty:
            if hasattr(row, "updated_by_id") and sess.is_modified(row, include_collections=False):
                row.updated_by_id = user_id
