"""Small helpers shared by the API modules."""
import json

from flask import request

from app.core.errors import ValidationError


def form_or_json():
    """Body as a dict for JSON or multipart requests. JSON-encoded form fields
    (e.g. ``institution``) are decoded."""
    if request.is_json:
        return request.get_json(silent=True) or {}
    data = request.form.to_dict(flat=True)
    for key, value in list(data.items()):
        if isinstance(value, str) and value[:1] in ("{", "["):
            try:
                data[key] = json.loads(value)
            except ValueError as exc:
                raise ValidationError(f"Field {key} is not valid JSON") from exc
    return data


def arg_int(name, default=None):
    value = request.args.get(name)
    if value in (None, ""):
        return default
    try:
        return int(value)
    except ValueError as exc:
        raise ValidationError(f"Query parameter {name} must be an integer") from exc


def arg_bool(name, default=None):
    value = request.args.get(name)
    if value in (None, ""):
        return default
    return value.lower() in ("1", "true", "yes")
