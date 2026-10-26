"""Interviews (M13). Online interviews run on the college's own video platform
(Zoom, Google Meet, Teams ...): the college pastes the meeting link when scheduling."""
from datetime import timedelta
from urllib.parse import urlparse

from flask import current_app

from app.core.activity import activity
from app.core.errors import BusinessRuleViolation, Conflict, NotFound, PermissionDenied, ValidationError
from app.core.utils import IST, ensure_aware, utcnow
from app.enums import (InterviewMode, InterviewOutcome, InterviewStatus, MigrationStatus as S,
                       NotificationEvent, Permission, RecordStatus, Weekday)
from app.extensions import db
from app.models import Interview, InterviewPanelist, InterviewWorkingHours, MigrationRequest, User
from app.services.notifications import notify
from app.core.records import active, is_active

WEEKDAYS = [Weekday.MON, Weekday.TUE, Weekday.WED, Weekday.THU, Weekday.FRI, Weekday.SAT, Weekday.SUN]


def _when(dt):
    return ensure_aware(dt).astimezone(IST).strftime("%d %b %Y, %I:%M %p IST")


def _within_working_hours(college_id, start, end):
    local_start, local_end = ensure_aware(start).astimezone(IST), ensure_aware(end).astimezone(IST)
    if local_start.date() != local_end.date():
        return False
    day = WEEKDAYS[local_start.weekday()]
    for row in active(InterviewWorkingHours).filter_by(college_id=college_id, weekday=day):
        if row.start_time <= local_start.time() and local_end.time() <= row.end_time:
            return True
    return False


def _panel_conflicts(panel_ids, start, end, exclude_id=None):
    q = (db.session.query(Interview).join(InterviewPanelist, InterviewPanelist.interview_id == Interview.id)
         .filter(InterviewPanelist.user_id.in_(panel_ids), InterviewPanelist.status == RecordStatus.ACTIVE,
                 Interview.interview_status == InterviewStatus.SCHEDULED,
                 Interview.scheduled_start < end, Interview.scheduled_end > start))
    if exclude_id:
        q = q.filter(Interview.id != exclude_id)
    return q.first()


def _student_conflict(req, start, end, exclude_id=None):
    q = (active(Interview).join(MigrationRequest, MigrationRequest.id == Interview.request_id)
         .filter(MigrationRequest.student_id == req.student_id,
                 Interview.interview_status == InterviewStatus.SCHEDULED,
                 Interview.scheduled_start < end, Interview.scheduled_end > start))
    if exclude_id:
        q = q.filter(Interview.id != exclude_id)
    return q.first()


def _validate_slot(actor, req, start, end, panel_ids, extra_slot_reason, exclude_id=None):
    start, end = ensure_aware(start), ensure_aware(end)
    if end <= start:
        raise ValidationError("The interview must end after it starts")
    if start <= utcnow():
        raise ValidationError("The interview must be in the future")
    if not _within_working_hours(req.destination_college_id, start, end):
        if not extra_slot_reason:
            raise BusinessRuleViolation("This slot is outside the college's interview hours. "
                                        "A college admin can add a one-off slot with a reason.",
                                        error_code="OUTSIDE_WORKING_HOURS")
        if not (actor.is_platform_admin or actor.admin_of(req.destination_college)):
            raise PermissionDenied("Only the college admin can add a slot outside working hours")
    for uid in panel_ids:
        member = db.session.get(User, uid)
        if member is None or not any(m.college_id == req.destination_college_id
                                     and Permission.CONDUCT_INTERVIEW in _perms(member, m)
                                     for m in member.memberships):
            raise ValidationError(f"User {uid} cannot interview for this college")
    clash = _panel_conflicts(panel_ids, start, end, exclude_id)
    if clash:
        raise Conflict("An interviewer is already booked at this time", error_code="SLOT_CONFLICT",
                       details={"interview_id": clash.id})
    if _student_conflict(req, start, end, exclude_id):
        raise Conflict("The student has another interview at this time", error_code="STUDENT_SLOT_CONFLICT")
    return start, end


def _meeting_fields(mode, data, current=None):
    """Validate the link the college pasted. Required for online interviews."""
    url = (data.get("meeting_url") if "meeting_url" in data else (current.meeting_url if current else None)) or None
    info = data.get("meeting_info") if "meeting_info" in data else (current.meeting_info if current else None)
    if mode == InterviewMode.ONLINE:
        if not url:
            raise ValidationError("Paste the meeting link for an online interview",
                                  details={"fields": {"meeting_url": ["Required for online interviews"]}})
        parsed = urlparse(url.strip())
        if parsed.scheme not in current_app.config["INTERVIEW_URL_SCHEMES"] or not parsed.netloc:
            raise ValidationError("The meeting link must be a full https:// address",
                                  details={"fields": {"meeting_url": ["Enter a valid https link"]}})
        return url.strip(), (info or None)
    return None, None  # in person: no link


def _ctx(req, iv):
    return {"reference": req.reference_no, "college": req.destination_college.name,
            "when": _when(iv.scheduled_start), "mode": iv.mode.value.replace("_", " ").lower(),
            "where": iv.meeting_url if iv.mode == InterviewMode.ONLINE else (iv.location or "the college"),
            "info": iv.meeting_info or ""}


def _perms(user, membership):
    from app.core.auth import Actor
    return Actor(user).membership_permissions(membership)


def _require_scheduler(actor, req):
    if not actor.can(Permission.CONDUCT_INTERVIEW, req.destination_college):
        raise PermissionDenied("You cannot schedule interviews for this college")


def schedule(actor, req, data):
    from app.services.migration import transition
    _require_scheduler(actor, req)
    if req.request_status != S.UNDER_REVIEW:
        raise BusinessRuleViolation("Interviews are scheduled during review", error_code="NOT_UNDER_REVIEW")
    panel = data.get("panel_user_ids") or [actor.user.id]
    start, end = _validate_slot(actor, req, data["scheduled_start"], data["scheduled_end"], panel,
                                data.get("extra_slot_reason"))
    mode = InterviewMode(data.get("mode", InterviewMode.ONLINE.value))
    url, info = _meeting_fields(mode, data)
    if mode == InterviewMode.IN_PERSON and not data.get("location"):
        raise ValidationError("Give the room or address for an in-person interview",
                              details={"fields": {"location": ["Required for in-person interviews"]}})
    iv = Interview(request_id=req.id, college_id=req.destination_college_id, mode=mode, scheduled_start=start,
                   scheduled_end=end, location=data.get("location") if mode == InterviewMode.IN_PERSON else None,
                   meeting_url=url, meeting_info=info, extra_slot_reason=data.get("extra_slot_reason"))
    db.session.add(iv)
    db.session.flush()
    for uid in panel:
        db.session.add(InterviewPanelist(interview_id=iv.id, user_id=uid))
    transition(req, S.INTERVIEW_SCHEDULED, f"Interview on {_when(start)}")
    ctx = _ctx(req, iv)
    notify(req.student.user_id, NotificationEvent.INTERVIEW_SCHEDULED, ctx)
    for uid in panel:
        notify(uid, NotificationEvent.INTERVIEW_SCHEDULED, ctx)
    activity.step("Interview scheduled", entity="interview", entity_id=iv.id)
    db.session.commit()
    return iv


def reschedule(actor, iv, data, by_student=False):
    req = db.session.get(MigrationRequest, iv.request_id)
    if iv.interview_status != InterviewStatus.SCHEDULED:
        raise BusinessRuleViolation("Only scheduled interviews can be moved")
    cfg = current_app.config
    if by_student:
        if req.student.user_id != actor.user.id:
            raise PermissionDenied("Not your interview")
        if req.student_reschedules >= cfg["MAX_STUDENT_RESCHEDULES"]:
            raise BusinessRuleViolation("You have already rescheduled once", error_code="RESCHEDULE_LIMIT")
        if ensure_aware(iv.scheduled_start) - utcnow() < timedelta(hours=cfg["INTERVIEW_RESCHEDULE_MIN_HOURS"]):
            raise BusinessRuleViolation(f"Reschedule at least {cfg['INTERVIEW_RESCHEDULE_MIN_HOURS']} hours ahead",
                                        error_code="TOO_LATE_TO_RESCHEDULE")
        req.student_reschedules += 1
    else:
        _require_scheduler(actor, req)
    panel = [p.user_id for p in iv.panel if is_active(p)]
    start, end = _validate_slot(actor if not by_student else _college_proxy(actor), req, data["scheduled_start"],
                                data["scheduled_end"], panel, data.get("extra_slot_reason"), exclude_id=iv.id)
    # Students can only move the time; the mode and link stay as the college set them.
    mode = iv.mode if by_student else InterviewMode(data.get("mode", iv.mode.value))
    url, info = _meeting_fields(mode, {} if by_student else data, current=iv)
    location = iv.location if by_student else data.get("location", iv.location)
    iv.interview_status = InterviewStatus.RESCHEDULED
    new = Interview(request_id=req.id, college_id=iv.college_id, mode=mode, scheduled_start=start, scheduled_end=end,
                    location=location if mode == InterviewMode.IN_PERSON else None, meeting_url=url, meeting_info=info,
                    rescheduled_from_id=iv.id, requested_by_student=by_student,
                    extra_slot_reason=data.get("extra_slot_reason"))
    db.session.add(new)
    db.session.flush()
    for uid in panel:
        db.session.add(InterviewPanelist(interview_id=new.id, user_id=uid))
    ctx = _ctx(req, new)
    notify(req.student.user_id, NotificationEvent.INTERVIEW_RESCHEDULED, ctx)
    for uid in panel:
        notify(uid, NotificationEvent.INTERVIEW_RESCHEDULED, ctx)
    activity.step("Interview rescheduled", entity="interview", entity_id=new.id, previous=iv.id,
                  by_student=by_student)
    db.session.commit()
    return new


class _college_proxy:
    """Student reschedules are validated like a college user without extra-slot rights."""

    def __init__(self, actor):
        self.actor = actor
        self.is_platform_admin = False
        self.user = actor.user

    def admin_of(self, _):
        return False


def cancel(actor, iv, reason):
    from app.services.migration import transition
    req = db.session.get(MigrationRequest, iv.request_id)
    _require_scheduler(actor, req)
    if iv.interview_status != InterviewStatus.SCHEDULED:
        raise BusinessRuleViolation("Only scheduled interviews can be cancelled")
    if not reason:
        raise ValidationError("A reason is required")
    iv.interview_status = InterviewStatus.CANCELLED
    iv.cancellation_reason = reason
    if req.request_status == S.INTERVIEW_SCHEDULED:
        transition(req, S.UNDER_REVIEW, f"Interview cancelled: {reason}")
    ctx = {"reference": req.reference_no, "when": _when(iv.scheduled_start), "note": reason}
    notify(req.student.user_id, NotificationEvent.INTERVIEW_CANCELLED, ctx)
    for p in iv.panel:
        notify(p.user_id, NotificationEvent.INTERVIEW_CANCELLED, ctx)
    db.session.commit()
    return iv


def record_outcome(actor, iv, outcome, remarks=None):
    from app.services.migration import _reject, transition
    req = db.session.get(MigrationRequest, iv.request_id)
    if not actor.can(Permission.CONDUCT_INTERVIEW, req.destination_college):
        raise PermissionDenied("You cannot record outcomes for this college")
    if iv.interview_status != InterviewStatus.SCHEDULED:
        raise BusinessRuleViolation("The outcome was already recorded or the interview was cancelled")
    if ensure_aware(iv.scheduled_start) > utcnow():
        raise BusinessRuleViolation("The interview has not started yet", error_code="INTERVIEW_NOT_STARTED")
    outcome = InterviewOutcome(outcome)
    iv.interview_status = InterviewStatus.COMPLETED
    iv.outcome = outcome
    iv.remarks = remarks
    iv.outcome_recorded_by_id = actor.user.id
    iv.outcome_recorded_at = utcnow()
    activity.step("Interview outcome recorded", entity="interview", entity_id=iv.id, outcome=outcome)
    if outcome == InterviewOutcome.PASSED:
        transition(req, S.UNDER_REVIEW, "Interview passed")
    else:
        _reject(req, f"Not selected after interview. {remarks or ''}".strip(), actor)
    db.session.commit()
    return iv


def mark_no_show(actor, iv, note=None):
    from app.services.migration import _reject, transition
    req = db.session.get(MigrationRequest, iv.request_id)
    if not actor.can(Permission.CONDUCT_INTERVIEW, req.destination_college):
        raise PermissionDenied("You cannot record attendance for this college")
    if iv.interview_status != InterviewStatus.SCHEDULED:
        raise BusinessRuleViolation("Only scheduled interviews can be marked as no-show")
    if ensure_aware(iv.scheduled_end) > utcnow():
        raise BusinessRuleViolation("Wait until the slot has ended", error_code="INTERVIEW_NOT_OVER")
    iv.interview_status = InterviewStatus.NO_SHOW
    req.no_show_count += 1
    limit = current_app.config["MAX_NO_SHOWS"]
    activity.step("No-show recorded", entity="interview", entity_id=iv.id, count=req.no_show_count)
    if req.no_show_count >= limit:
        _reject(req, "Did not attend the interview", actor)
        message = "The request was closed because the interview was missed twice."
    else:
        transition(req, S.UNDER_REVIEW, "No-show; a new slot can be scheduled")
        message = "The college may offer you one more slot."
    notify(req.student.user_id, NotificationEvent.NO_SHOW_RECORDED,
           {"reference": req.reference_no, "note": note or message})
    db.session.commit()
    return iv


# ---------------------------------------------------------- meeting link
def update_meeting_link(actor, iv, data):
    """The college changes the link or meeting details of a scheduled online interview."""
    req = db.session.get(MigrationRequest, iv.request_id)
    _require_scheduler(actor, req)
    if iv.interview_status != InterviewStatus.SCHEDULED:
        raise BusinessRuleViolation("Only scheduled interviews can be changed")
    if iv.mode != InterviewMode.ONLINE:
        raise BusinessRuleViolation("This is an in-person interview; reschedule it to make it online",
                                    error_code="NOT_ONLINE")
    old = iv.meeting_url
    iv.meeting_url, iv.meeting_info = _meeting_fields(iv.mode, data, current=iv)
    activity.change("interview", iv.id, "meeting_url", old, iv.meeting_url)
    activity.step("Meeting link updated", entity="interview", entity_id=iv.id)
    ctx = _ctx(req, iv)
    notify(req.student.user_id, NotificationEvent.INTERVIEW_LINK_UPDATED, ctx)
    for p in iv.panel:
        if is_active(p):
            notify(p.user_id, NotificationEvent.INTERVIEW_LINK_UPDATED, ctx)
    db.session.commit()
    return iv


def visible_interviews(actor):
    q = active(Interview)
    if actor.is_platform_admin:
        return q
    if actor.is_student:
        from app.models import StudentProfile
        profile = StudentProfile.query.filter_by(user_id=actor.user.id).first()
        return q.join(MigrationRequest, MigrationRequest.id == Interview.request_id).filter(
            MigrationRequest.student_id == (profile.id if profile else -1))
    from app.models import College
    ids = list(actor.college_ids())
    universities = list(actor.university_ids())
    if universities:
        ids += [cid for (cid,) in db.session.query(College.id).filter(College.university_id.in_(universities))]
    return q.filter(Interview.college_id.in_(ids or [-1]))


def get_interview(iv_id):
    iv = db.session.get(Interview, iv_id)
    if iv is None or not is_active(iv):
        raise NotFound("Interview not found")
    return iv
