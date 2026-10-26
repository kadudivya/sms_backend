"""Courses, syllabi, equivalences, eligibility rules, grading schemes, academic
years, migration windows and course offerings (5, 12.1)."""
from difflib import SequenceMatcher

from simpleeval import InvalidExpression, SimpleEval

from app.core.activity import activity
from app.core.errors import (BusinessRuleViolation, Conflict, PermissionDenied, ValidationError)
from app.core.utils import normalize_name, today_ist, utcnow
from app.enums import (ApprovalStatus, CourseLevel, DocumentOwnerType, DocumentType, GradeSystem,
                       NotificationEvent, OfferMode, Permission)
from app.extensions import db
from app.models import (AcademicYear, Course, CourseEquivalence, CourseOffering, CourseSyllabus,
                        EligibilityRule, GradingScheme, MigrationWindow, OfferingRequest,
                        Syllabus, SyllabusSubject)
from app.core.institutions import is_college, is_university, university_of as _university_of
from app.services.directory import holders_of, institution_admins
from app.services.notifications import notify
from app.core.records import activate, active, deactivate, is_active, is_operational, vacancy_total_seats


def _require_university(inst):
    if not is_university(inst):
        raise ValidationError("This must be done for a university")
    if not is_operational(inst):
        raise BusinessRuleViolation("The university is not active", error_code="UNIVERSITY_NOT_ACTIVE")


# ------------------------------------------------------------------ courses
def create_course(actor, university, data):
    _require_university(university)
    actor.require(Permission.MANAGE_COURSES, university)
    code = data["code"].strip().upper()
    norm = normalize_name(data["name"])
    level = CourseLevel(data["level"])
    if Course.query.filter_by(university_id=university.id, code=code).first():
        raise Conflict(f"Course code {code} already exists in this university", error_code="DUPLICATE_COURSE_CODE")
    if Course.query.filter_by(university_id=university.id, normalized_name=norm, level=level).first():
        raise Conflict("A course with this name and level already exists", error_code="DUPLICATE_COURSE")
    similar = [c.name for c in Course.query.filter_by(university_id=university.id).all()
               if SequenceMatcher(None, c.normalized_name, norm).ratio() >= 0.85]
    if similar and not data.get("confirm_similar"):
        raise Conflict("Similar course names exist. Resend with confirm_similar=true to create anyway.",
                       error_code="SIMILAR_COURSE_EXISTS", details={"similar": similar})
    if data["total_semesters"] < 1 or data["duration_years"] < 1:
        raise ValidationError("Duration and semesters must be positive")
    course = Course(university_id=university.id, code=code, name=data["name"].strip(), normalized_name=norm,
                    level=level, discipline=data.get("discipline"), duration_years=data["duration_years"],
                    total_semesters=data["total_semesters"],
                    offer_mode=OfferMode(data.get("offer_mode", OfferMode.MERIT_ROUND.value)),
                    interview_required=data.get("interview_required", False))
    db.session.add(course)
    db.session.flush()
    activity.step("Course created", entity="course", entity_id=course.id, code=code)
    activity.set_institution(university)
    db.session.commit()
    return course


def update_course(actor, course, data):
    actor.require(Permission.MANAGE_COURSES, course.university)
    for key in ("name", "discipline", "duration_years", "total_semesters", "offer_mode", "interview_required"):
        if key in data:
            value = OfferMode(data[key]) if key == "offer_mode" else data[key]
            activity.change("course", course.id, key, getattr(course, key), value)
            setattr(course, key, value)
    if "name" in data:
        course.normalized_name = normalize_name(data["name"])
    db.session.commit()
    return course


def set_course_active(actor, course, active):
    actor.require(Permission.MANAGE_COURSES, course.university)
    activate(course) if active else deactivate(course)
    activity.step("Course " + ("activated" if active else "deactivated"), entity="course", entity_id=course.id)
    db.session.commit()
    return course


# ------------------------------------------------------------------ syllabus
def _validate_subjects(subjects):
    codes = [s["code"].strip().upper() for s in subjects]
    dupes = {c for c in codes if codes.count(c) > 1}
    if dupes:
        raise ValidationError("Duplicate subject codes: " + ", ".join(sorted(dupes)))
    for s in subjects:
        if s["credits"] < 0 or s["semester"] < 1:
            raise ValidationError(f"Subject {s['code']}: credits must be >= 0 and semester >= 1")


def _replace_subjects(syllabus, subjects):
    for old in list(syllabus.subjects):
        db.session.delete(old)
    db.session.flush()
    for s in subjects:
        db.session.add(SyllabusSubject(syllabus_id=syllabus.id, semester=s["semester"],
                                       code=s["code"].strip().upper(), name=s["name"].strip(),
                                       credits=s["credits"], is_elective=s.get("is_elective", False)))


def create_syllabus(actor, university, data):
    _require_university(university)
    actor.require(Permission.MANAGE_SYLLABUS, university)
    _validate_subjects(data.get("subjects", []))
    syl = Syllabus(university_id=university.id, name=data["name"].strip(),
                   regulation_year=data["regulation_year"], version_no=1)
    db.session.add(syl)
    db.session.flush()
    _replace_subjects(syl, data.get("subjects", []))
    activity.step("Syllabus draft created", entity="syllabus", entity_id=syl.id,
                  subjects=len(data.get("subjects", [])))
    db.session.commit()
    return syl


def update_syllabus(actor, syl, data):
    actor.require(Permission.MANAGE_SYLLABUS, syl.university)
    if syl.is_published:
        raise BusinessRuleViolation("A published syllabus is read-only; create a new version",
                                    error_code="SYLLABUS_PUBLISHED")
    if "name" in data:
        syl.name = data["name"]
    if "regulation_year" in data:
        syl.regulation_year = data["regulation_year"]
    if "subjects" in data:
        _validate_subjects(data["subjects"])
        _replace_subjects(syl, data["subjects"])
    activity.step("Syllabus draft updated", entity="syllabus", entity_id=syl.id)
    db.session.commit()
    return syl


def publish_syllabus(actor, syl):
    actor.require(Permission.MANAGE_SYLLABUS, syl.university)
    if syl.is_published:
        raise BusinessRuleViolation("Already published")
    if not syl.subjects:
        raise BusinessRuleViolation("Add subjects before publishing", error_code="SYLLABUS_EMPTY")
    syl.is_published = True
    syl.published_at = utcnow()
    activity.step("Syllabus published", entity="syllabus", entity_id=syl.id, version=syl.version_no)
    db.session.commit()
    return syl


def new_syllabus_version(actor, syl):
    actor.require(Permission.MANAGE_SYLLABUS, syl.university)
    latest = (db.session.query(db.func.max(Syllabus.version_no))
              .filter_by(university_id=syl.university_id, name=syl.name).scalar())
    copy = Syllabus(university_id=syl.university_id, name=syl.name, regulation_year=syl.regulation_year,
                    version_no=latest + 1)
    db.session.add(copy)
    db.session.flush()
    _replace_subjects(copy, [{"semester": s.semester, "code": s.code, "name": s.name, "credits": s.credits,
                              "is_elective": s.is_elective} for s in syl.subjects])
    activity.step("New syllabus version drafted", entity="syllabus", entity_id=copy.id, version=copy.version_no)
    db.session.commit()
    return copy


def assign_syllabus(actor, course, syl, batch_from, batch_to=None):
    actor.require(Permission.MANAGE_SYLLABUS, course.university)
    if syl.university_id != course.university_id:
        raise ValidationError("The syllabus belongs to another university")
    if not syl.is_published:
        raise BusinessRuleViolation("Publish the syllabus before assigning it", error_code="SYLLABUS_NOT_PUBLISHED")
    if batch_to is not None and batch_to < batch_from:
        raise ValidationError("batch_to must not be before batch_from")
    hi = batch_to if batch_to is not None else 9999
    for existing in active(CourseSyllabus).filter_by(course_id=course.id).all():
        ex_hi = existing.batch_to if existing.batch_to is not None else 9999
        if existing.batch_from <= hi and batch_from <= ex_hi:
            raise Conflict(f"Batches overlap with an existing assignment ({existing.batch_from}-"
                           f"{existing.batch_to or 'open'}). Close it first.", error_code="BATCH_OVERLAP")
    row = CourseSyllabus(course_id=course.id, syllabus_id=syl.id, batch_from=batch_from, batch_to=batch_to)
    db.session.add(row)
    db.session.flush()
    activity.step("Syllabus assigned to course", entity="course_syllabus", entity_id=row.id)
    db.session.commit()
    return row


def close_syllabus_assignment(actor, row, batch_to):
    actor.require(Permission.MANAGE_SYLLABUS, row.course.university)
    if batch_to < row.batch_from:
        raise ValidationError("batch_to must not be before batch_from")
    row.batch_to = batch_to
    db.session.commit()
    return row


def syllabus_for_batch(course_id, batch_year):
    q = active(CourseSyllabus).filter(CourseSyllabus.course_id == course_id,
                                       CourseSyllabus.batch_from <= batch_year)
    for row in q.order_by(CourseSyllabus.batch_from.desc()).all():
        if row.batch_to is None or row.batch_to >= batch_year:
            return row.syllabus
    return None


# ------------------------------------------------------------ equivalences
def create_equivalence(actor, course, data):
    actor.require(Permission.MANAGE_ACADEMIC_RULES, course.university)
    norm = normalize_name(data["source_course_name"])
    exists = active(CourseEquivalence).filter_by(course_id=course.id, normalized_source_name=norm,
                                                  source_university_id=data.get("source_university_id")).first()
    if exists:
        raise Conflict("This equivalence already exists", error_code="DUPLICATE_EQUIVALENCE")
    row = CourseEquivalence(course_id=course.id, source_university_id=data.get("source_university_id"),
                            source_course_name=data["source_course_name"].strip(), normalized_source_name=norm,
                            source_course_level=CourseLevel(data["source_course_level"])
                            if data.get("source_course_level") else None, note=data.get("note"))
    db.session.add(row)
    db.session.flush()
    activity.step("Course equivalence added", entity="course_equivalence", entity_id=row.id)
    db.session.commit()
    return row


def set_equivalence_active(actor, row, active):
    course = db.session.get(Course, row.course_id)
    actor.require(Permission.MANAGE_ACADEMIC_RULES, course.university)
    activate(row) if active else deactivate(row)
    db.session.commit()
    return row


def accepts_source_course(course, source_course_name, source_university_id):
    """True / False / None (None = no list configured, decide manually)."""
    rows = active(CourseEquivalence).filter_by(course_id=course.id).all()
    norm = normalize_name(source_course_name)
    if norm and norm == course.normalized_name:
        return True
    if not rows:
        return None
    for r in rows:
        if r.normalized_source_name == norm and (r.source_university_id in (None, source_university_id)):
            return True
    return False


# ------------------------------------------------------------ eligibility
def create_eligibility_rule(actor, course, data):
    actor.require(Permission.MANAGE_ACADEMIC_RULES, course.university)
    latest = (db.session.query(db.func.max(EligibilityRule.version_no))
              .filter_by(course_id=course.id).scalar()) or 0
    mins = {str(k): float(v) for k, v in (data.get("min_credits_by_entry_semester") or {}).items()}
    rule = EligibilityRule(course_id=course.id, version_no=latest + 1, effective_from=data["effective_from"],
                           min_percentage=data.get("min_percentage"),
                           min_completed_semesters=data.get("min_completed_semesters"),
                           max_active_backlogs=data.get("max_active_backlogs"),
                           same_discipline_required=data.get("same_discipline_required", False),
                           min_credits_by_entry_semester=mins, is_statutory=data.get("is_statutory", False),
                           notes=data.get("notes"))
    db.session.add(rule)
    db.session.flush()
    activity.step("Eligibility rule version created", entity="eligibility_rule", entity_id=rule.id,
                  version=rule.version_no)
    db.session.commit()
    return rule


def rule_in_force(course_id, on_date):
    """M12-F07: the version in force on the submission date."""
    return (active(EligibilityRule).filter(EligibilityRule.course_id == course_id,
                                            EligibilityRule.effective_from <= on_date)
            .order_by(EligibilityRule.effective_from.desc(), EligibilityRule.version_no.desc()).first())


# ---------------------------------------------------------- grading scheme
def evaluate_formula(expr, cgpa):
    evaluator = SimpleEval(names={"cgpa": float(cgpa)}, functions={"min": min, "max": max, "round": round})
    try:
        value = float(evaluator.eval(expr))
    except (InvalidExpression, SyntaxError, TypeError, ZeroDivisionError, ValueError) as exc:
        raise ValidationError(f"Formula could not be evaluated: {exc}", error_code="INVALID_FORMULA") from exc
    return round(value, 2)


def create_grading_scheme(actor, university, data, circular_file=None):
    _require_university(university)
    actor.require(Permission.MANAGE_ACADEMIC_RULES, university)
    system = GradeSystem(data["grade_system"])
    expr = (data.get("formula_expr") or "").strip() or None
    if system in (GradeSystem.CGPA_10, GradeSystem.CGPA_4):
        if not expr:
            raise ValidationError("A conversion formula is required for CGPA systems")
        top = 10 if system == GradeSystem.CGPA_10 else 4
        for sample in (top * 0.4, top * 0.8, top):
            value = evaluate_formula(expr, sample)
            if not 0 <= value <= 100:
                raise ValidationError(f"Formula gives {value}% for CGPA {sample}; it must stay within 0-100",
                                      error_code="INVALID_FORMULA")
    if circular_file is None and not data.get("source_url"):
        raise ValidationError("Attach the official circular or give its URL", error_code="CIRCULAR_REQUIRED")
    hi = data.get("batch_to") or 9999
    for ex in active(GradingScheme).filter_by(university_id=university.id, grade_system=system).all():
        ex_hi = ex.batch_to or 9999
        if ex.batch_from <= hi and data["batch_from"] <= ex_hi:
            raise Conflict("Batches overlap with an existing grading scheme", error_code="BATCH_OVERLAP")
    scheme = GradingScheme(university_id=university.id, grade_system=system, formula_expr=expr,
                           batch_from=data["batch_from"], batch_to=data.get("batch_to"),
                           source_url=data.get("source_url"))
    db.session.add(scheme)
    db.session.flush()
    if circular_file is not None:
        from app.services.documents import store_upload
        doc = store_upload(circular_file, DocumentOwnerType.UNIVERSITY, university.id,
                           DocumentType.GRADING_CIRCULAR, institution=university)
        scheme.circular_document_id = doc.id
    activity.step("Grading scheme saved", entity="grading_scheme", entity_id=scheme.id)
    db.session.commit()
    return scheme


def scheme_for(university_id, grade_system, batch_year):
    q = active(GradingScheme).filter(GradingScheme.university_id == university_id,
                                      GradingScheme.grade_system == GradeSystem(grade_system),
                                      GradingScheme.batch_from <= batch_year)
    for s in q.order_by(GradingScheme.batch_from.desc()).all():
        if s.batch_to is None or s.batch_to >= batch_year:
            return s
    return None


# --------------------------------------------------------- academic years
def create_academic_year(actor, data, university=None):
    if university is None:
        if not actor.is_platform_admin:
            raise PermissionDenied("Only the Platform Admin maintains the platform calendar")
    else:
        _require_university(university)
        actor.require(Permission.MANAGE_ACADEMIC_RULES, university)
    if data["end_date"] <= data["start_date"]:
        raise ValidationError("End date must be after start date")
    uid = university.id if university else None
    for ex in active(AcademicYear).filter_by(university_id=uid).all():
        if ex.start_date <= data["end_date"] and data["start_date"] <= ex.end_date:
            raise Conflict(f"Overlaps academic year {ex.label}", error_code="ACADEMIC_YEAR_OVERLAP")
    year = AcademicYear(university_id=uid, label=data["label"], start_date=data["start_date"],
                        end_date=data["end_date"], is_current=False)
    db.session.add(year)
    db.session.flush()
    if data.get("is_current"):
        _make_current(year)
    activity.step("Academic year created", entity="academic_year", entity_id=year.id, label=year.label)
    db.session.commit()
    return year


def _make_current(year):
    for other in active(AcademicYear).filter_by(university_id=year.university_id, is_current=True).all():
        other.is_current = False
    year.is_current = True


def set_current_year(actor, year):
    if year.university_id is None:
        if not actor.is_platform_admin:
            raise PermissionDenied("Platform Admin only")
    else:
        actor.require(Permission.MANAGE_ACADEMIC_RULES, year.university)
    _make_current(year)
    db.session.commit()
    return year


# --------------------------------------------------------- migration window
def _validate_window(course, year, data):
    if year.university_id not in (None, course.university_id):
        raise ValidationError("The academic year belongs to another university")
    if data["closes_on"] < data["opens_on"]:
        raise ValidationError("closes_on must not be before opens_on")
    sems = sorted(set(int(s) for s in data["entry_semesters"]))
    if not sems or any(s < 2 or s > course.total_semesters for s in sems):
        raise ValidationError(f"Entry semesters must be between 2 and {course.total_semesters}")
    return sems


def create_window(actor, course, year, data):
    actor.require(Permission.MANAGE_ACADEMIC_RULES, course.university)
    sems = _validate_window(course, year, data)
    if active(MigrationWindow).filter_by(course_id=course.id, academic_year_id=year.id).first():
        raise Conflict("A window already exists for this course and year; update it instead",
                       error_code="WINDOW_EXISTS")
    window = MigrationWindow(course_id=course.id, academic_year_id=year.id, opens_on=data["opens_on"],
                             closes_on=data["closes_on"], entry_semesters=sems)
    db.session.add(window)
    db.session.flush()
    activity.step("Migration window created", entity="migration_window", entity_id=window.id)
    db.session.commit()
    return window


def update_window(actor, window, data):
    course = window.course
    actor.require(Permission.MANAGE_ACADEMIC_RULES, course.university)
    if window.merit_round_completed_at:
        raise BusinessRuleViolation("The merit round has run; the window can no longer change",
                                    error_code="WINDOW_LOCKED")
    merged = {"opens_on": data.get("opens_on", window.opens_on), "closes_on": data.get("closes_on", window.closes_on),
              "entry_semesters": data.get("entry_semesters", window.entry_semesters)}
    sems = _validate_window(course, window.academic_year, merged)
    for key, value in (("opens_on", merged["opens_on"]), ("closes_on", merged["closes_on"]), ("entry_semesters", sems)):
        activity.change("migration_window", window.id, key, getattr(window, key), value)
        setattr(window, key, value)
    db.session.commit()
    return window


def window_is_open(window, on=None):
    on = on or today_ist()
    return is_active(window) and window.opens_on <= on <= window.closes_on


# --------------------------------------------------------- course offerings
def assign_course_to_college(actor, course, college, sanctioned_intake):
    actor.require(Permission.APPROVE_COURSE_OFFERING, course.university)
    if not is_college(college) or college.university_id != course.university_id:
        raise ValidationError("The college is not affiliated to this course's university",
                              error_code="NOT_AFFILIATED")
    if not is_operational(college):
        raise BusinessRuleViolation("The college is not active", error_code="COLLEGE_NOT_ACTIVE")
    existing = CourseOffering.query.filter_by(college_id=college.id, course_id=course.id).first()
    if existing and is_active(existing):
        raise Conflict("The college already offers this course", error_code="OFFERING_EXISTS")
    if existing:
        activate(existing)
        existing.sanctioned_intake = sanctioned_intake
        offering = existing
    else:
        offering = CourseOffering(college_id=college.id, course_id=course.id, sanctioned_intake=sanctioned_intake,
                                  assigned_directly=True)
        db.session.add(offering)
    db.session.flush()
    activity.step("Course assigned to college", entity="course_offering", entity_id=offering.id)
    db.session.commit()
    return offering


def request_offering(actor, college, course, proposed_intake, justification=None):
    actor.require(Permission.REQUEST_COURSE_OFFERING, college)
    if not is_college(college) or college.university_id != course.university_id:
        raise ValidationError("You can request only your university's courses", error_code="NOT_AFFILIATED")
    if active(CourseOffering).filter_by(college_id=college.id, course_id=course.id).first():
        raise Conflict("Your college already offers this course", error_code="OFFERING_EXISTS")
    if active(OfferingRequest).filter_by(college_id=college.id, course_id=course.id,
                                          approval_status=ApprovalStatus.PENDING).first():
        raise Conflict("A request for this course is already pending", error_code="REQUEST_PENDING")
    req = OfferingRequest(college_id=college.id, course_id=course.id, proposed_intake=proposed_intake,
                          justification=justification)
    db.session.add(req)
    db.session.flush()
    for u in holders_of(course.university, Permission.APPROVE_COURSE_OFFERING) or institution_admins(course.university):
        notify(u, NotificationEvent.OFFERING_REQUEST_PENDING, {"college": college.name, "course": course.name})
    activity.step("Course offering requested", entity="offering_request", entity_id=req.id)
    db.session.commit()
    return req


def decide_offering_request(actor, req, approve, sanctioned_intake=None, note=None):
    course = req.course
    actor.require(Permission.APPROVE_COURSE_OFFERING, course.university)
    if req.approval_status != ApprovalStatus.PENDING:
        raise BusinessRuleViolation("Already decided", error_code="ALREADY_DECIDED")
    req.decided_by_id = actor.user.id
    req.decided_at = utcnow()
    req.decision_note = note
    offering = None
    if approve:
        req.approval_status = ApprovalStatus.APPROVED
        offering = CourseOffering.query.filter_by(college_id=req.college_id, course_id=course.id).first()
        intake = sanctioned_intake or req.proposed_intake
        if offering:
            activate(offering)
            offering.sanctioned_intake = intake
            offering.offering_request_id = req.id
        else:
            offering = CourseOffering(college_id=req.college_id, course_id=course.id, sanctioned_intake=intake,
                                      offering_request_id=req.id)
            db.session.add(offering)
        db.session.flush()
    else:
        if not note:
            raise ValidationError("A reason is required to reject")
        req.approval_status = ApprovalStatus.REJECTED
    for u in institution_admins(req.college):
        notify(u, NotificationEvent.OFFERING_REQUEST_DECIDED,
               {"course": course.name, "decision": req.approval_status.value.lower(), "note": note or ""})
    activity.step("Offering request decided", entity="offering_request", entity_id=req.id,
                  decision=req.approval_status)
    db.session.commit()
    return req, offering


def update_offering(actor, offering, sanctioned_intake=None, active=None):
    actor.require(Permission.APPROVE_COURSE_OFFERING, offering.course.university)
    if sanctioned_intake is not None:
        from app.models import Vacancy
        for vac in active(Vacancy).filter_by(offering_id=offering.id).all():
            if vacancy_total_seats(vac) > sanctioned_intake:
                raise BusinessRuleViolation("Uploaded vacancies exceed the new intake; correct them first",
                                            error_code="INTAKE_BELOW_VACANCY")
        activity.change("course_offering", offering.id, "sanctioned_intake", offering.sanctioned_intake,
                        sanctioned_intake)
        offering.sanctioned_intake = sanctioned_intake
    if active is not None:
        activate(offering) if active else deactivate(offering)
    db.session.commit()
    return offering


def university_of(inst):
    """The university itself, or a college's parent university (None for a college without one)."""
    return _university_of(inst)
