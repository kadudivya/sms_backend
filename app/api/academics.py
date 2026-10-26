from flask import Blueprint, g, request
from marshmallow import Schema, fields, validate

from app.api.common import arg_int, form_or_json
from app.core.api import api
from app.core.errors import PermissionDenied
from app.core.responses import created, ok, paginated
from app.core.validation import get_or_404, load
from app.enums import (ApprovalStatus, CourseLevel, GradeSystem, OfferMode, Permission,
                       RecordStatus, Role)
from app.models import (AcademicYear, Course, CourseEquivalence, CourseOffering, CourseSyllabus,
                        College, EligibilityRule, GradingScheme, MigrationWindow, OfferingRequest, University,
                        Syllabus)
from app.services import academics as svc
from app.core.records import active

bp = Blueprint("academics", __name__, url_prefix="")

STAFF_ROLES = [Role.PLATFORM_ADMIN, Role.UNIVERSITY_ADMIN, Role.UNIVERSITY_USER, Role.COLLEGE_ADMIN,
               Role.COLLEGE_USER]


class CourseSchema(Schema):
    code = fields.String(required=True, validate=validate.Length(min=1, max=30))
    name = fields.String(required=True, validate=validate.Length(min=2, max=300))
    level = fields.String(required=True, validate=validate.OneOf(CourseLevel.values()))
    discipline = fields.String(validate=validate.Length(max=150))
    duration_years = fields.Integer(required=True, validate=validate.Range(min=1, max=7))
    total_semesters = fields.Integer(required=True, validate=validate.Range(min=1, max=14))
    offer_mode = fields.String(validate=validate.OneOf(OfferMode.values()))
    interview_required = fields.Boolean()
    confirm_similar = fields.Boolean()


class SubjectSchema(Schema):
    semester = fields.Integer(required=True, validate=validate.Range(min=1, max=14))
    code = fields.String(required=True, validate=validate.Length(min=1, max=30))
    name = fields.String(required=True, validate=validate.Length(min=1, max=300))
    credits = fields.Decimal(required=True, places=2, as_string=False)
    is_elective = fields.Boolean(load_default=False)


class SyllabusSchema(Schema):
    name = fields.String(required=True, validate=validate.Length(min=2, max=300))
    regulation_year = fields.Integer(required=True, validate=validate.Range(min=1990, max=2100))
    subjects = fields.List(fields.Nested(SubjectSchema), load_default=list)


class AssignSyllabusSchema(Schema):
    syllabus_id = fields.Integer(required=True)
    batch_from = fields.Integer(required=True, validate=validate.Range(min=1990, max=2100))
    batch_to = fields.Integer(validate=validate.Range(min=1990, max=2100), allow_none=True)


class CloseAssignmentSchema(Schema):
    batch_to = fields.Integer(required=True)


class EquivalenceSchema(Schema):
    source_course_name = fields.String(required=True, validate=validate.Length(min=2, max=300))
    source_university_id = fields.Integer(allow_none=True)
    source_course_level = fields.String(validate=validate.OneOf(CourseLevel.values()))
    note = fields.String(validate=validate.Length(max=500))


class EligibilitySchema(Schema):
    effective_from = fields.Date(required=True)
    min_percentage = fields.Decimal(places=2, validate=validate.Range(min=0, max=100), allow_none=True)
    min_completed_semesters = fields.Integer(validate=validate.Range(min=0), allow_none=True)
    max_active_backlogs = fields.Integer(validate=validate.Range(min=0), allow_none=True)
    same_discipline_required = fields.Boolean(load_default=False)
    min_credits_by_entry_semester = fields.Dict(keys=fields.String(), values=fields.Float(), load_default=dict)
    is_statutory = fields.Boolean(load_default=False)
    notes = fields.String(validate=validate.Length(max=1000))


class GradingSchemeSchema(Schema):
    grade_system = fields.String(required=True, validate=validate.OneOf(GradeSystem.values()))
    formula_expr = fields.String(validate=validate.Length(max=300))
    batch_from = fields.Integer(required=True)
    batch_to = fields.Integer(allow_none=True)
    source_url = fields.Url()


class AcademicYearSchema(Schema):
    label = fields.String(required=True, validate=validate.Regexp(r"^\d{4}-\d{2}$"))
    start_date = fields.Date(required=True)
    end_date = fields.Date(required=True)
    is_current = fields.Boolean(load_default=False)


class WindowSchema(Schema):
    academic_year_id = fields.Integer(required=True)
    opens_on = fields.Date(required=True)
    closes_on = fields.Date(required=True)
    entry_semesters = fields.List(fields.Integer(), required=True)


class WindowUpdateSchema(Schema):
    opens_on = fields.Date()
    closes_on = fields.Date()
    entry_semesters = fields.List(fields.Integer())


class AssignCourseSchema(Schema):
    college_id = fields.Integer(required=True)
    sanctioned_intake = fields.Integer(required=True, validate=validate.Range(min=1))


class OfferingRequestSchema(Schema):
    course_id = fields.Integer(required=True)
    proposed_intake = fields.Integer(required=True, validate=validate.Range(min=1))
    justification = fields.String(validate=validate.Length(max=1000))


class OfferingDecisionSchema(Schema):
    approve = fields.Boolean(required=True)
    sanctioned_intake = fields.Integer(validate=validate.Range(min=1))
    note = fields.String(validate=validate.Length(max=1000))


class OfferingUpdateSchema(Schema):
    sanctioned_intake = fields.Integer(validate=validate.Range(min=1))
    active = fields.Boolean()


def _uni(university_id):
    return get_or_404(University, university_id, label="University")


def _syllabus_view(s, with_subjects=True):
    out = s.to_dict()
    if with_subjects:
        out["subjects"] = [x.to_dict() for x in s.subjects]
        out["total_credits"] = float(sum(x.credits for x in s.subjects))
    return out


def _offering_view(o):
    return {**o.to_dict(), "college_name": o.college.name, "college_aishe_code": o.college.aishe_code,
            "course_name": o.course.name, "course_code": o.course.code, "university_id": o.course.university_id}


# ------------------------------------------------------------------ courses
@bp.post("/universities/<int:university_id>/courses")
@api("CREATE_COURSE", permissions=[Permission.MANAGE_COURSES])
def create_course(university_id):
    course = svc.create_course(g.actor, _uni(university_id), load(CourseSchema))
    return created(course.to_dict(), "Course created")


@bp.get("/universities/<int:university_id>/courses")
@api("LIST_COURSES", auth=False)
def list_courses(university_id):
    q = Course.query.filter(Course.university_id == university_id)
    if request.args.get("status", "ACTIVE") != "ALL":
        q = q.filter(Course.status == RecordStatus(request.args.get("status", "ACTIVE")))
    if request.args.get("q"):
        q = q.filter(Course.normalized_name.like(f"%{request.args['q'].lower()}%"))
    if request.args.get("level"):
        q = q.filter(Course.level == CourseLevel(request.args["level"]))
    return paginated(q.order_by(Course.name), lambda c: c.to_dict())


@bp.get("/courses/<int:course_id>")
@api("GET_COURSE", auth=False)
def get_course(course_id):
    course = get_or_404(Course, course_id, label="Course")
    assignments = [{**a.to_dict(), "syllabus_name": a.syllabus.name, "syllabus_version": a.syllabus.version_no}
                   for a in active(CourseSyllabus).filter_by(course_id=course.id)]
    from app.core.utils import today_ist
    rule = svc.rule_in_force(course.id, today_ist())
    return ok({**course.to_dict(), "syllabus_assignments": assignments,
               "eligibility_rule": rule.to_dict() if rule else None})


@bp.patch("/courses/<int:course_id>")
@api("UPDATE_COURSE", permissions=[Permission.MANAGE_COURSES])
def update_course(course_id):
    course = get_or_404(Course, course_id, label="Course")
    return ok(svc.update_course(g.actor, course, load(CourseSchema, partial=True)).to_dict(), "Course updated")


@bp.post("/courses/<int:course_id>/deactivate")
@api("DEACTIVATE_COURSE",
     permissions=[Permission.MANAGE_COURSES])
def deactivate_course(course_id):
    course = get_or_404(Course, course_id, label="Course")
    return ok(svc.set_course_active(g.actor, course, False).to_dict(), "Course deactivated")


@bp.post("/courses/<int:course_id>/activate")
@api("ACTIVATE_COURSE", permissions=[Permission.MANAGE_COURSES])
def activate_course(course_id):
    course = get_or_404(Course, course_id, label="Course")
    return ok(svc.set_course_active(g.actor, course, True).to_dict(), "Course activated")


# ----------------------------------------------------------------- syllabus
@bp.post("/universities/<int:university_id>/syllabi")
@api("CREATE_SYLLABUS", permissions=[Permission.MANAGE_SYLLABUS])
def create_syllabus(university_id):
    syl = svc.create_syllabus(g.actor, _uni(university_id), load(SyllabusSchema))
    return created(_syllabus_view(syl), "Syllabus draft created")


@bp.get("/universities/<int:university_id>/syllabi")
@api("LIST_SYLLABI", roles=STAFF_ROLES)
def list_syllabi(university_id):
    q = active(Syllabus).filter(Syllabus.university_id == university_id)
    if request.args.get("published") in ("true", "false"):
        q = q.filter(Syllabus.is_published.is_(request.args["published"] == "true"))
    return paginated(q.order_by(Syllabus.name, Syllabus.version_no.desc()), lambda s: _syllabus_view(s, False))


@bp.get("/syllabi/<int:syllabus_id>")
@api("GET_SYLLABUS", roles=STAFF_ROLES)
def get_syllabus(syllabus_id):
    return ok(_syllabus_view(get_or_404(Syllabus, syllabus_id, label="Syllabus")))


@bp.put("/syllabi/<int:syllabus_id>")
@api("UPDATE_SYLLABUS",
     permissions=[Permission.MANAGE_SYLLABUS])
def update_syllabus(syllabus_id):
    syl = get_or_404(Syllabus, syllabus_id, label="Syllabus")
    return ok(_syllabus_view(svc.update_syllabus(g.actor, syl, load(SyllabusSchema, partial=True))), "Saved")


@bp.post("/syllabi/<int:syllabus_id>/publish")
@api("PUBLISH_SYLLABUS", permissions=[Permission.MANAGE_SYLLABUS])
def publish_syllabus(syllabus_id):
    syl = get_or_404(Syllabus, syllabus_id, label="Syllabus")
    return ok(_syllabus_view(svc.publish_syllabus(g.actor, syl)), "Syllabus published")


@bp.post("/syllabi/<int:syllabus_id>/versions")
@api("CREATE_SYLLABUS_VERSION", permissions=[Permission.MANAGE_SYLLABUS])
def new_version(syllabus_id):
    syl = get_or_404(Syllabus, syllabus_id, label="Syllabus")
    return created(_syllabus_view(svc.new_syllabus_version(g.actor, syl)), "New version drafted")


@bp.post("/courses/<int:course_id>/syllabi")
@api("ASSIGN_SYLLABUS_TO_COURSE",
     permissions=[Permission.MANAGE_SYLLABUS])
def assign_syllabus(course_id):
    course = get_or_404(Course, course_id, label="Course")
    data = load(AssignSyllabusSchema)
    syl = get_or_404(Syllabus, data["syllabus_id"], label="Syllabus")
    row = svc.assign_syllabus(g.actor, course, syl, data["batch_from"], data.get("batch_to"))
    return created(row.to_dict(), "Syllabus assigned")


@bp.post("/course-syllabi/<int:row_id>/close")
@api("CLOSE_SYLLABUS_ASSIGNMENT",
     permissions=[Permission.MANAGE_SYLLABUS])
def close_assignment(row_id):
    row = get_or_404(CourseSyllabus, row_id, label="Assignment")
    return ok(svc.close_syllabus_assignment(g.actor, row, load(CloseAssignmentSchema)["batch_to"]).to_dict())


# -------------------------------------------------------------- equivalence
@bp.post("/courses/<int:course_id>/equivalences")
@api("CREATE_COURSE_EQUIVALENCE", permissions=[Permission.MANAGE_ACADEMIC_RULES])
def create_equivalence(course_id):
    course = get_or_404(Course, course_id, label="Course")
    return created(svc.create_equivalence(g.actor, course, load(EquivalenceSchema)).to_dict(), "Equivalence added")


@bp.get("/courses/<int:course_id>/equivalences")
@api("LIST_COURSE_EQUIVALENCES", auth=False)
def list_equivalences(course_id):
    q = active(CourseEquivalence).filter_by(course_id=course_id).order_by(CourseEquivalence.source_course_name)
    return paginated(q, lambda r: r.to_dict())


@bp.post("/equivalences/<int:row_id>/deactivate")
@api("DEACTIVATE_COURSE_EQUIVALENCE",
     permissions=[Permission.MANAGE_ACADEMIC_RULES])
def deactivate_equivalence(row_id):
    row = get_or_404(CourseEquivalence, row_id, label="Equivalence")
    return ok(svc.set_equivalence_active(g.actor, row, False).to_dict(), "Equivalence removed")


# ----------------------------------------------------------------- rules
@bp.post("/courses/<int:course_id>/eligibility-rules")
@api("CREATE_ELIGIBILITY_RULE",
     permissions=[Permission.MANAGE_ACADEMIC_RULES])
def create_rule(course_id):
    course = get_or_404(Course, course_id, label="Course")
    return created(svc.create_eligibility_rule(g.actor, course, load(EligibilitySchema)).to_dict(), "Rule saved")


@bp.get("/courses/<int:course_id>/eligibility-rules")
@api("LIST_ELIGIBILITY_RULES", auth=False)
def list_rules(course_id):
    q = active(EligibilityRule).filter_by(course_id=course_id).order_by(EligibilityRule.version_no.desc())
    return paginated(q, lambda r: r.to_dict())


@bp.post("/universities/<int:university_id>/grading-schemes")
@api("CREATE_GRADING_SCHEME",
     permissions=[Permission.MANAGE_ACADEMIC_RULES])
def create_scheme(university_id):
    data = load(GradingSchemeSchema, form_or_json())
    scheme = svc.create_grading_scheme(g.actor, _uni(university_id), data, request.files.get("circular"))
    return created(scheme.to_dict(), "Grading scheme saved")


@bp.get("/universities/<int:university_id>/grading-schemes")
@api("LIST_GRADING_SCHEMES", auth=False)
def list_schemes(university_id):
    q = active(GradingScheme).filter_by(university_id=university_id).order_by(GradingScheme.batch_from.desc())
    return paginated(q, lambda s: s.to_dict())


# ------------------------------------------------------------ academic years
@bp.post("/academic-years")
@api("CREATE_ACADEMIC_YEAR",
     roles=[Role.PLATFORM_ADMIN, Role.UNIVERSITY_ADMIN, Role.UNIVERSITY_USER])
def create_year():
    uid = arg_int("university_id")
    university = _uni(uid) if uid else None
    year = svc.create_academic_year(g.actor, load(AcademicYearSchema), university)
    return created(year.to_dict(), "Academic year created")


@bp.get("/academic-years")
@api("LIST_ACADEMIC_YEARS", auth=False)
def list_years():
    uid = arg_int("university_id")
    q = active(AcademicYear).filter(AcademicYear.university_id.in_([uid]) if uid else AcademicYear.university_id.is_(None))
    if uid and request.args.get("include_platform", "true") == "true":
        q = active(AcademicYear).filter((AcademicYear.university_id == uid) | AcademicYear.university_id.is_(None))
    return paginated(q.order_by(AcademicYear.start_date.desc()), lambda y: y.to_dict())


@bp.post("/academic-years/<int:year_id>/set-current")
@api("SET_CURRENT_ACADEMIC_YEAR",
     roles=[Role.PLATFORM_ADMIN, Role.UNIVERSITY_ADMIN, Role.UNIVERSITY_USER])
def set_current(year_id):
    year = get_or_404(AcademicYear, year_id, label="Academic year")
    return ok(svc.set_current_year(g.actor, year).to_dict(), "Current year set")


# --------------------------------------------------------------- windows
@bp.post("/courses/<int:course_id>/migration-windows")
@api("CREATE_MIGRATION_WINDOW",
     permissions=[Permission.MANAGE_ACADEMIC_RULES])
def create_window(course_id):
    course = get_or_404(Course, course_id, label="Course")
    data = load(WindowSchema)
    year = get_or_404(AcademicYear, data["academic_year_id"], label="Academic year")
    return created(svc.create_window(g.actor, course, year, data).to_dict(), "Window created")


@bp.patch("/migration-windows/<int:window_id>")
@api("UPDATE_MIGRATION_WINDOW",
     permissions=[Permission.MANAGE_ACADEMIC_RULES])
def update_window(window_id):
    window = get_or_404(MigrationWindow, window_id, label="Window")
    return ok(svc.update_window(g.actor, window, load(WindowUpdateSchema, partial=True)).to_dict(), "Window updated")


@bp.get("/migration-windows")
@api("LIST_MIGRATION_WINDOWS", auth=False)
def list_windows():
    q = active(MigrationWindow)
    if arg_int("course_id"):
        q = q.filter(MigrationWindow.course_id == arg_int("course_id"))
    if arg_int("academic_year_id"):
        q = q.filter(MigrationWindow.academic_year_id == arg_int("academic_year_id"))
    if arg_int("university_id"):
        q = q.join(Course, Course.id == MigrationWindow.course_id).filter(Course.university_id == arg_int("university_id"))
    return paginated(q.order_by(MigrationWindow.opens_on.desc()), lambda w: {
        **w.to_dict(), "course_name": w.course.name, "academic_year": w.academic_year.label,
        "is_open": svc.window_is_open(w)})


# -------------------------------------------------------------- offerings
@bp.post("/courses/<int:course_id>/offerings")
@api("ASSIGN_COURSE_TO_COLLEGE",
     permissions=[Permission.APPROVE_COURSE_OFFERING])
def assign_course(course_id):
    course = get_or_404(Course, course_id, label="Course")
    data = load(AssignCourseSchema)
    college = get_or_404(College, data["college_id"], label="College")
    return created(_offering_view(svc.assign_course_to_college(g.actor, course, college, data["sanctioned_intake"])),
                   "Course assigned")


@bp.post("/colleges/<int:college_id>/offering-requests")
@api("REQUEST_COURSE_OFFERING", permissions=[Permission.REQUEST_COURSE_OFFERING])
def request_offering(college_id):
    college = get_or_404(College, college_id, label="College")
    data = load(OfferingRequestSchema)
    course = get_or_404(Course, data["course_id"], active_only=True, label="Course")
    req = svc.request_offering(g.actor, college, course, data["proposed_intake"], data.get("justification"))
    return created(req.to_dict(), "Request sent to the university")


@bp.get("/offering-requests")
@api("LIST_OFFERING_REQUESTS",
     roles=STAFF_ROLES)
def list_offering_requests():
    q = active(OfferingRequest).join(Course, Course.id == OfferingRequest.course_id)
    if not g.actor.is_platform_admin:
        q = q.filter((Course.university_id.in_(list(g.actor.university_ids()) or [-1])) |
                     (OfferingRequest.college_id.in_(list(g.actor.college_ids()) or [-1])))
    status = request.args.get("approval_status", "PENDING")
    if status != "ALL":
        q = q.filter(OfferingRequest.approval_status == ApprovalStatus(status))
    return paginated(q.order_by(OfferingRequest.created_at), lambda r: {
        **r.to_dict(), "college_name": r.college.name, "course_name": r.course.name})


@bp.post("/offering-requests/<int:req_id>/decision")
@api("DECIDE_OFFERING_REQUEST",
     permissions=[Permission.APPROVE_COURSE_OFFERING])
def decide_offering(req_id):
    req = get_or_404(OfferingRequest, req_id, label="Offering request")
    data = load(OfferingDecisionSchema)
    req, offering = svc.decide_offering_request(g.actor, req, data["approve"], data.get("sanctioned_intake"),
                                                data.get("note"))
    return ok({"request": req.to_dict(), "offering": _offering_view(offering) if offering else None}, "Decided")


@bp.get("/course-offerings")
@api("LIST_COURSE_OFFERINGS", auth=False)
def list_offerings():
    q = active(CourseOffering)
    if arg_int("college_id"):
        q = q.filter(CourseOffering.college_id == arg_int("college_id"))
    if arg_int("course_id"):
        q = q.filter(CourseOffering.course_id == arg_int("course_id"))
    if arg_int("university_id"):
        q = q.join(Course, Course.id == CourseOffering.course_id).filter(Course.university_id == arg_int("university_id"))
    return paginated(q.order_by(CourseOffering.id), _offering_view)


@bp.patch("/course-offerings/<int:offering_id>")
@api("UPDATE_COURSE_OFFERING",
     permissions=[Permission.APPROVE_COURSE_OFFERING])
def update_offering(offering_id):
    offering = get_or_404(CourseOffering, offering_id, label="Offering")
    data = load(OfferingUpdateSchema, partial=True)
    if not data:
        raise PermissionDenied("Nothing to change")
    return ok(_offering_view(svc.update_offering(g.actor, offering, data.get("sanctioned_intake"),
                                                 data.get("active"))), "Offering updated")
