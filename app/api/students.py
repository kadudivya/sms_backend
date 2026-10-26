from flask import Blueprint, g, request
from marshmallow import Schema, fields, validate

from app.api.common import arg_int, form_or_json
from app.core.api import api
from app.core.responses import created, ok, paginated
from app.core.validation import get_or_404, load
from app.enums import (CourseLevel, Gender, GradeSystem, Permission, ReservationCategory,
                       Role)
from app.extensions import limiter
from app.models import AcademicRecord, StudentProfile
from app.services import onboarding as onboarding_svc
from app.services import students as svc
from app.core.records import active, student_name

bp = Blueprint("students", __name__, url_prefix="")
MOBILE = validate.Regexp(r"^(\+91)?[6-9]\d{9}$", error="Enter a valid Indian mobile number")


class RegisterSchema(Schema):
    first_name = fields.String(required=True, validate=validate.Length(min=1, max=100))
    last_name = fields.String(validate=validate.Length(max=100))
    email = fields.Email(required=True)
    mobile = fields.String(required=True, validate=MOBILE)
    password = fields.String(required=True)
    confirm_password = fields.String(required=True)
    date_of_birth = fields.Date()
    gender = fields.String(validate=validate.OneOf(Gender.values()))
    category = fields.String(validate=validate.OneOf(ReservationCategory.values()))
    abc_account_id = fields.String(validate=validate.Regexp(r"^\d{12}$", error="ABC account ID must be 12 digits"))
    consent = fields.Boolean(required=True)
    whatsapp_opt_in = fields.Boolean(load_default=False)
    language = fields.String(validate=validate.OneOf(["en", "hi"]))


class ProfileSchema(Schema):
    first_name = fields.String(validate=validate.Length(min=1, max=100))
    last_name = fields.String(validate=validate.Length(max=100))
    date_of_birth = fields.Date()
    gender = fields.String(validate=validate.OneOf(Gender.values()))
    category = fields.String(validate=validate.OneOf(ReservationCategory.values()))
    abc_account_id = fields.String(validate=validate.Regexp(r"^\d{12}$"))
    guardian_name = fields.String(validate=validate.Length(max=200))
    guardian_email = fields.Email()
    guardian_mobile = fields.String(validate=MOBILE)
    address_line = fields.String(validate=validate.Length(max=500))
    state_id = fields.Integer()
    district_id = fields.Integer()
    pincode = fields.String(validate=validate.Regexp(r"^[1-9]\d{5}$"))


class EnrolmentSchema(Schema):
    current_college_id = fields.Integer(required=True)
    enrolment_number = fields.String(required=True, validate=validate.Length(min=1, max=50))
    current_course_name = fields.String(required=True, validate=validate.Length(min=2, max=300))
    current_course_level = fields.String(validate=validate.OneOf(CourseLevel.values()))
    current_discipline = fields.String(validate=validate.Length(max=150))
    current_semester = fields.Integer(required=True, validate=validate.Range(min=1, max=14))
    batch_year = fields.Integer(required=True, validate=validate.Range(min=1990, max=2100))


class EnrolmentDecisionSchema(Schema):
    approve = fields.Boolean(required=True)
    reason = fields.String(validate=validate.Length(max=500))


class ParentConsentSchema(Schema):
    otp = fields.String(required=True)
    declaration = fields.Boolean(required=True)


class SubjectSchema(Schema):
    subject_code = fields.String(required=True, validate=validate.Length(min=1, max=30))
    subject_name = fields.String(required=True, validate=validate.Length(min=1, max=300))
    credits = fields.Decimal(required=True, places=2, as_string=False)
    grade = fields.String(validate=validate.Length(max=10))
    marks_obtained = fields.Decimal(places=2, as_string=False)
    max_marks = fields.Decimal(places=2, as_string=False)
    passed = fields.Boolean(load_default=True)


class RecordSchema(Schema):
    semester = fields.Integer(required=True, validate=validate.Range(min=1, max=14))
    grade_system = fields.String(required=True, validate=validate.OneOf(GradeSystem.values()))
    sgpa = fields.Decimal(places=2, as_string=False, allow_none=True)
    cgpa = fields.Decimal(places=2, as_string=False, allow_none=True)
    percentage = fields.Decimal(places=2, as_string=False, allow_none=True,
                                validate=validate.Range(min=0, max=100))
    credits_earned = fields.Decimal(places=2, as_string=False, allow_none=True)
    active_backlogs = fields.Integer(load_default=0, validate=validate.Range(min=0))
    subjects = fields.List(fields.Nested(SubjectSchema), load_default=list)


class CollegeOnboardingSchema(Schema):
    college_id = fields.Integer(required=True)
    contact_email = fields.Email(required=True)
    contact_name = fields.String(validate=validate.Length(max=200))
    note = fields.String(validate=validate.Length(max=1000))


@bp.post("/students")
@limiter.limit("10 per hour")
@api("CREATE_STUDENT", auth=False)
def create_student():
    data = load(RegisterSchema)
    if data["password"] != data["confirm_password"]:
        from app.core.errors import ValidationError
        raise ValidationError("Password and confirmation do not match")
    return created(svc.register_student(data), "Registered. Verify your email to continue.")


@bp.get("/students/me")
@api("GET_STUDENT_PROFILE", roles=[Role.STUDENT])
def get_profile():
    return ok(svc.profile_view(svc.get_profile(g.current_user)))


@bp.patch("/students/me")
@api("UPDATE_STUDENT_PROFILE", roles=[Role.STUDENT])
def update_profile():
    profile = svc.update_profile(g.current_user, load(ProfileSchema, partial=True))
    return ok(svc.profile_view(profile), "Profile saved")


@bp.post("/students/me/enrolment")
@api("SUBMIT_ENROLMENT_DETAILS", roles=[Role.STUDENT])
def submit_enrolment():
    data = load(EnrolmentSchema, form_or_json())
    profile = svc.submit_enrolment(g.current_user, data, request.files.get("proof"))
    return ok(svc.profile_view(profile), "Sent to your college for verification")


@bp.post("/students/me/parental-consent/request")
@api("REQUEST_PARENTAL_CONSENT", roles=[Role.STUDENT])
def request_parent_consent():
    return ok(svc.request_parental_consent(g.current_user) or None, "Code sent to your parent or guardian")


@bp.post("/students/me/parental-consent/confirm")
@api("CONFIRM_PARENTAL_CONSENT", roles=[Role.STUDENT])
def confirm_parent_consent():
    data = load(ParentConsentSchema)
    profile = svc.confirm_parental_consent(g.current_user, data["otp"], data["declaration"])
    return ok(svc.profile_view(profile), "Parental consent recorded")


@bp.post("/students/me/college-onboarding")
@api("REQUEST_COLLEGE_ONBOARDING", roles=[Role.STUDENT])
def request_college_onboarding():
    data = load(CollegeOnboardingSchema)
    row = onboarding_svc.request_college_onboarding(g.current_user, data["college_id"], data["contact_email"],
                                                    data.get("contact_name"), data.get("note"))
    return created(row.to_dict(), "Request sent to the platform team")


@bp.get("/students/me/academic-records")
@api("LIST_ACADEMIC_RECORDS", roles=[Role.STUDENT])
def list_records():
    profile = svc.get_profile(g.current_user)
    q = active(AcademicRecord).filter_by(student_id=profile.id, is_current=True).order_by(AcademicRecord.semester)
    return ok([svc.record_view(r) for r in q])


@bp.post("/students/me/academic-records")
@api("CREATE_ACADEMIC_RECORD", roles=[Role.STUDENT])
def create_record():
    data = load(RecordSchema, form_or_json())
    rec = svc.save_academic_record(g.current_user, data, request.files.get("marksheet"))
    return created(svc.record_view(rec), "Record saved")


@bp.put("/students/me/academic-records/<int:record_id>")
@api("UPDATE_ACADEMIC_RECORD", roles=[Role.STUDENT])
def update_record(record_id):
    rec = get_or_404(AcademicRecord, record_id, active_only=True, label="Record")
    data = load(RecordSchema, form_or_json())
    return ok(svc.record_view(svc.save_academic_record(g.current_user, data, request.files.get("marksheet"), rec)),
              "Record saved")


@bp.post("/students/me/academic-records/<int:record_id>/deactivate")
@api("DEACTIVATE_ACADEMIC_RECORD", roles=[Role.STUDENT])
def deactivate_record(record_id):
    rec = get_or_404(AcademicRecord, record_id, active_only=True, label="Record")
    svc.deactivate_record(g.current_user, rec)
    return ok(None, "Record removed")


# ------------------------------------------------------------ staff side
def _queue_view(p):
    return {**svc.profile_view(p), "student_name": student_name(p)}


@bp.get("/enrolment-verifications")
@api("LIST_ENROLMENT_VERIFICATIONS",
     permissions=[Permission.VERIFY_ENROLMENT])
def enrolment_queue():
    q = svc.enrolment_queue(g.actor)
    if arg_int("college_id"):
        q = q.filter(StudentProfile.current_college_id == arg_int("college_id"))
    return paginated(q.order_by(StudentProfile.updated_at), _queue_view)


@bp.post("/enrolment-verifications/<int:student_id>/decision")
@api("DECIDE_ENROLMENT",
     permissions=[Permission.VERIFY_ENROLMENT])
def decide_enrolment(student_id):
    profile = get_or_404(StudentProfile, student_id, label="Student")
    data = load(EnrolmentDecisionSchema)
    svc.decide_enrolment(g.actor, profile, data["approve"], data.get("reason"))
    return ok(_queue_view(profile), "Decision recorded")


@bp.get("/students/<int:student_id>")
@api("GET_STUDENT_FOR_STAFF",
     roles=[Role.PLATFORM_ADMIN, Role.UNIVERSITY_ADMIN, Role.UNIVERSITY_USER, Role.COLLEGE_ADMIN,
            Role.COLLEGE_USER])
def get_student(student_id):
    from app.core.errors import PermissionDenied
    from app.models import MigrationRequest
    profile = get_or_404(StudentProfile, student_id, label="Student")
    inst_ids = {profile.current_college_id}
    for r in MigrationRequest.query.filter_by(student_id=profile.id):
        inst_ids.update({r.source_college_id, r.destination_college_id})
    from app.models import College
    from app.extensions import db
    if not any(i and g.actor.can_view_institution(db.session.get(College, i)) for i in inst_ids):
        raise PermissionDenied("This student is not linked to your institution")
    recs = active(AcademicRecord).filter_by(student_id=profile.id, is_current=True).order_by(AcademicRecord.semester)
    return ok({**_queue_view(profile), "academic_records": [svc.record_view(r) for r in recs]})
