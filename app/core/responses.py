"""One response envelope for every API.

{
  "success": true,
  "api_code": "CREATE_STUDENT",
  "request_id": "...",
  "message": "...",
  "data": {...} | [...] | null,
  "errors": [] ,
  "meta": {"page": 1, "page_size": 20, "total": 125, "pages": 7}   # lists only
}
"""
from flask import current_app, g, jsonify, request

from app.core.errors import ValidationError


def _envelope(success, data=None, message="", errors=None, meta=None):
    body = {
        "success": success,
        "api_code": getattr(g, "api_code", None),
        "request_id": getattr(g, "request_id", None),
        "message": message,
        "data": data,
        "errors": errors or [],
    }
    if meta is not None:
        body["meta"] = meta
    return body


def ok(data=None, message="OK", status=200, meta=None):
    return jsonify(_envelope(True, data, message, meta=meta)), status


def created(data=None, message="Created"):
    return ok(data, message, 201)


def error_response(error_code, message, status, details=None):
    errors = [{"code": error_code, "message": message, "details": details or {}}]
    return jsonify(_envelope(False, None, message, errors)), status


def page_args():
    cfg = current_app.config
    try:
        page = max(int(request.args.get("page", 1)), 1)
        size = int(request.args.get("page_size", cfg["DEFAULT_PAGE_SIZE"]))
    except ValueError as exc:
        raise ValidationError("page and page_size must be integers") from exc
    size = max(1, min(size, cfg["MAX_PAGE_SIZE"]))
    return page, size


def paginated(query, serializer, message="OK"):
    page, size = page_args()
    total = query.order_by(None).count()
    items = query.limit(size).offset((page - 1) * size).all()
    meta = {"page": page, "page_size": size, "total": total, "pages": (total + size - 1) // size}
    return ok([serializer(i) for i in items], message, meta=meta)
