from flask import Blueprint, g
from marshmallow import Schema, fields, validate

from app.api.common import arg_int
from app.core.api import api
from app.core.responses import ok, paginated
from app.core.validation import get_or_404, load
from app.enums import Permission, Weekday
from app.models import (AcademicYear, College, CourseOffering, InterviewWorkingHours, Vacancy)
from app.services import vacancies as svc
from app.core.records import active

bp = Blueprint("vacancies", __name__, url_prefix="")


class VacancySchema(Schema):
    offering_id = fields.Integer(required=True)
    academic_year_id = fields.Integer(required=True)
    entry_semester = fields.Integer(required=True)
    categories = fields.Dict(keys=fields.String(), values=fields.Integer(), required=True)


class HoursRowSchema(Schema):
    weekday = fields.String(required=True, validate=validate.OneOf(Weekday.values()))
    start_time = fields.Time(required=True)
    end_time = fields.Time(required=True)
    slot_minutes = fields.Integer(load_default=30)


class HoursSchema(Schema):
    hours = fields.List(fields.Nested(HoursRowSchema), required=True)


@bp.put("/vacancies")
@api("UPSERT_VACANCY",
     permissions=[Permission.MANAGE_VACANCIES])
def upsert_vacancy():
    data = load(VacancySchema)
    offering = get_or_404(CourseOffering, data["offering_id"], label="Offering")
    year = get_or_404(AcademicYear, data["academic_year_id"], label="Academic year")
    vac = svc.upsert_vacancy(g.actor, offering, year, data["entry_semester"], data["categories"])
    return ok(svc.vacancy_view(vac), "Vacancy saved")


@bp.get("/vacancies")
@api("LIST_VACANCIES", auth=False)
def list_vacancies():
    q = active(Vacancy).join(CourseOffering, CourseOffering.id == Vacancy.offering_id)
    for arg, col in (("college_id", CourseOffering.college_id), ("course_id", CourseOffering.course_id),
                     ("offering_id", Vacancy.offering_id), ("academic_year_id", Vacancy.academic_year_id),
                     ("entry_semester", Vacancy.entry_semester)):
        if arg_int(arg):
            q = q.filter(col == arg_int(arg))
    return paginated(q.order_by(Vacancy.id), svc.vacancy_view)


@bp.put("/colleges/<int:college_id>/interview-hours")
@api("SET_INTERVIEW_WORKING_HOURS",
     permissions=[Permission.MANAGE_INTERVIEW_HOURS])
def set_hours(college_id):
    college = get_or_404(College, college_id, label="College")
    rows = svc.set_working_hours(g.actor, college, load(HoursSchema)["hours"])
    return ok([r.to_dict() for r in rows], "Working hours saved")


@bp.get("/colleges/<int:college_id>/interview-hours")
@api("LIST_INTERVIEW_WORKING_HOURS", auth=False)
def list_hours(college_id):
    rows = active(InterviewWorkingHours).filter_by(college_id=college_id).all()
    return ok([r.to_dict() for r in rows])

