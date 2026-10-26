"""Student search over offerings with live availability (M10)."""
from flask import Blueprint, request
from sqlalchemy import func

from app.api.common import arg_int
from app.core.api import api
from app.core.responses import ok, paginated
from app.core.utils import today_ist
from app.core.validation import get_or_404
from app.enums import CourseLevel, OnboardingStatus, RecordStatus
from app.extensions import db
from app.models import (AcademicYear, College, Course, CourseOffering, MigrationWindow, University, Vacancy,
                        VacancyCategory)
from app.core.records import active, vacancy_available

bp = Blueprint("search", __name__, url_prefix="/search")


def _offering_card(o, year_id=None, entry_semester=None):
    course = o.course
    q = active(Vacancy).filter(Vacancy.offering_id == o.id)
    if year_id:
        q = q.filter(Vacancy.academic_year_id == year_id)
    if entry_semester:
        q = q.filter(Vacancy.entry_semester == entry_semester)
    vacancies = q.all()
    windows = active(MigrationWindow).filter_by(course_id=course.id).filter(
        MigrationWindow.closes_on >= today_ist()).order_by(MigrationWindow.opens_on).all()
    window = windows[0] if windows else None
    available = sum(vacancy_available(v) for v in vacancies)
    return {
        "offering_id": o.id, "course_id": course.id, "course_name": course.name, "course_code": course.code,
        "level": course.level.value, "discipline": course.discipline,
        "college_id": o.college_id, "college_name": o.college.name, "college_aishe_code": o.college.aishe_code,
        "university_id": course.university_id, "university_name": course.university.name,
        "state": o.college.state.name if o.college.state else None,
        "district": o.college.district.name if o.college.district else None, "pincode": o.college.pincode,
        "vacancy_uploaded": bool(vacancies),
        "available_seats": available if vacancies else None,
        "accepting_requests": bool(vacancies) and available > 0 and window is not None
        and window.opens_on <= today_ist() <= window.closes_on,
        "window": ({"id": window.id, "opens_on": window.opens_on.isoformat(), "closes_on": window.closes_on.isoformat(),
                    "entry_semesters": window.entry_semesters, "academic_year_id": window.academic_year_id}
                   if window else None),
        "interview_required": course.interview_required, "offer_mode": course.offer_mode.value,
        "by_semester": [{"academic_year_id": v.academic_year_id, "entry_semester": v.entry_semester,
                         "available": vacancy_available(v)} for v in vacancies],
    }


@bp.get("/offerings")
@api("SEARCH_OFFERINGS", auth=False)
def search_offerings():
    q = (active(CourseOffering).join(Course, Course.id == CourseOffering.course_id)
         .join(College, College.id == CourseOffering.college_id)
         .filter(Course.status == RecordStatus.ACTIVE, College.onboarding_status == OnboardingStatus.ACTIVE,
                 College.status == RecordStatus.ACTIVE))
    if request.args.get("course"):
        q = q.filter(Course.normalized_name.like(f"%{request.args['course'].lower()}%"))
    if request.args.get("college"):
        q = q.filter(func.lower(College.name).like(f"%{request.args['college'].lower()}%"))
    if request.args.get("university"):
        uni_ids = [u.id for u in University.query.filter(
            func.lower(University.name).like(f"%{request.args['university'].lower()}%"))]
        q = q.filter(Course.university_id.in_(uni_ids or [-1]))
    for arg, col in (("university_id", Course.university_id), ("state_id", College.state_id),
                     ("district_id", College.district_id)):
        if arg_int(arg):
            q = q.filter(col == arg_int(arg))
    if request.args.get("pincode"):
        q = q.filter(College.pincode == request.args["pincode"])
    if request.args.get("level"):
        q = q.filter(Course.level == CourseLevel(request.args["level"]))
    year_id, sem = arg_int("academic_year_id"), arg_int("entry_semester")
    if request.args.get("available_only") == "true" or year_id or sem:
        vq = db.session.query(Vacancy.offering_id).join(VacancyCategory, VacancyCategory.vacancy_id == Vacancy.id) \
            .filter(Vacancy.status == RecordStatus.ACTIVE, VacancyCategory.status == RecordStatus.ACTIVE)
        if year_id:
            vq = vq.filter(Vacancy.academic_year_id == year_id)
        if sem:
            vq = vq.filter(Vacancy.entry_semester == sem)
        if request.args.get("available_only") == "true":
            vq = vq.filter(VacancyCategory.seats - VacancyCategory.blocked - VacancyCategory.filled > 0)
        q = q.filter(CourseOffering.id.in_(vq))
    sort = request.args.get("sort", "course")
    order = {"course": Course.name, "college": College.name}.get(sort, Course.name)
    return paginated(q.order_by(order, CourseOffering.id), lambda o: _offering_card(o, year_id, sem))


@bp.get("/offerings/<int:offering_id>")
@api("GET_OFFERING_DETAILS", auth=False)
def offering_details(offering_id):
    from app.services.academics import rule_in_force
    o = get_or_404(CourseOffering, offering_id, active_only=True, label="Offering")
    rule = rule_in_force(o.course_id, today_ist())
    years = {y.id: y.label for y in active(AcademicYear)}
    card = _offering_card(o)
    for row in card["by_semester"]:
        row["academic_year"] = years.get(row["academic_year_id"])
    return ok({**card, "eligibility_rule": rule.to_dict() if rule else None})
