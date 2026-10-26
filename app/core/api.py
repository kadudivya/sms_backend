"""The ``@api`` decorator: unique API codes, authentication, authorisation.

Usage::

    @bp.post("/students")
    @api("CREATE_STUDENT", auth=False)
    def create_student(): ...

Every API has a unique, human-readable code (CREATE_STUDENT, APPROVE_ENTRY ...).
All codes and their descriptions live in ``config.API_CODES``. The code is returned
in every response and stored on the request's single ``user_activity`` row.
The app does not start if a route uses a code missing from config, two routes use
the same code, or a code in config has no route.
"""
from functools import wraps

from flask import g

from app.core.activity import activity
from app.core.auth import Actor, load_session_from_token
from app.core.errors import PermissionDenied
from app.enums import Role
from config import API_CODES

API_REGISTRY = {}


class DuplicateApiCode(RuntimeError):
    pass


class UnknownApiCode(RuntimeError):
    pass


def api(code, *, auth=True, permissions=None, roles=None,
        allow_pending=False, token_type="access", module=None):
    if code not in API_CODES:
        raise UnknownApiCode(f"API code {code} is not defined in config.API_CODES")
    if code in API_REGISTRY:
        raise DuplicateApiCode(f"API code {code} is used by two routes")
    description = API_CODES[code]

    def decorator(fn):
        API_REGISTRY[code] = {
            "code": code,
            "description": description,
            "auth": auth,
            "permissions": [p.value if hasattr(p, "value") else p for p in (permissions or [])],
            "roles": [r.value if hasattr(r, "value") else r for r in (roles or [])],
            "function": f"{fn.__module__}.{fn.__name__}",
            "module": module or fn.__module__.rsplit(".", 1)[-1],
            "endpoint": None,
            "methods": [],
            "path": None,
        }

        @wraps(fn)
        def wrapper(*args, **kwargs):
            g.api_code = code
            activity.step("Request received", api_code=code)
            if auth:
                user, session = load_session_from_token(token_type, allow_pending=allow_pending)
                g.current_user, g.current_session = user, session
                g.actor = Actor(user)
                activity.step("Authenticated", entity="user", entity_id=user.id)
                if roles and not (g.actor.roles() & {Role(r) for r in roles}):
                    raise PermissionDenied("Your role cannot use this API")
                for p in permissions or ():
                    if not g.actor.has_any(p):
                        raise PermissionDenied(f"Missing permission {getattr(p, 'value', p)}")
                activity.step("Authorised")
            return fn(*args, **kwargs)

        wrapper.api_code = code
        return wrapper

    return decorator


def bind_registry_to_routes(app):
    """Fill path/methods for each code from the URL map (for the catalogue)."""
    by_view = {}
    for code, entry in API_REGISTRY.items():
        by_view.setdefault(entry["function"], code)
    for rule in app.url_map.iter_rules():
        view = app.view_functions.get(rule.endpoint)
        code = getattr(view, "api_code", None)
        if code and code in API_REGISTRY:
            entry = API_REGISTRY[code]
            entry["endpoint"] = rule.endpoint
            entry["path"] = rule.rule
            entry["methods"] = sorted(m for m in rule.methods if m not in ("HEAD", "OPTIONS"))
    unused = sorted(set(API_CODES) - set(API_REGISTRY))
    if unused:
        raise UnknownApiCode("config.API_CODES lists codes with no route: " + ", ".join(unused))
    unbound = sorted(c for c, e in API_REGISTRY.items() if not e["path"])
    if unbound:
        raise UnknownApiCode("API codes not attached to a URL: " + ", ".join(unbound))
