"""Support tickets (12.3) and data-protection rights (11.1)."""
import io
import json
from datetime import timedelta

from flask import Blueprint, current_app, g, request, send_file
from marshmallow import Schema, fields, validate

from app.core.activity import activity
from app.core.api import api
from app.core.auth import end_all_sessions
from app.core.errors import BusinessRuleViolation, NotFound
from app.core.responses import created, ok, paginated
from app.core.utils import reference_number, utcnow
from app.core.validation import get_or_404, load
from app.enums import (AccountState, DataRequestState, DataRequestType, DocumentOwnerType, DocumentType,
                       NotificationEvent, Role, SessionEndReason, TicketCategory, TicketState)
from app.extensions import db
from app.models import (ConsentRecord, DataRequest, Document, MigrationRequest, StudentProfile, SupportTicket,
                        SupportTicketReply, User)
from app.services.directory import platform_admins
from app.services.documents import store_upload
from app.services.notifications import notify
from app.core.records import active, deactivate, user_public_dict

bp = Blueprint("support", __name__, url_prefix="")
EVERYONE = [Role.PLATFORM_ADMIN, Role.UNIVERSITY_ADMIN, Role.UNIVERSITY_USER, Role.COLLEGE_ADMIN,
            Role.COLLEGE_USER, Role.STUDENT]


class TicketSchema(Schema):
    category = fields.String(validate=validate.OneOf(TicketCategory.values()), load_default="GENERAL")
    subject = fields.String(required=True, validate=validate.Length(min=3, max=300))
    description = fields.String(required=True, validate=validate.Length(min=10, max=5000))
    related_request_id = fields.Integer()


class ReplySchema(Schema):
    message = fields.String(required=True, validate=validate.Length(min=1, max=5000))
    is_internal = fields.Boolean(load_default=False)


class TicketStateSchema(Schema):
    ticket_state = fields.String(required=True, validate=validate.OneOf(TicketState.values()))
    assigned_to_id = fields.Integer()


class DataRequestSchema(Schema):
    request_type = fields.String(required=True, validate=validate.OneOf(DataRequestType.values()))
    details = fields.String(validate=validate.Length(max=4000))


class CompleteDataRequestSchema(Schema):
    approve = fields.Boolean(required=True)
    response_note = fields.String(validate=validate.Length(max=4000))


def _ticket_view(t, include_internal=False):
    return {**t.to_dict(), "raised_by": t.raised_by_id,
            "replies": [r.to_dict() for r in t.replies if include_internal or not r.is_internal]}


def _get_ticket(ticket_id):
    t = get_or_404(SupportTicket, ticket_id, label="Ticket")
    if not (g.actor.is_platform_admin or t.raised_by_id == g.current_user.id):
        raise NotFound("Ticket not found")
    return t


@bp.post("/support-tickets")
@api("CREATE_SUPPORT_TICKET", roles=EVERYONE, allow_pending=True)
def create_ticket():
    from app.api.common import form_or_json
    data = load(TicketSchema, form_or_json())
    t = SupportTicket(reference_no=reference_number("TKT"), raised_by_id=g.current_user.id,
                      category=TicketCategory(data["category"]), subject=data["subject"],
                      description=data["description"], related_request_id=data.get("related_request_id"))
    db.session.add(t)
    db.session.flush()
    if request.files.get("attachment"):
        store_upload(request.files["attachment"], DocumentOwnerType.SUPPORT_TICKET, t.id, DocumentType.SUPPORT_ATTACHMENT)
    for pa in platform_admins():
        notify(pa, NotificationEvent.SUPPORT_TICKET_RAISED,
               {"requester": g.current_user.full_name, "reference": t.reference_no, "subject": t.subject})
    activity.step("Ticket raised", entity="support_ticket", entity_id=t.id)
    db.session.commit()
    return created(_ticket_view(t), "Ticket raised")


@bp.get("/support-tickets/mine")
@api("LIST_MY_SUPPORT_TICKETS", allow_pending=True)
def my_tickets():
    q = active(SupportTicket).filter_by(raised_by_id=g.current_user.id).order_by(SupportTicket.created_at.desc())
    return paginated(q, lambda t: t.to_dict())


@bp.get("/support-tickets/<int:ticket_id>")
@api("GET_SUPPORT_TICKET", allow_pending=True)
def get_ticket(ticket_id):
    t = _get_ticket(ticket_id)
    return ok(_ticket_view(t, include_internal=g.actor.is_platform_admin))


@bp.post("/support-tickets/<int:ticket_id>/replies")
@api("REPLY_SUPPORT_TICKET", allow_pending=True)
def reply(ticket_id):
    t = _get_ticket(ticket_id)
    data = load(ReplySchema)
    internal = data["is_internal"] and g.actor.is_platform_admin
    db.session.add(SupportTicketReply(ticket_id=t.id, author_id=g.current_user.id, message=data["message"],
                                      is_internal=internal))
    if g.actor.is_platform_admin and not internal:
        t.first_response_at = t.first_response_at or utcnow()
        if t.ticket_state == TicketState.OPEN:
            t.ticket_state = TicketState.IN_PROGRESS
        notify(t.raised_by_id, NotificationEvent.SUPPORT_TICKET_ANSWERED, {"reference": t.reference_no})
    db.session.commit()
    return created(_ticket_view(t, include_internal=g.actor.is_platform_admin), "Reply added")


@bp.get("/support-tickets")
@api("LIST_SUPPORT_QUEUE", roles=[Role.PLATFORM_ADMIN])
def queue():
    q = active(SupportTicket)
    if request.args.get("ticket_state"):
        q = q.filter(SupportTicket.ticket_state == TicketState(request.args["ticket_state"]))
    if request.args.get("category"):
        q = q.filter(SupportTicket.category == TicketCategory(request.args["category"]))
    return paginated(q.order_by(SupportTicket.created_at), lambda t: {
        **t.to_dict(), "overdue_first_response": t.first_response_at is None and
        utcnow() - t.created_at > timedelta(hours=current_app.config["SUPPORT_FIRST_RESPONSE_HOURS"])})


@bp.patch("/support-tickets/<int:ticket_id>")
@api("UPDATE_SUPPORT_TICKET", roles=[Role.PLATFORM_ADMIN])
def update_ticket(ticket_id):
    t = get_or_404(SupportTicket, ticket_id, label="Ticket")
    data = load(TicketStateSchema)
    t.ticket_state = TicketState(data["ticket_state"])
    if data.get("assigned_to_id"):
        t.assigned_to_id = data["assigned_to_id"]
    if t.ticket_state in (TicketState.RESOLVED, TicketState.CLOSED):
        t.resolved_at = t.resolved_at or utcnow()
    db.session.commit()
    return ok(t.to_dict(), "Ticket updated")


# ------------------------------------------------------------ data rights
def _export(user):
    profile = StudentProfile.query.filter_by(user_id=user.id).first()
    data = {"account": user_public_dict(user),
            "consents": [c.to_dict() for c in ConsentRecord.query.filter_by(user_id=user.id)]}
    if profile:
        data["profile"] = profile.to_dict()
        data["academic_records"] = [{**r.to_dict(), "subjects": [s.to_dict() for s in r.subjects]}
                                    for r in profile.academic_records]
        data["migration_requests"] = [r.to_dict() for r in MigrationRequest.query.filter_by(student_id=profile.id)]
    data["documents"] = [d.to_dict(exclude=("storage_key",)) for d in
                         Document.query.filter_by(owner_type=DocumentOwnerType.USER, owner_id=user.id)]
    return data


@bp.get("/privacy/my-data")
@api("EXPORT_MY_DATA", allow_pending=True)
def export_my_data():
    payload = json.dumps(_export(g.current_user), indent=2, default=str).encode()
    return send_file(io.BytesIO(payload), mimetype="application/json", as_attachment=True,
                     download_name=f"my-data-{g.current_user.id}.json")


@bp.post("/privacy/requests")
@api("CREATE_DATA_REQUEST", allow_pending=True)
def create_data_request():
    data = load(DataRequestSchema)
    row = DataRequest(user_id=g.current_user.id, request_type=DataRequestType(data["request_type"]),
                      details=data.get("details"),
                      due_at=utcnow() + timedelta(days=current_app.config["DATA_REQUEST_RESPONSE_DAYS"]))
    db.session.add(row)
    db.session.flush()
    for pa in platform_admins():
        notify(pa, NotificationEvent.GENERIC, {"subject": "Data protection request",
                                               "message": f"{g.current_user.full_name}: {row.request_type.value}"})
    db.session.commit()
    return created(row.to_dict(), "Request recorded; answered within 30 days")


@bp.get("/privacy/requests/mine")
@api("LIST_MY_DATA_REQUESTS", allow_pending=True)
def my_data_requests():
    q = active(DataRequest).filter_by(user_id=g.current_user.id).order_by(DataRequest.created_at.desc())
    return paginated(q, lambda r: r.to_dict())


@bp.get("/privacy/requests")
@api("LIST_DATA_REQUESTS", roles=[Role.PLATFORM_ADMIN])
def list_data_requests():
    q = active(DataRequest)
    if request.args.get("request_state"):
        q = q.filter(DataRequest.request_state == DataRequestState(request.args["request_state"]))
    return paginated(q.order_by(DataRequest.due_at), lambda r: r.to_dict())


@bp.post("/privacy/requests/<int:row_id>/complete")
@api("COMPLETE_DATA_REQUEST", roles=[Role.PLATFORM_ADMIN])
def complete_data_request(row_id):
    row = get_or_404(DataRequest, row_id, label="Data request")
    data = load(CompleteDataRequestSchema)
    if row.request_state != DataRequestState.OPEN:
        raise BusinessRuleViolation("Already completed")
    user = db.session.get(User, row.user_id)
    if data["approve"] and row.request_type == DataRequestType.ERASURE:
        _erase(user)
    row.request_state = DataRequestState.COMPLETED if data["approve"] else DataRequestState.REJECTED
    row.completed_at = utcnow()
    row.handled_by_id = g.current_user.id
    row.response_note = data.get("response_note")
    notify(user, NotificationEvent.GENERIC, {"subject": "Your data protection request",
                                             "message": f"Your {row.request_type.value.lower()} request is "
                                                        f"{row.request_state.value.lower()}. {row.response_note or ''}"})
    db.session.commit()
    return ok(row.to_dict(), "Request completed")


def _erase(user):
    """Anonymise the account. Records an institution must keep for its admission register stay (11.1)."""
    profile = StudentProfile.query.filter_by(user_id=user.id).first()
    if profile:
        admitted = MigrationRequest.query.filter(MigrationRequest.student_id == profile.id,
                                                 MigrationRequest.admitted_at.isnot(None)).first()
        if not admitted:
            for field in ("guardian_name", "guardian_email", "guardian_mobile", "address_line", "pincode",
                          "abc_account_id", "date_of_birth"):
                setattr(profile, field, None)
            profile.first_name, profile.last_name = "Erased", "User"
        deactivate(profile)
    end_all_sessions(user, SessionEndReason.DEACTIVATED)
    user.email = f"erased-{user.uuid}@erased.invalid"
    user.full_name, user.mobile = "Erased User", None
    user.password_hash = None
    user.account_state = AccountState.SUSPENDED
    deactivate(user)
    activity.step("Account erased/anonymised", entity="user", entity_id=user.id)


@bp.post("/privacy/withdraw-consent")
@api("WITHDRAW_CONSENT", roles=[Role.STUDENT])
def withdraw_consent():
    from app.services.migration import WITHDRAWABLE, _withdraw
    user = g.current_user
    profile = StudentProfile.query.filter_by(user_id=user.id).first()
    if profile:
        open_requests = MigrationRequest.query.filter(MigrationRequest.student_id == profile.id,
                                                      MigrationRequest.request_status.in_(list(WITHDRAWABLE))).all()
        for req in open_requests:
            _withdraw(req, "Consent withdrawn", user.id)
    for c in active(ConsentRecord).filter_by(user_id=user.id):
        c.withdrawn_at = utcnow()
        deactivate(c)
    end_all_sessions(user, SessionEndReason.DEACTIVATED)
    user.account_state = AccountState.SUSPENDED
    deactivate(user)
    db.session.commit()
    return ok(None, "Consent withdrawn and account closed")
