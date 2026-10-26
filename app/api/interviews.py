from flask import Blueprint, g, request
from marshmallow import Schema, fields, validate

from app.api.common import arg_int
from app.core.api import api
from app.core.responses import created, ok, paginated
from app.core.validation import load
from app.enums import InterviewMode, InterviewOutcome, InterviewStatus, Permission, Role
from app.models import Interview
from app.services import interviews as svc
from app.services import migration as migration_svc
from app.core.records import is_active

bp = Blueprint("interviews", __name__, url_prefix="/interviews")
EVERYONE = [Role.PLATFORM_ADMIN, Role.UNIVERSITY_ADMIN, Role.UNIVERSITY_USER, Role.COLLEGE_ADMIN,
            Role.COLLEGE_USER, Role.STUDENT]


class ScheduleSchema(Schema):
    request_id = fields.Integer(required=True)
    scheduled_start = fields.AwareDateTime(required=True, default_timezone=None)
    scheduled_end = fields.AwareDateTime(required=True, default_timezone=None)
    mode = fields.String(validate=validate.OneOf(InterviewMode.values()), load_default=InterviewMode.ONLINE.value)
    location = fields.String(validate=validate.Length(max=500))  # in person: room or address
    meeting_url = fields.String(validate=validate.Length(max=1000))  # online: the college's own meeting link
    meeting_info = fields.String(validate=validate.Length(max=500))  # optional meeting ID / passcode
    panel_user_ids = fields.List(fields.Integer(), load_default=list)
    extra_slot_reason = fields.String(validate=validate.Length(max=500))


class RescheduleSchema(Schema):
    scheduled_start = fields.AwareDateTime(required=True)
    scheduled_end = fields.AwareDateTime(required=True)
    mode = fields.String(validate=validate.OneOf(InterviewMode.values()))
    location = fields.String(validate=validate.Length(max=500))
    meeting_url = fields.String(validate=validate.Length(max=1000))
    meeting_info = fields.String(validate=validate.Length(max=500))
    extra_slot_reason = fields.String(validate=validate.Length(max=500))


class CancelSchema(Schema):
    reason = fields.String(required=True, validate=validate.Length(min=3, max=500))


class OutcomeSchema(Schema):
    outcome = fields.String(required=True, validate=validate.OneOf([InterviewOutcome.PASSED.value,
                                                                     InterviewOutcome.FAILED.value]))
    remarks = fields.String(validate=validate.Length(max=2000))


class NoShowSchema(Schema):
    note = fields.String(validate=validate.Length(max=500))


class MeetingLinkSchema(Schema):
    meeting_url = fields.String(required=True, validate=validate.Length(min=8, max=1000))
    meeting_info = fields.String(validate=validate.Length(max=500), allow_none=True)


def _view(iv):
    return {**iv.to_dict(), "panel_user_ids": [p.user_id for p in iv.panel if is_active(p)]}


def _visible(iv):
    from app.core.errors import PermissionDenied
    if not svc.visible_interviews(g.actor).filter(Interview.id == iv.id).first():
        raise PermissionDenied("This interview is outside your scope")
    return iv


@bp.post("")
@api("SCHEDULE_INTERVIEW",
     permissions=[Permission.CONDUCT_INTERVIEW])
def schedule():
    data = load(ScheduleSchema)
    req = migration_svc.get_request(data["request_id"])
    return created(_view(svc.schedule(g.actor, req, data)), "Interview scheduled")


@bp.get("")
@api("LIST_INTERVIEWS", roles=EVERYONE)
def list_interviews():
    q = svc.visible_interviews(g.actor)
    if request.args.get("status"):
        q = q.filter(Interview.interview_status == InterviewStatus(request.args["status"]))
    if arg_int("request_id"):
        q = q.filter(Interview.request_id == arg_int("request_id"))
    if request.args.get("from"):
        q = q.filter(Interview.scheduled_start >= request.args["from"])
    if request.args.get("to"):
        q = q.filter(Interview.scheduled_start <= request.args["to"])
    return paginated(q.order_by(Interview.scheduled_start), _view)


@bp.get("/<int:iv_id>")
@api("GET_INTERVIEW", roles=EVERYONE)
def get_interview(iv_id):
    return ok(_view(_visible(svc.get_interview(iv_id))))


@bp.post("/<int:iv_id>/reschedule")
@api("RESCHEDULE_INTERVIEW", permissions=[Permission.CONDUCT_INTERVIEW])
def reschedule(iv_id):
    return ok(_view(svc.reschedule(g.actor, svc.get_interview(iv_id), load(RescheduleSchema))), "Rescheduled")


@bp.post("/<int:iv_id>/student-reschedule")
@api("REQUEST_INTERVIEW_RESCHEDULE",
     roles=[Role.STUDENT])
def student_reschedule(iv_id):
    iv = svc.get_interview(iv_id)
    return ok(_view(svc.reschedule(g.actor, iv, load(RescheduleSchema), by_student=True)), "Rescheduled")


@bp.post("/<int:iv_id>/cancel")
@api("CANCEL_INTERVIEW", permissions=[Permission.CONDUCT_INTERVIEW])
def cancel(iv_id):
    return ok(_view(svc.cancel(g.actor, svc.get_interview(iv_id), load(CancelSchema)["reason"])), "Cancelled")


@bp.post("/<int:iv_id>/outcome")
@api("RECORD_INTERVIEW_OUTCOME", permissions=[Permission.CONDUCT_INTERVIEW])
def outcome(iv_id):
    data = load(OutcomeSchema)
    return ok(_view(svc.record_outcome(g.actor, svc.get_interview(iv_id), data["outcome"], data.get("remarks"))),
              "Outcome recorded")


@bp.post("/<int:iv_id>/no-show")
@api("MARK_INTERVIEW_NO_SHOW", permissions=[Permission.CONDUCT_INTERVIEW])
def no_show(iv_id):
    return ok(_view(svc.mark_no_show(g.actor, svc.get_interview(iv_id), load(NoShowSchema).get("note"))),
              "No-show recorded")


@bp.put("/<int:iv_id>/meeting-link")
@api("UPDATE_INTERVIEW_MEETING_LINK",
     permissions=[Permission.CONDUCT_INTERVIEW])
def update_meeting_link(iv_id):
    return ok(_view(svc.update_meeting_link(g.actor, svc.get_interview(iv_id), load(MeetingLinkSchema))),
              "Meeting link updated")
