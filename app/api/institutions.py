from flask import Blueprint, g, request
from marshmallow import Schema, fields, validate
from sqlalchemy import func

from app.api.common import arg_int
from app.core.api import api
from app.core.errors import PermissionDenied
from app.core.responses import ok, paginated
from app.core.validation import get_or_404, load
from app.core.errors import ValidationError
from app.core.institutions import KIND_URL, get_institution
from app.enums import OnboardingStatus, Permission, Role
from app.models import College, District, State, University
from app.services import institutions as svc
from app.services.onboarding import institution_card

bp = Blueprint("institutions", __name__, url_prefix="")


class ProfileSchema(Schema):
    address_line = fields.String(validate=validate.Length(max=500))
    city = fields.String(validate=validate.Length(max=150))
    pincode = fields.String()
    website = fields.String(validate=validate.Length(max=300))
    phone = fields.String(validate=validate.Length(max=30))
    state_id = fields.Integer()
    district_id = fields.Integer()


class SuspendSchema(Schema):
    reason = fields.String(required=True, validate=validate.Length(min=3, max=500))


class AffiliationSchema(Schema):
    parent_university_id = fields.Integer(required=True)


class ReservationPolicySchema(Schema):
    release_unfilled_reserved_to_open = fields.Boolean(required=True)


@bp.get("/states")
@api("LIST_STATES", auth=False)
def list_states():
    return ok([s.to_dict() for s in State.query.order_by(State.name)])


@bp.get("/states/<int:state_id>/districts")
@api("LIST_DISTRICTS", auth=False)
def list_districts(state_id):
    return ok([d.to_dict() for d in District.query.filter_by(state_id=state_id).order_by(District.name)])


@bp.get("/pincodes/<pin>")
@api("LOOKUP_PINCODE", auth=False)
def lookup_pincode(pin):
    return ok(svc.pincode_lookup(pin))


@bp.get("/institutions")
@api("LIST_INSTITUTIONS",
     roles=[Role.PLATFORM_ADMIN, Role.UNIVERSITY_ADMIN, Role.UNIVERSITY_USER, Role.COLLEGE_ADMIN,
            Role.COLLEGE_USER])
def list_institutions():
    """?kind=university|college (default college). Colleges also filter by institution_type
    (COLLEGE / STANDALONE / PM_VIDYALAXMI / R&D) and university_id."""
    kind = (request.args.get("kind") or "college").lower()
    if kind not in KIND_URL.values():
        raise ValidationError("kind must be university or college")
    model = University if kind == "university" else College
    q = model.query
    if request.args.get("q"):
        term = f"%{request.args['q'].strip().lower()}%"
        q = q.filter(db_or(func.lower(model.name).like(term), func.lower(model.aishe_code).like(term)))
    if request.args.get("onboarding_status"):
        q = q.filter(model.onboarding_status == OnboardingStatus(request.args["onboarding_status"]))
    for col in ("state_id", "district_id"):
        value = arg_int(col)
        if value:
            q = q.filter(getattr(model, col) == value)
    if model is College:
        if request.args.get("institution_type"):
            q = q.filter(College.institution_type == request.args["institution_type"].upper())
        if arg_int("university_id"):
            q = q.filter(College.university_id == arg_int("university_id"))
    if request.args.get("unverified_pincode") == "true":
        q = q.filter(model.pincode_verified.is_(False))
    if not g.actor.is_platform_admin:
        universities, colleges = list(g.actor.university_ids()), list(g.actor.college_ids())
        if model is University:
            q = q.filter(University.id.in_(universities or [-1]))
        else:
            q = q.filter(db_or(College.id.in_(colleges or [-1]), College.university_id.in_(universities or [-1])))
    return paginated(q.order_by(model.name), lambda i: {
        **i.to_dict(exclude=("letter_header", "letter_footer")),
        "kind": kind.upper(),
        "state_name": i.state.name if i.state else None,
        "district_name": i.district.name if i.district else None,
        "parent_name": i.university.name if model is College and i.university else None})


def db_or(*args):
    from app.extensions import db
    return db.or_(*args)


# <kind> is "university" or "college": the table the id belongs to
INSTITUTION_URL = "/institutions/<any(university,college):kind>/<int:institution_id>"


@bp.get(INSTITUTION_URL)
@api("GET_INSTITUTION", allow_pending=True)
def get_institution_card(kind, institution_id):
    inst = get_institution(kind, institution_id)
    if not (g.actor.can_view_institution(inst) or g.actor.is_student):
        raise PermissionDenied("This institution is outside your scope")
    return ok(institution_card(inst))


@bp.patch(INSTITUTION_URL)
@api("UPDATE_INSTITUTION_PROFILE",
     roles=[Role.PLATFORM_ADMIN, Role.UNIVERSITY_ADMIN, Role.COLLEGE_ADMIN])
def update_institution(kind, institution_id):
    inst = get_institution(kind, institution_id)
    svc.update_profile(g.actor, inst, load(ProfileSchema, partial=True))
    return ok(institution_card(inst), "Profile updated")


@bp.post(INSTITUTION_URL + "/suspend")
@api("SUSPEND_INSTITUTION",
     roles=[Role.PLATFORM_ADMIN, Role.UNIVERSITY_ADMIN, Role.UNIVERSITY_USER])
def suspend(kind, institution_id):
    inst = get_institution(kind, institution_id)
    data = load(SuspendSchema)
    svc.set_suspended(g.actor, inst, True, data["reason"])
    return ok(institution_card(inst), "Institution suspended")


@bp.post(INSTITUTION_URL + "/reactivate")
@api("REACTIVATE_INSTITUTION",
     roles=[Role.PLATFORM_ADMIN, Role.UNIVERSITY_ADMIN, Role.UNIVERSITY_USER])
def reactivate(kind, institution_id):
    inst = get_institution(kind, institution_id)
    svc.set_suspended(g.actor, inst, False)
    return ok(institution_card(inst), "Institution reactivated")


@bp.put("/colleges/<int:college_id>/affiliation")
@api("UPDATE_COLLEGE_AFFILIATION",
     permissions=[Permission.MANAGE_COLLEGE_STATUS])
def update_affiliation(college_id):
    college = get_or_404(College, college_id, label="College")
    data = load(AffiliationSchema)
    parent = get_or_404(University, data["parent_university_id"], label="University")
    svc.update_affiliation(g.actor, college, parent)
    return ok(institution_card(college), "Affiliation updated")


@bp.put(INSTITUTION_URL + "/branding")
@api("UPDATE_LETTER_BRANDING", permissions=[Permission.MANAGE_LETTER_BRANDING])
def update_branding(kind, institution_id):
    inst = get_institution(kind, institution_id)
    svc.update_branding(g.actor, inst, request.form.get("letter_header"), request.form.get("letter_footer"),
                        request.files.get("seal"))
    return ok(institution_card(inst), "Branding updated")


@bp.put("/universities/<int:university_id>/reservation-policy")
@api("UPDATE_RESERVATION_POLICY",
     permissions=[Permission.MANAGE_ACADEMIC_RULES])
def reservation_policy(university_id):
    inst = get_or_404(University, university_id, label="University")
    data = load(ReservationPolicySchema)
    svc.update_reservation_policy(g.actor, inst, data["release_unfilled_reserved_to_open"])
    return ok(institution_card(inst), "Policy updated")
