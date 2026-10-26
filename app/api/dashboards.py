import io

from flask import Blueprint, g, request, send_file
from marshmallow import Schema, fields

from app.core.api import api
from app.core.errors import PermissionDenied
from app.core.responses import ok
from app.core.validation import get_or_404, load
from app.enums import Permission, Role
from app.models import College, University
from app.services import reports as svc

bp = Blueprint("dashboards", __name__, url_prefix="")


class ReportFilters(Schema):
    date_from = fields.Date()
    date_to = fields.Date()
    college_id = fields.Integer()
    course_id = fields.Integer()
    academic_year_id = fields.Integer()
    status = fields.String()
    direction = fields.String()


@bp.get("/dashboards/student")
@api("GET_STUDENT_DASHBOARD", roles=[Role.STUDENT])
def student():
    return ok(svc.student_dashboard(g.current_user))


@bp.get("/dashboards/college/<int:college_id>")
@api("GET_COLLEGE_DASHBOARD",
     roles=[Role.PLATFORM_ADMIN, Role.COLLEGE_ADMIN, Role.COLLEGE_USER, Role.UNIVERSITY_ADMIN, Role.UNIVERSITY_USER])
def college(college_id):
    inst = get_or_404(College, college_id, label="College")
    return ok(svc.college_dashboard(g.actor, inst))


@bp.get("/dashboards/university/<int:university_id>")
@api("GET_UNIVERSITY_DASHBOARD",
     roles=[Role.PLATFORM_ADMIN, Role.UNIVERSITY_ADMIN, Role.UNIVERSITY_USER])
def university(university_id):
    inst = get_or_404(University, university_id, label="University")
    return ok(svc.university_dashboard(g.actor, inst))


@bp.get("/dashboards/platform")
@api("GET_PLATFORM_DASHBOARD",
     roles=[Role.PLATFORM_ADMIN])
def platform():
    return ok(svc.platform_dashboard())


REPORT_CODES = {  # report name -> API code (descriptions are in config.API_CODES)
    "applications": "REPORT_APPLICATIONS",
    "vacancies": "REPORT_VACANCIES",
    "migration-flow": "REPORT_MIGRATION_FLOW",
    "turnaround": "REPORT_TURNAROUND",
    "interviews": "REPORT_INTERVIEWS",
    "credit-transfer": "REPORT_CREDIT_TRANSFER",
    "document-verification": "REPORT_DOCUMENT_VERIFICATION",
    "onboarding": "REPORT_ONBOARDING",
}


def _make_report_view(name, fn):
    def view():
        if name == "onboarding" and not g.actor.is_platform_admin:
            raise PermissionDenied("The onboarding report is for the Platform Admin")
        filters = load(ReportFilters, request.args.to_dict())
        data = fn(g.actor, filters)
        if request.args.get("format") == "xlsx":
            content = svc.to_xlsx(name, data["rows"])
            return send_file(io.BytesIO(content), as_attachment=True, download_name=f"{name}-report.xlsx",
                             mimetype="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")
        return ok(data)

    code = REPORT_CODES[name]
    view.__name__ = code.lower()
    decorated = api(code, permissions=[Permission.VIEW_REPORTS])(view)
    bp.add_url_rule(f"/reports/{name}", endpoint=code.lower(), view_func=decorated, methods=["GET"])


for _name, _fn in svc.REPORTS.items():
    _make_report_view(_name, _fn)
