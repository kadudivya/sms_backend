from flask import Blueprint, g, request

from app.api.common import arg_int
from app.core.api import api
from app.core.responses import ok, page_args, paginated
from app.core.utils import utcnow
from app.core.validation import get_or_404
from app.enums import NotificationChannel, NotificationEvent, NotificationState, Permission, Role
from app.extensions import db
from app.models import Notification
from app.core.records import active

bp = Blueprint("notifications", __name__, url_prefix="/notifications")
EVERYONE = [Role.PLATFORM_ADMIN, Role.UNIVERSITY_ADMIN, Role.UNIVERSITY_USER, Role.COLLEGE_ADMIN,
            Role.COLLEGE_USER, Role.STUDENT]


def _view(n):
    return n.to_dict(exclude=("payload", "idempotency_key", "recipient"))


@bp.get("")
@api("LIST_MY_NOTIFICATIONS", allow_pending=True)
def mine():
    q = active(Notification).filter_by(user_id=g.current_user.id, channel=NotificationChannel.IN_APP)
    if request.args.get("unread") == "true":
        q = q.filter(Notification.read_at.is_(None))
    unread = active(Notification).filter_by(user_id=g.current_user.id, channel=NotificationChannel.IN_APP,
                                             read_at=None).count()
    page, size = page_args()
    total = q.count()
    items = q.order_by(Notification.created_at.desc()).limit(size).offset((page - 1) * size).all()
    return ok([_view(n) for n in items], meta={"page": page, "page_size": size, "total": total,
                                               "pages": (total + size - 1) // size, "unread": unread})


@bp.post("/<int:notification_id>/read")
@api("MARK_NOTIFICATION_READ", allow_pending=True)
def mark_read(notification_id):
    n = get_or_404(Notification, notification_id, label="Notification")
    if n.user_id != g.current_user.id:
        from app.core.errors import NotFound
        raise NotFound("Notification not found")
    n.read_at = n.read_at or utcnow()
    db.session.commit()
    return ok(_view(n))


@bp.post("/read-all")
@api("MARK_ALL_NOTIFICATIONS_READ", allow_pending=True)
def mark_all():
    rows = active(Notification).filter_by(user_id=g.current_user.id, channel=NotificationChannel.IN_APP,
                                           read_at=None).all()
    for n in rows:
        n.read_at = utcnow()
    db.session.commit()
    return ok({"marked": len(rows)})


@bp.get("/history")
@api("LIST_NOTIFICATION_HISTORY",
     roles=[Role.PLATFORM_ADMIN, Role.UNIVERSITY_ADMIN, Role.COLLEGE_ADMIN])
def history():
    q = active(Notification)
    if not g.actor.is_platform_admin:
        from app.models import InstitutionMembership
        from app.core.institutions import ref_filter_many, scope_for
        universities, colleges = scope_for(g.actor, Permission.VIEW_AUDIT_LOGS)
        users = db.session.query(InstitutionMembership.user_id).filter(
            ref_filter_many(InstitutionMembership, universities, colleges))
        q = q.filter(Notification.user_id.in_(users))
    for arg, col, enum_cls in (("channel", Notification.channel, NotificationChannel),
                               ("state", Notification.delivery_state, NotificationState),
                               ("event", Notification.event, NotificationEvent)):
        if request.args.get(arg):
            q = q.filter(col == enum_cls(request.args[arg]))
    if arg_int("user_id"):
        q = q.filter(Notification.user_id == arg_int("user_id"))
    return paginated(q.order_by(Notification.created_at.desc()),
                     lambda n: n.to_dict(exclude=("payload", "idempotency_key", "body")))
