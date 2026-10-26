from flask import request
from marshmallow import EXCLUDE
from marshmallow import ValidationError as MaValidationError

from app.core.activity import activity
from app.core.errors import ValidationError
from app.core.records import is_active


def load(schema_cls, data=None, partial=False, many=False):
    """Validate input with a marshmallow schema; raises a 422 with field errors."""
    if data is None:
        if request.is_json:
            data = request.get_json(silent=True) or {}
        else:
            data = request.form.to_dict(flat=True)
    try:
        result = schema_cls(unknown=EXCLUDE, partial=partial, many=many).load(data)
    except MaValidationError as exc:
        raise ValidationError("Some fields are invalid", details={"fields": exc.messages}) from exc
    activity.step("Validated input")
    return result


def get_or_404(model, pk, active_only=False, label=None):
    from app.core.errors import NotFound
    from app.extensions import db
    obj = db.session.get(model, pk)
    if obj is None or (active_only and not is_active(obj)):
        raise NotFound(f"{label or model.__name__} not found")
    return obj
