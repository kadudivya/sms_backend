from flask import Blueprint, g, request
from marshmallow import Schema, fields, validate

from app.api.common import arg_int, form_or_json
from app.core.api import api
from app.core.errors import ValidationError
from app.core.responses import created, ok, paginated
from app.core.validation import get_or_404, load
from app.enums import (CreditDecision, DocumentType, MigrationStatus, MigrationType, NocRefusalReason,
                       Permission, Role)
from app.models import Appeal, MigrationRequest, MigrationWindow
from app.services import migration as svc
from app.services import seats
from app.core.records import active

bp = Blueprint("migration", __name__, url_prefix="/migration-requests")
STAFF = [Role.PLATFORM_ADMIN, Role.UNIVERSITY_ADMIN, Role.UNIVERSITY_USER, Role.COLLEGE_ADMIN, Role.COLLEGE_USER]


class CreateSchema(Schema):
    offering_id = fields.Integer(required=True)
    academic_year_id = fields.Integer()
    entry_semester = fields.Integer(required=True, validate=validate.Range(min=2, max=14))


class DraftUpdateSchema(Schema):
    entry_semester = fields.Integer(validate=validate.Range(min=2, max=14))


class ConversionSchema(Schema):
    certified_percentage = fields.Decimal(places=2, as_string=False, validate=validate.Range(min=0, max=100))


class ReasonSchema(Schema):
    reason = fields.String(validate=validate.Length(max=2000))


class RequiredReasonSchema(Schema):
    reason = fields.String(required=True, validate=validate.Length(min=3, max=2000))
    version = fields.Integer()


class NocSchema(Schema):
    approve = fields.Boolean(required=True)
    refusal_reason = fields.String(validate=validate.OneOf(NocRefusalReason.values()))
    note = fields.String(validate=validate.Length(max=1000))
    version = fields.Integer()


class NoteSchema(Schema):
    note = fields.String(validate=validate.Length(max=2000))
    version = fields.Integer()


class CorrectionSchema(Schema):
    note = fields.String(required=True, validate=validate.Length(min=3, max=2000))
    version = fields.Integer()


class CreditRowSchema(Schema):
    id = fields.Integer(required=True)
    decision = fields.String(required=True, validate=validate.OneOf(CreditDecision.values()))
    reason = fields.String(validate=validate.Length(max=500))
    mapped_subject_code = fields.String(validate=validate.Length(max=30))


class CreditsSchema(Schema):
    decisions = fields.List(fields.Nested(CreditRowSchema), required=True, validate=validate.Length(min=1))


class EntrySchema(Schema):
    is_eligible = fields.Boolean(required=True)
    credits_accepted = fields.Decimal(places=2, as_string=False)
    additional_papers = fields.List(fields.String(), load_default=list)
    remarks = fields.String(validate=validate.Length(max=2000))
    version = fields.Integer()


class AdmissionSchema(Schema):
    fee_paid = fields.Boolean(required=True)
    version = fields.Integer()


class AppealSchema(Schema):
    reason = fields.String(required=True, validate=validate.Length(min=10, max=4000))


class AppealDecisionSchema(Schema):
    uphold = fields.Boolean(required=True)
    note = fields.String(validate=validate.Length(max=2000))


def _req(req_id):
    req = svc.get_request(req_id)
    svc.require_view(g.actor, req)
    return req


# ------------------------------------------------------------- student
@bp.post("")
@api("CREATE_MIGRATION_REQUEST", roles=[Role.STUDENT])
def create_request():
    req = svc.create_request(g.current_user, load(CreateSchema))
    return created(svc.detail(g.actor, req), "Draft created")


@bp.get("/mine")
@api("LIST_MY_MIGRATION_REQUESTS", roles=[Role.STUDENT])
def my_requests():
    return paginated(svc.student_requests(g.current_user).order_by(MigrationRequest.created_at.desc()), svc.summary)


@bp.patch("/<int:req_id>")
@api("UPDATE_MIGRATION_REQUEST", roles=[Role.STUDENT])
def update_draft(req_id):
    req = _req(req_id)
    return ok(svc.detail(g.actor, svc.update_draft(g.current_user, req, load(DraftUpdateSchema))), "Draft updated")


@bp.post("/<int:req_id>/documents")
@api("UPLOAD_REQUEST_DOCUMENT",
     roles=[Role.STUDENT])
def upload_document(req_id):
    req = _req(req_id)
    doc_type = request.form.get("doc_type")
    if doc_type not in DocumentType.values():
        raise ValidationError("doc_type is required", details={"allowed": sorted(t.value for t in svc.STUDENT_UPLOADABLE)})
    doc = svc.upload_document(g.current_user, req, request.files.get("file"), doc_type,
                              arg_int("replace_document_id") or (int(request.form["replace_document_id"])
                                                                 if request.form.get("replace_document_id") else None))
    return created(doc.to_dict(exclude=("storage_key",)), "Document uploaded")


@bp.get("/<int:req_id>/checklist")
@api("GET_DOCUMENT_CHECKLIST", roles=[Role.STUDENT] + STAFF)
def checklist(req_id):
    return ok(svc.checklist(_req(req_id)))


@bp.put("/<int:req_id>/grade-conversion")
@api("SET_GRADE_CONVERSION",
     roles=[Role.STUDENT])
def set_conversion(req_id):
    req = _req(req_id)
    data = load(ConversionSchema, form_or_json())
    conv = svc.set_conversion(g.current_user, req, request.files.get("certificate"), data.get("certified_percentage"))
    return ok(conv.to_dict(), "Conversion saved")


@bp.post("/<int:req_id>/submit")
@api("SUBMIT_MIGRATION_REQUEST", roles=[Role.STUDENT])
def submit(req_id):
    return ok(svc.detail(g.actor, svc.submit(g.current_user, _req(req_id))), "Request submitted")


@bp.post("/<int:req_id>/resubmit")
@api("RESUBMIT_MIGRATION_REQUEST", roles=[Role.STUDENT])
def resubmit(req_id):
    return ok(svc.detail(g.actor, svc.resubmit(g.current_user, _req(req_id))), "Corrections submitted")


@bp.post("/<int:req_id>/withdraw")
@api("WITHDRAW_MIGRATION_REQUEST", roles=[Role.STUDENT])
def withdraw(req_id):
    data = load(ReasonSchema)
    return ok(svc.summary(svc.withdraw(g.current_user, _req(req_id), data.get("reason"))), "Request withdrawn")


@bp.post("/<int:req_id>/accept-offer")
@api("ACCEPT_OFFER", roles=[Role.STUDENT])
def accept_offer(req_id):
    return ok(svc.detail(g.actor, svc.accept_offer(g.current_user, _req(req_id))), "Offer accepted")


@bp.post("/<int:req_id>/decline-offer")
@api("DECLINE_OFFER", roles=[Role.STUDENT])
def decline_offer(req_id):
    return ok(svc.summary(svc.decline_offer(g.current_user, _req(req_id))), "Offer declined")


@bp.post("/<int:req_id>/cancellation-request")
@api("REQUEST_CANCELLATION", roles=[Role.STUDENT])
def request_cancellation(req_id):
    data = load(RequiredReasonSchema)
    return ok(svc.summary(svc.request_cancellation(g.current_user, _req(req_id), data["reason"])),
              "Cancellation requested")


@bp.post("/<int:req_id>/appeal")
@api("FILE_APPEAL", roles=[Role.STUDENT])
def file_appeal(req_id):
    data = load(AppealSchema, form_or_json())
    appeal = svc.file_appeal(g.current_user, _req(req_id), data["reason"], request.files.get("evidence"))
    return created(appeal.to_dict(), "Appeal filed")


# ------------------------------------------------------------ both sides
@bp.get("/<int:req_id>")
@api("GET_MIGRATION_REQUEST",
     roles=[Role.STUDENT] + STAFF)
def get_request(req_id):
    return ok(svc.detail(g.actor, _req(req_id)))


@bp.get("")
@api("LIST_INSTITUTION_MIGRATION_REQUESTS",
     roles=STAFF)
def list_requests():
    q = svc.staff_query(g.actor, request.args.get("direction"))
    if request.args.get("status"):
        statuses = [MigrationStatus(s) for s in request.args["status"].split(",")]
        q = q.filter(MigrationRequest.request_status.in_(statuses))
    for arg, col in (("college_id", None), ("academic_year_id", MigrationRequest.academic_year_id),
                     ("offering_id", MigrationRequest.destination_offering_id)):
        value = arg_int(arg)
        if value and col is not None:
            q = q.filter(col == value)
        elif value:
            q = q.filter((MigrationRequest.source_college_id == value) |
                         (MigrationRequest.destination_college_id == value))
    if request.args.get("migration_type"):
        q = q.filter(MigrationRequest.migration_type == MigrationType(request.args["migration_type"]))
    if request.args.get("q"):
        q = q.filter(MigrationRequest.reference_no.ilike(f"%{request.args['q']}%"))
    return paginated(q.order_by(MigrationRequest.submitted_at.desc().nullslast()), svc.summary)


# ------------------------------------------------------------ source side
@bp.post("/<int:req_id>/noc")
@api("DECIDE_NOC",
     permissions=[Permission.DECIDE_NOC])
def decide_noc(req_id):
    data = load(NocSchema)
    req = svc.decide_noc(g.actor, _req(req_id), data["approve"], data.get("refusal_reason"), data.get("note"),
                         data.get("version"))
    return ok(svc.summary(req), "NOC decided")


@bp.post("/<int:req_id>/transfer-certificate")
@api("UPLOAD_TRANSFER_CERTIFICATE",
     permissions=[Permission.ISSUE_RELEASE])
def upload_tc(req_id):
    req = svc.upload_transfer_certificate(g.actor, _req(req_id), request.files.get("file"))
    return ok(svc.summary(req), "Transfer certificate uploaded")


@bp.post("/<int:req_id>/university-release")
@api("RELEASE_FROM_UNIVERSITY",
     permissions=[Permission.ISSUE_RELEASE])
def university_release(req_id):
    req = svc.university_release(g.actor, _req(req_id), request.files.get("file"))
    return ok(svc.summary(req), "Migration certificate uploaded")


# ----------------------------------------------------- destination college
@bp.post("/<int:req_id>/start-review")
@api("START_REVIEW", permissions=[Permission.REVIEW_REQUEST])
def start_review(req_id):
    return ok(svc.detail(g.actor, svc.start_review(g.actor, _req(req_id))), "Review started")


@bp.post("/<int:req_id>/return-for-correction")
@api("RETURN_FOR_CORRECTION",
     permissions=[Permission.REVIEW_REQUEST])
def return_for_correction(req_id):
    data = load(CorrectionSchema)
    return ok(svc.summary(svc.return_for_correction(g.actor, _req(req_id), data["note"], data.get("version"))),
              "Returned for correction")


@bp.put("/<int:req_id>/credits")
@api("ASSESS_CREDITS", permissions=[Permission.ASSESS_CREDITS])
def assess_credits(req_id):
    req = _req(req_id)
    svc.assess_credits(g.actor, req, load(CreditsSchema)["decisions"])
    return ok({"credit_summary": svc.credit_summary(req)}, "Credits saved")


@bp.post("/<int:req_id>/eligibility-check")
@api("RUN_ELIGIBILITY_CHECK", permissions=[Permission.REVIEW_REQUEST])
def eligibility_check(req_id):
    req = svc.rerun_eligibility(g.actor, _req(req_id))
    return ok({"eligibility_result": req.eligibility_result.value, "notes": req.eligibility_notes})


@bp.post("/<int:req_id>/recommend")
@api("RECOMMEND_MIGRATION_REQUEST", permissions=[Permission.RECOMMEND_REQUEST])
def recommend(req_id):
    data = load(NoteSchema)
    return ok(svc.summary(svc.recommend(g.actor, _req(req_id), data.get("note"), data.get("version"))), "Recommended")


@bp.post("/<int:req_id>/reject")
@api("REJECT_MIGRATION_REQUEST",
     roles=STAFF)
def reject(req_id):
    data = load(RequiredReasonSchema)
    return ok(svc.summary(svc.reject(g.actor, _req(req_id), data["reason"], data.get("version"))), "Rejected")


@bp.post("/<int:req_id>/approve-cancellation")
@api("APPROVE_CANCELLATION", permissions=[Permission.APPROVE_CANCELLATION])
def approve_cancellation(req_id):
    data = load(ReasonSchema)
    return ok(svc.summary(svc.approve_cancellation(g.actor, _req(req_id), data.get("reason"))), "Cancelled")


@bp.post("/<int:req_id>/confirm-admission")
@api("CONFIRM_ADMISSION", permissions=[Permission.CONFIRM_ADMISSION])
def confirm_admission(req_id):
    data = load(AdmissionSchema)
    return ok(svc.summary(svc.confirm_admission(g.actor, _req(req_id), data["fee_paid"], data.get("version"))),
              "Admission confirmed")


@bp.post("/<int:req_id>/verify-originals")
@api("VERIFY_ORIGINALS",
     permissions=[Permission.VERIFY_ORIGINALS])
def verify_originals(req_id):
    return ok(svc.summary(svc.verify_originals(g.actor, _req(req_id))), "Originals verified")


@bp.post("/<int:req_id>/cancel-non-reporting")
@api("CANCEL_FOR_NON_REPORTING",
     permissions=[Permission.VERIFY_ORIGINALS])
def cancel_non_reporting(req_id):
    data = load(ReasonSchema)
    return ok(svc.summary(svc.cancel_for_non_reporting(g.actor, _req(req_id), data.get("reason"))), "Cancelled")


# --------------------------------------------------- destination university
@bp.post("/<int:req_id>/approve-entry")
@api("APPROVE_ENTRY",
     permissions=[Permission.APPROVE_ENTRY])
def approve_entry(req_id):
    data = load(EntrySchema)
    return ok(svc.detail(g.actor, svc.approve_entry(g.actor, _req(req_id), data, data.get("version"))),
              "Entry decision recorded")


@bp.post("/<int:req_id>/override-eligibility")
@api("OVERRIDE_ELIGIBILITY",
     permissions=[Permission.OVERRIDE_ELIGIBILITY])
def override(req_id):
    data = load(RequiredReasonSchema)
    req = svc.override_eligibility(g.actor, _req(req_id), data["reason"])
    return ok({"eligibility_result": req.eligibility_result.value, "notes": req.eligibility_notes}, "Overridden")


@bp.post("/<int:req_id>/cancel-admission")
@api("CANCEL_ADMISSION", permissions=[Permission.CANCEL_ADMISSION])
def cancel_admission(req_id):
    data = load(RequiredReasonSchema)
    return ok(svc.summary(svc.cancel_admission(g.actor, _req(req_id), data["reason"])), "Admission cancelled")


@bp.post("/appeals/<int:appeal_id>/decision")
@api("DECIDE_APPEAL", permissions=[Permission.DECIDE_APPEAL])
def decide_appeal(appeal_id):
    appeal = get_or_404(Appeal, appeal_id, label="Appeal")
    data = load(AppealDecisionSchema)
    appeal = svc.decide_appeal(g.actor, appeal, data["uphold"], data.get("note"))
    return ok(appeal.to_dict(), "Appeal decided")


@bp.get("/appeals")
@api("LIST_APPEALS", permissions=[Permission.DECIDE_APPEAL])
def list_appeals():
    q = active(Appeal).join(MigrationRequest, MigrationRequest.id == Appeal.request_id)
    if not g.actor.is_platform_admin:
        ids = list(g.actor.university_ids()) or [-1]
        q = q.filter((MigrationRequest.destination_university_id.in_(ids)) |
                     (MigrationRequest.source_university_id.in_(ids)))
    if request.args.get("decision", "PENDING") != "ALL":
        from app.enums import AppealDecision
        q = q.filter(Appeal.decision == AppealDecision(request.args.get("decision", "PENDING")))
    return paginated(q.order_by(Appeal.due_at), lambda a: {**a.to_dict(), "request": svc.summary(
        MigrationRequest.query.get(a.request_id))})


@bp.post("/merit-rounds/<int:window_id>")
@api("RUN_MERIT_ROUND",
     permissions=[Permission.APPROVE_ENTRY])
def run_merit_round(window_id):
    from app.core.errors import BusinessRuleViolation
    from app.core.utils import today_ist
    from app.extensions import db
    window = get_or_404(MigrationWindow, window_id, label="Window")
    g.actor.require(Permission.APPROVE_ENTRY, window.course.university)
    if window.closes_on >= today_ist():
        raise BusinessRuleViolation("The window has not closed yet", error_code="WINDOW_OPEN")
    result = seats.run_merit_round(window, svc.transition)
    db.session.commit()
    return ok(result, "Merit round complete")
