from flask import Blueprint, current_app, g, request
from marshmallow import Schema, fields, validate

from app.api.common import form_or_json
from app.core.api import api
from app.core.errors import ValidationError
from app.core.responses import created, ok, paginated
from app.core.validation import get_or_404, load
from app.core.institutions import REGISTRATION_TYPES, target
from app.enums import ApprovalStatus, InstitutionKind, Permission, Role
from app.extensions import limiter
from app.models import CollegeOnboardingRequest, RegistrationRequest, University
from app.services import onboarding as svc
from app.services.documents import download_link, get_document
from app.core.records import active

bp = Blueprint("onboarding", __name__, url_prefix="/onboarding")


class InstitutionDetailsSchema(Schema):
    name = fields.String(required=True, validate=validate.Length(min=3, max=400))
    state_id = fields.Integer(required=True)
    district_id = fields.Integer(required=True)
    address_line = fields.String(required=True, validate=validate.Length(max=500))
    city = fields.String(validate=validate.Length(max=150))
    pincode = fields.String(required=True)
    website = fields.String(validate=validate.Length(max=300))
    phone = fields.String(validate=validate.Length(max=30))
    parent_university_aishe_code = fields.String()


class CreateInstitutionSchema(Schema):
    aishe_code = fields.String(required=True)
    admin_email = fields.Email(required=True)
    admin_name = fields.String(required=True, validate=validate.Length(min=2, max=200))
    institution = fields.Nested(InstitutionDetailsSchema)


class SignupLinkSchema(Schema):
    aishe_code = fields.String(required=True)
    email = fields.Email(required=True)
    note = fields.String(validate=validate.Length(max=500))


class AcceptInvitationSchema(Schema):
    full_name = fields.String(validate=validate.Length(min=2, max=200))
    password = fields.String(required=True)
    confirm_password = fields.String()
    mobile = fields.String(validate=validate.Regexp(r"^(\+91)?[6-9]\d{9}$"))
    designation = fields.String(validate=validate.Length(max=150))
    official_email = fields.Email()
    institution = fields.Nested(InstitutionDetailsSchema)


class RegistrationSchema(Schema):
    institution_type = fields.String(required=True, validate=validate.OneOf(REGISTRATION_TYPES))
    aishe_code = fields.String(required=True)
    full_name = fields.String(required=True, validate=validate.Length(min=2, max=200))
    email = fields.Email(required=True)
    password = fields.String(required=True)
    confirm_password = fields.String(required=True)
    mobile = fields.String(required=True, validate=validate.Regexp(r"^(\+91)?[6-9]\d{9}$"))
    designation = fields.String(required=True, validate=validate.Length(max=150))
    official_email = fields.Email()
    institution = fields.Nested(InstitutionDetailsSchema)


class DecisionSchema(Schema):
    note = fields.String(validate=validate.Length(max=1000))


class RejectSchema(Schema):
    reason = fields.String(required=True, validate=validate.Length(min=3, max=1000))


class OnboardingDecisionSchema(Schema):
    approve = fields.Boolean(required=True)
    note = fields.String(validate=validate.Length(max=500))


class PlatformAdminInviteSchema(Schema):
    email = fields.Email(required=True)
    full_name = fields.String(required=True)


def _check_confirm(data):
    if "confirm_password" in data and data["confirm_password"] != data["password"]:
        raise ValidationError("Password and confirmation do not match")


# ------------------------------------------------------------- public
@bp.get("/lookup")
@limiter.limit("60 per minute")
@api("LOOKUP_INSTITUTION_BY_AISHE", auth=False)
def lookup():
    code = request.args.get("aishe_code", "")
    itype = request.args.get("institution_type")
    inst = svc.lookup_institution(code, itype)
    if inst is None:
        return ok({"found": False, "aishe_code": code.upper()}, "Not found: enter the institution details")
    card = svc.institution_card(inst)
    return ok({"found": True, "institution": {k: card[k] for k in (
        "id", "kind", "registration_type", "aishe_code", "name", "institution_type", "state_name",
        "district_name", "address_line",
        "pincode", "parent_name", "parent_aishe_code", "onboarding_status", "has_active_admin")}})


@bp.get("/invitations/<token>")
@api("GET_INVITATION", auth=False)
def get_invitation(token):
    inv = svc.get_invitation_by_token(token)
    return ok(svc.describe_invitation(inv))


@bp.post("/invitations/<token>/accept")
@limiter.limit("20 per hour")
@api("ACCEPT_INVITATION", auth=False)
def accept_invitation(token):
    data = load(AcceptInvitationSchema, form_or_json())
    _check_confirm(data)
    return created(svc.accept_invitation(token, data, request.files.get("authorization_letter")),
                   "Invitation accepted")


@bp.post("/registrations")
@limiter.limit(lambda: current_app.config["RATELIMIT_REGISTRATION"])
@api("SUBMIT_INSTITUTION_REGISTRATION", auth=False)
def submit_registration():
    data = load(RegistrationSchema, form_or_json())
    _check_confirm(data)
    return created(svc.submit_registration_request(data, request.files.get("authorization_letter")),
                   "Registration submitted. Verify your email; an approver will review it.")


# ------------------------------------------------------ platform admin
@bp.post("/universities")
@api("CREATE_UNIVERSITY",
     roles=[Role.PLATFORM_ADMIN])
def create_university():
    data = load(CreateInstitutionSchema)
    inst, inv, debug = svc.create_institution_and_invite_admin(
        g.actor, "UNIVERSITY", data["aishe_code"], data["admin_email"], data["admin_name"],
        data.get("institution"))
    return created({"institution": svc.institution_card(inst), "invitation_id": inv.id, **debug},
                   "University ready; admin invited")


@bp.post("/standalone-institutions")
@api("CREATE_STANDALONE_INSTITUTION",
     roles=[Role.PLATFORM_ADMIN])
def create_standalone():
    data = load(CreateInstitutionSchema)
    inst, inv, debug = svc.create_institution_and_invite_admin(
        g.actor, "STANDALONE", data["aishe_code"], data["admin_email"], data["admin_name"],
        data.get("institution"))
    return created({"institution": svc.institution_card(inst), "invitation_id": inv.id, **debug},
                   "Institution ready; admin invited")


@bp.post("/universities/signup-links")
@api("SEND_UNIVERSITY_SIGNUP_LINK",
     roles=[Role.PLATFORM_ADMIN])
def university_signup_link():
    data = load(SignupLinkSchema)
    inv, debug = svc.send_signup_link(g.actor, "UNIVERSITY", data["aishe_code"], data["email"],
                                      note=data.get("note"))
    return created({"invitation_id": inv.id, "expires_at": inv.expires_at.isoformat(), **debug},
                   "Signup link sent")


@bp.post("/standalone-institutions/signup-links")
@api("SEND_STANDALONE_SIGNUP_LINK", roles=[Role.PLATFORM_ADMIN])
def standalone_signup_link():
    data = load(SignupLinkSchema)
    inv, debug = svc.send_signup_link(g.actor, "STANDALONE", data["aishe_code"], data["email"],
                                      note=data.get("note"))
    return created({"invitation_id": inv.id, "expires_at": inv.expires_at.isoformat(), **debug},
                   "Signup link sent")


@bp.post("/platform-admins")
@api("INVITE_PLATFORM_ADMIN", roles=[Role.PLATFORM_ADMIN])
def invite_platform_admin():
    data = load(PlatformAdminInviteSchema)
    inv, debug = svc.invite_platform_admin(g.actor, data["email"], data["full_name"])
    return created({"invitation_id": inv.id, **debug}, "Platform Admin invited")


# ---------------------------------------------------- university admin
@bp.post("/universities/<int:university_id>/colleges")
@api("CREATE_COLLEGE",
     permissions=[Permission.CREATE_COLLEGE])
def create_college(university_id):
    university = get_or_404(University, university_id, label="University")
    data = load(CreateInstitutionSchema)
    inst, inv, debug = svc.create_institution_and_invite_admin(
        g.actor, "COLLEGE", data["aishe_code"], data["admin_email"], data["admin_name"],
        data.get("institution"), parent=university)
    return created({"institution": svc.institution_card(inst), "invitation_id": inv.id, **debug},
                   "College ready; admin invited")


@bp.post("/universities/<int:university_id>/colleges/signup-links")
@api("SEND_COLLEGE_SIGNUP_LINK",
     permissions=[Permission.CREATE_COLLEGE])
def college_signup_link(university_id):
    university = get_or_404(University, university_id, label="University")
    data = load(SignupLinkSchema)
    inv, debug = svc.send_signup_link(g.actor, "COLLEGE", data["aishe_code"], data["email"],
                                      parent=university, note=data.get("note"))
    return created({"invitation_id": inv.id, "expires_at": inv.expires_at.isoformat(), **debug},
                   "Signup link sent")


# ---------------------------------------------------------- reviewing
def _registration_view(reg):
    letter = get_document(reg.authorization_letter_id) if reg.authorization_letter_id else None
    return {
        **reg.to_dict(),
        "institution": svc.institution_card(target(reg)) if target(reg) else None,
        "requester": {"id": reg.requester.id, "full_name": reg.requester.full_name,
                      "email": reg.requester.email, "mobile": reg.requester.mobile,
                      "email_verified": reg.requester.email_verified_at is not None},
        "authorization_letter": ({"id": letter.id, "filename": letter.original_filename,
                                  "download_url": download_link(g.actor, letter)} if letter else None),
    }


@bp.get("/registrations")
@api("LIST_REGISTRATION_REQUESTS",
     roles=[Role.PLATFORM_ADMIN, Role.UNIVERSITY_ADMIN, Role.UNIVERSITY_USER])
def list_registrations():
    q = svc.visible_registrations(g.actor)
    status = request.args.get("approval_status", ApprovalStatus.PENDING.value)
    if status != "ALL":
        q = q.filter(RegistrationRequest.approval_status == ApprovalStatus(status))
    if request.args.get("institution_kind"):
        q = q.filter(RegistrationRequest.institution_kind == InstitutionKind(request.args["institution_kind"]))
    if request.args.get("second_claim") == "true":
        q = q.filter(RegistrationRequest.is_second_claim.is_(True))
    def view(r):
        inst = target(r)
        return {**r.to_dict(), "institution_name": inst.name if inst else None,
                "aishe_code": inst.aishe_code if inst else None,
                "requester_name": r.requester.full_name, "requester_email": r.requester.email}
    return paginated(q.order_by(RegistrationRequest.created_at), view)


@bp.get("/registrations/<int:reg_id>")
@api("GET_REGISTRATION_REQUEST",
     roles=[Role.PLATFORM_ADMIN, Role.UNIVERSITY_ADMIN, Role.UNIVERSITY_USER])
def get_registration(reg_id):
    reg = get_or_404(RegistrationRequest, reg_id, label="Registration request")
    svc._require_reviewer(g.actor, reg)
    return ok(_registration_view(reg))


@bp.post("/registrations/<int:reg_id>/approve")
@api("APPROVE_REGISTRATION",
     roles=[Role.PLATFORM_ADMIN, Role.UNIVERSITY_ADMIN, Role.UNIVERSITY_USER])
def approve_registration(reg_id):
    reg = get_or_404(RegistrationRequest, reg_id, label="Registration request")
    data = load(DecisionSchema)
    svc.approve_registration(g.actor, reg, data.get("note"))
    return ok(reg.to_dict(), "Registration approved")


@bp.post("/registrations/<int:reg_id>/reject")
@api("REJECT_REGISTRATION",
     roles=[Role.PLATFORM_ADMIN, Role.UNIVERSITY_ADMIN, Role.UNIVERSITY_USER])
def reject_registration(reg_id):
    reg = get_or_404(RegistrationRequest, reg_id, label="Registration request")
    data = load(RejectSchema)
    svc.reject_registration(g.actor, reg, data["reason"])
    return ok(reg.to_dict(), "Registration rejected")


# ------------------------------------------- student-led onboarding queue
@bp.get("/college-requests")
@api("LIST_COLLEGE_ONBOARDING_REQUESTS",
     roles=[Role.PLATFORM_ADMIN])
def list_college_requests():
    q = active(CollegeOnboardingRequest)
    status = request.args.get("approval_status", "PENDING")
    if status != "ALL":
        q = q.filter(CollegeOnboardingRequest.approval_status == ApprovalStatus(status))
    return paginated(q.order_by(CollegeOnboardingRequest.created_at), lambda r: {
        **r.to_dict(), "college_name": r.college.name, "aishe_code": r.college.aishe_code,
        "institution_type": r.college.institution_type})


@bp.post("/college-requests/<int:row_id>/decision")
@api("DECIDE_COLLEGE_ONBOARDING_REQUEST",
     roles=[Role.PLATFORM_ADMIN])
def decide_college_request(row_id):
    row = get_or_404(CollegeOnboardingRequest, row_id, label="Onboarding request")
    data = load(OnboardingDecisionSchema)
    row, debug = svc.decide_college_onboarding(g.actor, row, data["approve"], data.get("note"))
    return ok({**row.to_dict(), **debug}, "Decision recorded")
