from flask import Blueprint, g
from marshmallow import Schema, fields, validate

from app.api.common import arg_int
from app.core.api import api
from app.core.errors import NotFound, PermissionDenied
from app.core.responses import ok
from app.core.storage import signed_download_url
from app.core.validation import get_or_404, load
from app.enums import LetterType, Permission, Role
from app.extensions import db
from app.core.institutions import target
from app.models import Letter, LetterTemplate, University
from app.services import migration as migration_svc
from app.services.letters import DEFAULT_TEMPLATES
from app.core.records import activate, active

bp = Blueprint("letters", __name__, url_prefix="")
EVERYONE = [Role.PLATFORM_ADMIN, Role.UNIVERSITY_ADMIN, Role.UNIVERSITY_USER, Role.COLLEGE_ADMIN,
            Role.COLLEGE_USER, Role.STUDENT]


class TemplateSchema(Schema):
    letter_type = fields.String(required=True, validate=validate.OneOf(LetterType.values()))
    university_id = fields.Integer(allow_none=True)
    subject = fields.String(required=True, validate=validate.Length(min=3, max=300))
    body = fields.String(required=True, validate=validate.Length(min=10, max=10000))


@bp.get("/migration-requests/<int:req_id>/letters")
@api("LIST_REQUEST_LETTERS", roles=EVERYONE)
def list_letters(req_id):
    req = migration_svc.get_request(req_id)
    migration_svc.require_view(g.actor, req)
    rows = active(Letter).filter_by(request_id=req.id).order_by(Letter.issued_at).all()
    return ok([r.to_dict(exclude=("storage_key", "qr_token")) for r in rows])


@bp.get("/letters/<int:letter_id>/link")
@api("GET_LETTER_LINK", roles=EVERYONE)
def letter_link(letter_id):
    letter = get_or_404(Letter, letter_id, label="Letter")
    migration_svc.require_view(g.actor, migration_svc.get_request(letter.request_id))
    return ok({"download_url": signed_download_url(letter.storage_key, f"{letter.reference_no}.pdf",
                                                   "application/pdf")})


@bp.get("/public/letters/<token>")
@api("VERIFY_LETTER", auth=False)
def verify_letter(token):
    letter = Letter.query.filter_by(qr_token=token).first()
    if letter is None:
        raise NotFound("No letter matches this code", error_code="LETTER_NOT_FOUND")
    inst = target(letter)
    return ok({"reference_no": letter.reference_no, "letter_type": letter.letter_type.value,
               "letter_status": letter.letter_status.value, "issued_by": inst.name,
               "issued_on": letter.issued_at.date().isoformat()})


@bp.get("/letter-templates")
@api("LIST_LETTER_TEMPLATES",
     roles=[Role.PLATFORM_ADMIN, Role.UNIVERSITY_ADMIN, Role.UNIVERSITY_USER])
def list_templates():
    q = active(LetterTemplate)
    uid = arg_int("university_id")
    q = q.filter((LetterTemplate.university_id == uid) | LetterTemplate.university_id.is_(None)) if uid else q
    rows = [t.to_dict() for t in q.order_by(LetterTemplate.letter_type)]
    defaults = [{"letter_type": k.value, "subject": v[0], "body": v[1], "built_in": True}
                for k, v in DEFAULT_TEMPLATES.items()]
    return ok({"templates": rows, "built_in_defaults": defaults,
               "placeholders": ["reference_no", "student_name", "enrolment_number", "source_name",
                                "destination_name", "course_name", "entry_semester", "academic_year",
                                "offer_expires_on", "originals_due_on", "additional_papers", "rejection_reason",
                                "migration_certificate_note"]})


@bp.put("/letter-templates")
@api("UPSERT_LETTER_TEMPLATE",
     roles=[Role.PLATFORM_ADMIN, Role.UNIVERSITY_ADMIN])
def upsert_template():
    data = load(TemplateSchema)
    uid = data.get("university_id")
    if uid is None and not g.actor.is_platform_admin:
        raise PermissionDenied("Only the Platform Admin edits default templates")
    if uid is not None:
        g.actor.require(Permission.MANAGE_LETTER_BRANDING, get_or_404(University, uid, label="University"))
    row = LetterTemplate.query.filter_by(letter_type=LetterType(data["letter_type"]), university_id=uid).first()
    if row is None:
        row = LetterTemplate(letter_type=LetterType(data["letter_type"]), university_id=uid)
        db.session.add(row)
    row.subject, row.body = data["subject"], data["body"]
    activate(row)
    db.session.commit()
    return ok(row.to_dict(), "Template saved")
