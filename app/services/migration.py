"""The migration request workflow (6, 9, 12.1).

Every status change goes through ``transition`` which validates the move
against ALLOWED, writes the status history row, records the change on the
request's activity row and keeps ``closed_at`` for final statuses.
Decisions that compete (approve vs reject) pass the client's ``version`` so a
stale screen gets "request changed, reload" (INT-13).
"""
from flask import current_app

from app.core.activity import activity
from app.core.errors import (BusinessRuleViolation, Conflict, InvalidTransition, NotFound,
                             PermissionDenied, ValidationError)
from app.core.utils import deadline_after_days, ensure_aware, reference_number, today_ist, utcnow
from app.enums import (AppealDecision, CancellationStage, ConversionSource, CreditDecision,
                       DocumentOwnerType, DocumentType, EligibilityResult, EnrolmentStatus,
                       InterviewOutcome, InterviewStatus, LetterType, MigrationStatus as S,
                       MigrationType, NocRefusalReason, NotificationEvent, OfferMode, Permission,
                       ReservationCategory, VerificationStatus)
from app.extensions import db
from app.models import (AcademicYear, Appeal, CourseOffering, CreditAssessment, Document,
                        EquivalenceDecision, GradeConversion, Interview, MigrationRequest,
                        MigrationStatusHistory, MigrationWindow, StudentProfile)
from app.services import eligibility, seats
from app.services.directory import has_active_admin, platform_admins, responsible_users
from app.services.documents import store_upload
from app.services.letters import cancel_letters, issue_letter
from app.services.notifications import notify
from app.services.students import get_profile, is_minor, missing_profile_fields
from app.core.records import active, is_active, is_operational, student_name, vacancy_available

ALLOWED = {
    S.DRAFT: {S.SUBMITTED, S.WITHDRAWN},
    S.SUBMITTED: {S.NOC_GRANTED, S.REJECTED, S.WITHDRAWN},
    S.NOC_GRANTED: {S.UNDER_REVIEW, S.REJECTED, S.WITHDRAWN},
    S.UNDER_REVIEW: {S.RETURNED_FOR_CORRECTION, S.INTERVIEW_SCHEDULED, S.RECOMMENDED, S.REJECTED, S.WITHDRAWN},
    S.RETURNED_FOR_CORRECTION: {S.UNDER_REVIEW, S.WITHDRAWN, S.REJECTED},
    S.INTERVIEW_SCHEDULED: {S.UNDER_REVIEW, S.REJECTED, S.WITHDRAWN},
    S.RECOMMENDED: {S.ENTRY_APPROVED, S.WAITLISTED, S.PROVISIONAL_OFFER, S.REJECTED, S.WITHDRAWN},
    S.ENTRY_APPROVED: {S.PROVISIONAL_OFFER, S.WAITLISTED, S.WITHDRAWN, S.REJECTED},
    S.WAITLISTED: {S.PROVISIONAL_OFFER, S.WITHDRAWN, S.CANCELLED},
    S.PROVISIONAL_OFFER: {S.OFFER_ACCEPTED, S.OFFER_DECLINED, S.OFFER_EXPIRED, S.WITHDRAWN},
    S.OFFER_ACCEPTED: {S.RELEASED, S.CANCELLED},
    S.RELEASED: {S.ADMITTED_ORIGINALS_PENDING, S.CANCELLED},
    S.ADMITTED_ORIGINALS_PENDING: {S.ADMITTED, S.CANCELLED},
    S.ADMITTED: {S.CANCELLED},
    S.REJECTED: {S.APPEALED},
    S.APPEALED: {S.NOC_GRANTED, S.UNDER_REVIEW, S.RECOMMENDED, S.REJECTED},
}
FINAL = {S.ADMITTED, S.REJECTED, S.WITHDRAWN, S.OFFER_DECLINED, S.OFFER_EXPIRED, S.CANCELLED}
ACTIVE = set(ALLOWED) - {S.ADMITTED, S.REJECTED} | {S.APPEALED}
COUNTS_TOWARDS_LIMIT = ACTIVE - {S.DRAFT}

BASE_REQUIRED_DOCS = [DocumentType.PHOTO, DocumentType.ID_PROOF, DocumentType.CLASS_10_MARKSHEET,
                      DocumentType.CLASS_12_MARKSHEET, DocumentType.ENROLMENT_PROOF,
                      DocumentType.SEMESTER_MARKSHEET, DocumentType.ABC_CREDIT_STATEMENT]
STUDENT_UPLOADABLE = set(BASE_REQUIRED_DOCS) | {DocumentType.CONVERSION_CERTIFICATE,
                                                DocumentType.CATEGORY_CERTIFICATE, DocumentType.GAP_CERTIFICATE,
                                                DocumentType.ANTI_RAGGING_AFFIDAVIT, DocumentType.OTHER}


# --------------------------------------------------------------- core
def transition(req, to_status, reason=None, actor_id=None):
    from flask import g
    current = req.request_status
    if to_status not in ALLOWED.get(current, set()):
        raise InvalidTransition(f"A request in {current.value} cannot move to {to_status.value}",
                                details={"from": current.value, "to": to_status.value})
    actor_id = actor_id or (g.current_user.id if getattr(g, "current_user", None) else None)
    req.request_status = to_status
    if to_status in FINAL:
        req.closed_at = utcnow()
    db.session.add(MigrationStatusHistory(request_id=req.id, from_status=current, to_status=to_status,
                                          actor_id=actor_id, reason=reason))
    activity.change("migration_request", req.id, "request_status", current, to_status)
    activity.step(f"Status {current.value} -> {to_status.value}", entity="migration_request", entity_id=req.id)


def _check_version(req, version):
    if version is not None and int(version) != req.version:
        raise Conflict("This request was changed by someone else. Reload and try again.",
                       error_code="CONCURRENT_UPDATE", details={"current_version": req.version})


def get_request(req_id):
    req = db.session.get(MigrationRequest, req_id)
    if req is None or not is_active(req):
        raise NotFound("Migration request not found")
    activity.set_entity("migration_request", req.id)
    return req


def _student_owns(user, req):
    if req.student.user_id != user.id:
        raise PermissionDenied("This is not your request")


def can_view(actor, req):
    if actor.is_platform_admin or req.student.user_id == actor.user.id:
        return True
    colleges, universities = actor.college_ids(), actor.university_ids()
    if req.source_college_id in colleges or req.destination_college_id in colleges:
        return True
    return bool({req.source_university_id, req.destination_university_id} & universities)


def require_view(actor, req):
    if not can_view(actor, req):
        raise PermissionDenied("This request is outside your institution")


def _ctx(req, **extra):
    return {"reference": req.reference_no, "student": student_name(req.student),
            "destination": req.destination_college.name, "source": req.source_college.name, **extra}


def _notify_holders(inst, permission, event, ctx):
    """``inst``: a University or College row."""
    for u in responsible_users(inst, permission):
        notify(u, event, ctx)


# ----------------------------------------------------------- student side
def create_request(user, data):
    profile = get_profile(user)
    offering = db.session.get(CourseOffering, data["offering_id"])
    if offering is None or not is_active(offering) or not is_active(offering.course):
        raise NotFound("This course offering is not available", error_code="OFFERING_UNAVAILABLE")
    dest = offering.college
    if not is_operational(dest):
        raise BusinessRuleViolation("The destination college is not accepting requests", error_code="COLLEGE_NOT_ACTIVE")
    if profile.current_college_id is None:
        raise BusinessRuleViolation("Add your current college first", error_code="ENROLMENT_REQUIRED")
    if profile.current_college_id == dest.id:
        raise ValidationError("You are already at this college")
    course = offering.course
    year = db.session.get(AcademicYear, data["academic_year_id"]) if data.get("academic_year_id") else None
    window_q = active(MigrationWindow).filter_by(course_id=course.id)
    window = (window_q.filter_by(academic_year_id=year.id).first() if year
              else window_q.filter(MigrationWindow.closes_on >= today_ist()).order_by(MigrationWindow.opens_on).first())
    if window is None:
        raise BusinessRuleViolation("No migration window is set for this course", error_code="NO_WINDOW")
    year = window.academic_year
    entry = int(data["entry_semester"])
    if entry not in (window.entry_semesters or []):
        raise ValidationError(f"Entry is open only into semesters {window.entry_semesters}",
                              error_code="ENTRY_SEMESTER_CLOSED")
    previous = active(MigrationRequest).filter(MigrationRequest.student_id == profile.id,
                                                MigrationRequest.destination_offering_id == offering.id,
                                                MigrationRequest.academic_year_id == year.id).all()
    for p in previous:
        if p.request_status == S.DRAFT:
            raise Conflict("You already have a draft for this course", error_code="DUPLICATE_REQUEST",
                           details={"request_id": p.id})
        if p.submitted_at is not None:
            raise Conflict("You have already applied to this course this academic year. "
                           "You can apply again next academic year.", error_code="DUPLICATE_REQUEST",
                           details={"request_id": p.id})
    source = profile.current_college
    source_univ = source.university_id  # None for a college without a university
    dest_univ = dest.university_id
    req = MigrationRequest(reference_no=reference_number(), student_id=profile.id,
                           source_college_id=source.id, source_university_id=source_univ,
                           destination_offering_id=offering.id, destination_college_id=dest.id,
                           destination_university_id=dest_univ, academic_year_id=year.id, window_id=window.id,
                           entry_semester=entry,
                           migration_type=(MigrationType.SAME_UNIVERSITY if source_univ and source_univ == dest_univ
                                           else MigrationType.CROSS_UNIVERSITY),
                           request_status=S.DRAFT, category=profile.category,
                           interview_required=course.interview_required)
    db.session.add(req)
    db.session.flush()
    db.session.add(MigrationStatusHistory(request_id=req.id, from_status=None, to_status=S.DRAFT, actor_id=user.id,
                                          reason="Draft created"))
    activity.step("Draft created", entity="migration_request", entity_id=req.id, reference=req.reference_no,
                  migration_type=req.migration_type)
    db.session.commit()
    return req


def update_draft(user, req, data):
    _student_owns(user, req)
    if req.request_status not in (S.DRAFT,):
        raise BusinessRuleViolation("Only drafts can change course or semester", error_code="NOT_DRAFT")
    if "entry_semester" in data:
        if int(data["entry_semester"]) not in (req.window.entry_semesters or []):
            raise ValidationError("That entry semester is not open", error_code="ENTRY_SEMESTER_CLOSED")
        activity.change("migration_request", req.id, "entry_semester", req.entry_semester, data["entry_semester"])
        req.entry_semester = int(data["entry_semester"])
    db.session.commit()
    return req


def _editable_by_student(req):
    return req.request_status in (S.DRAFT, S.RETURNED_FOR_CORRECTION)


def upload_document(user, req, file_storage, doc_type, replace_document_id=None):
    _student_owns(user, req)
    doc_type = DocumentType(doc_type)
    if doc_type not in STUDENT_UPLOADABLE:
        raise ValidationError("Students cannot upload this document type")
    if not _editable_by_student(req):
        raise BusinessRuleViolation("Documents can be changed only in a draft or when corrections are requested",
                                    error_code="REQUEST_LOCKED")
    replace = None
    if replace_document_id:
        replace = db.session.get(Document, replace_document_id)
        if replace is None or replace.owner_id not in (req.id, user.id):
            raise ValidationError("The document to replace is not part of this request")
    owner_type, owner_id = (DocumentOwnerType.MIGRATION_REQUEST, req.id)
    doc = store_upload(file_storage, owner_type, owner_id, doc_type, replace_document=replace)
    db.session.commit()
    return doc


def _current_docs(req):
    user_id = req.student.user_id
    return (active(Document).filter(Document.is_current.is_(True))
            .filter(db.or_(db.and_(Document.owner_type == DocumentOwnerType.USER, Document.owner_id == user_id),
                           db.and_(Document.owner_type == DocumentOwnerType.MIGRATION_REQUEST,
                                   Document.owner_id == req.id))).all())


def checklist(req):
    """Required documents for this request with upload and verification state (7.1)."""
    required = list(BASE_REQUIRED_DOCS)
    if req.category != ReservationCategory.OPEN:
        required.append(DocumentType.CATEGORY_CERTIFICATE)
    conv = GradeConversion.query.filter_by(request_id=req.id).first()
    if conv is not None and conv.conversion_source == ConversionSource.CERTIFICATE:
        required.append(DocumentType.CONVERSION_CERTIFICATE)
    docs = _current_docs(req)
    by_type = {}
    for d in docs:
        by_type.setdefault(d.doc_type, []).append(d)
    items = []
    for t in required:
        found = by_type.get(t, [])
        rejected = [d for d in found if d.verification_status in (VerificationStatus.REJECTED,
                                                                  VerificationStatus.CORRECTION_REQUESTED)]
        items.append({
            "doc_type": t.value, "required": True, "uploaded": bool(found),
            "verified": bool(found) and all(d.verification_status == VerificationStatus.VERIFIED for d in found),
            "needs_attention": bool(rejected),
            "documents": [{"id": d.id, "filename": d.original_filename,
                           "verification_status": d.verification_status.value,
                           "rejection_reason": d.rejection_reason, "version_no": d.version_no} for d in found],
        })
    optional = [{"doc_type": t.value, "required": False, "uploaded": True,
                 "documents": [{"id": d.id, "filename": d.original_filename,
                                "verification_status": d.verification_status.value} for d in ds]}
                for t, ds in by_type.items() if t not in required]
    return items + optional


def set_conversion(user, req, certificate_file=None, certified_percentage=None):
    _student_owns(user, req)
    if not _editable_by_student(req):
        raise BusinessRuleViolation("Grades can change only in a draft or when corrections are requested",
                                    error_code="REQUEST_LOCKED")
    cert_doc = None
    if certificate_file is not None:
        cert_doc = store_upload(certificate_file, DocumentOwnerType.MIGRATION_REQUEST, req.id,
                                DocumentType.CONVERSION_CERTIFICATE)
    elif certified_percentage is not None:
        cert_doc = (active(Document).filter_by(owner_type=DocumentOwnerType.MIGRATION_REQUEST, owner_id=req.id,
                                                doc_type=DocumentType.CONVERSION_CERTIFICATE, is_current=True).first())
    conv = eligibility.set_conversion(req, cert_doc, certified_percentage)
    db.session.commit()
    return conv


def _submission_problems(req):
    profile = req.student
    problems = []
    if profile.enrolment_status != EnrolmentStatus.VERIFIED:
        problems.append("Your current college has not verified your enrolment yet")
    for f in missing_profile_fields(profile):
        problems.append(f"Profile field missing: {f}")
    if not profile.abc_account_id:
        problems.append("ABC account ID is required before submitting")
    if not req.window or not (req.window.opens_on <= today_ist() <= req.window.closes_on):
        problems.append("The migration window for this course is not open")
    vacancy = seats._vacancy_for(req)
    if vacancy is None or vacancy_available(vacancy) <= 0:
        problems.append("There is no available seat for this course and semester")
    if not GradeConversion.query.filter_by(request_id=req.id).first():
        problems.append("Grade conversion is missing")
    active_count = MigrationRequest.query.filter(MigrationRequest.student_id == profile.id,
                                                 MigrationRequest.id != req.id,
                                                 MigrationRequest.request_status.in_(list(COUNTS_TOWARDS_LIMIT))).count()
    limit = current_app.config["MAX_ACTIVE_REQUESTS_PER_STUDENT"]
    if active_count >= limit:
        problems.append(f"You already have {limit} active requests")
    accepted = MigrationRequest.query.filter(MigrationRequest.student_id == profile.id,
                                             MigrationRequest.request_status.in_(
                                                 [S.OFFER_ACCEPTED, S.RELEASED, S.ADMITTED_ORIGINALS_PENDING])).first()
    if accepted:
        problems.append("You have already accepted an offer")
    for item in checklist(req):
        if item["required"] and not item["uploaded"]:
            problems.append(f"Document missing: {item['doc_type']}")
    if is_minor(profile) and not profile.parental_consent_at:
        problems.append("Parental consent is required")
    return problems


def submit(user, req):
    _student_owns(user, req)
    if req.request_status != S.DRAFT:
        raise BusinessRuleViolation("Only drafts can be submitted", error_code="NOT_DRAFT")
    problems = _submission_problems(req)
    if problems:
        raise BusinessRuleViolation("The request is not complete", error_code="REQUEST_INCOMPLETE",
                                    details={"problems": problems})
    req.submitted_at = utcnow()
    req.noc_due_at = deadline_after_days(current_app.config["NOC_DECISION_DAYS"])
    transition(req, S.SUBMITTED, "Submitted by student", user.id)
    eligibility.run_check(req)
    db.session.flush()
    notify(user, NotificationEvent.REQUEST_SUBMITTED, _ctx(req))
    _notify_holders(req.source_college, Permission.DECIDE_NOC, NotificationEvent.NOC_REQUESTED,
                    _ctx(req, due=req.noc_due_at.date().isoformat()))
    activity.set_institution(req.source_college)
    db.session.commit()
    return req


def resubmit(user, req):
    _student_owns(user, req)
    if req.request_status != S.RETURNED_FOR_CORRECTION:
        raise BusinessRuleViolation("Only requests returned for correction can be resubmitted")
    missing = [i["doc_type"] for i in checklist(req) if i["required"] and not i["uploaded"]]
    if missing:
        raise BusinessRuleViolation("Documents still missing", error_code="REQUEST_INCOMPLETE",
                                    details={"problems": missing})
    transition(req, S.UNDER_REVIEW, "Corrections resubmitted", user.id)
    _sync_credit_rows(req)
    eligibility.run_check(req)
    _notify_holders(req.destination_college, Permission.REVIEW_REQUEST, NotificationEvent.GENERIC,
                    {"subject": f"Corrections received on {req.reference_no}",
                     "message": f"{student_name(req.student)} has resubmitted {req.reference_no}."})
    db.session.commit()
    return req


WITHDRAWABLE = {S.DRAFT, S.SUBMITTED, S.NOC_GRANTED, S.UNDER_REVIEW, S.RETURNED_FOR_CORRECTION,
                S.INTERVIEW_SCHEDULED, S.RECOMMENDED, S.ENTRY_APPROVED, S.WAITLISTED, S.PROVISIONAL_OFFER}


def withdraw(user, req, reason=None):
    _student_owns(user, req)
    if req.request_status not in WITHDRAWABLE:
        raise BusinessRuleViolation("After accepting an offer, ask the destination college to cancel instead",
                                    error_code="USE_CANCELLATION")
    _withdraw(req, reason or "Withdrawn by student", user.id)
    db.session.commit()
    return req


def _withdraw(req, reason, actor_id):
    had_seat = req.seat_state is not None
    transition(req, S.WITHDRAWN, reason, actor_id)
    if had_seat:
        seats.release_seat(req, transition)
    else:
        seats.recompute_waitlist(req)
    cancel_letters(req, [LetterType.PROVISIONAL_OFFER])
    _cancel_open_interviews(req, "Request withdrawn")


def accept_offer(user, req):
    _student_owns(user, req)
    if req.request_status != S.PROVISIONAL_OFFER:
        raise BusinessRuleViolation("There is no open offer on this request", error_code="NO_OFFER")
    if ensure_aware(req.offer_expires_at) < utcnow():
        raise BusinessRuleViolation("This offer has expired", error_code="OFFER_EXPIRED")
    req.accepted_at = utcnow()
    req.release_due_at = deadline_after_days(current_app.config["RELEASE_DEADLINE_DAYS"])
    transition(req, S.OFFER_ACCEPTED, "Offer accepted by student", user.id)
    others = MigrationRequest.query.filter(MigrationRequest.student_id == req.student_id,
                                           MigrationRequest.id != req.id,
                                           MigrationRequest.request_status.in_(list(WITHDRAWABLE))).all()
    for other in others:
        _withdraw(other, f"Withdrawn automatically: offer accepted on {req.reference_no}", user.id)
    ctx = _ctx(req, due=req.release_due_at.date().isoformat())
    _notify_holders(req.source_college, Permission.ISSUE_RELEASE, NotificationEvent.OFFER_ACCEPTED, ctx)
    if req.migration_type == MigrationType.CROSS_UNIVERSITY and req.source_university_id:
        _notify_holders(req.source_university, Permission.ISSUE_RELEASE, NotificationEvent.RELEASE_REQUESTED, ctx)
    activity.step("Other active requests withdrawn", count=len(others))
    db.session.commit()
    return req


def decline_offer(user, req):
    _student_owns(user, req)
    if req.request_status != S.PROVISIONAL_OFFER:
        raise BusinessRuleViolation("There is no open offer on this request", error_code="NO_OFFER")
    transition(req, S.OFFER_DECLINED, "Offer declined by student", user.id)
    seats.release_seat(req, transition)
    cancel_letters(req, [LetterType.PROVISIONAL_OFFER])
    db.session.commit()
    return req


def request_cancellation(user, req, reason):
    _student_owns(user, req)
    if req.request_status != S.OFFER_ACCEPTED:
        raise BusinessRuleViolation("Cancellation requests apply between accepting an offer and the release")
    req.cancellation_requested_at = utcnow()
    req.cancellation_reason = reason
    _notify_holders(req.destination_college, Permission.APPROVE_CANCELLATION, NotificationEvent.GENERIC,
                    {"subject": f"Cancellation requested on {req.reference_no}",
                     "message": f"{student_name(req.student)} asked to cancel: {reason}"})
    activity.step("Cancellation requested", entity="migration_request", entity_id=req.id)
    db.session.commit()
    return req


def file_appeal(user, req, reason, evidence_file=None):
    _student_owns(user, req)
    if req.request_status != S.REJECTED or req.rejected_at_status is None:
        raise BusinessRuleViolation("Only a rejected request can be appealed", error_code="NOT_REJECTED")
    if Appeal.query.filter_by(request_id=req.id).first():
        raise BusinessRuleViolation("This request has already been appealed once", error_code="ALREADY_APPEALED")
    days = current_app.config["APPEAL_WINDOW_DAYS"]
    if utcnow() > deadline_after_days(days, req.rejected_at):
        raise BusinessRuleViolation(f"Appeals must be filed within {days} days of rejection",
                                    error_code="APPEAL_WINDOW_CLOSED")
    appeal = Appeal(request_id=req.id, reason=reason, rejected_at_status=req.rejected_at_status,
                    due_at=deadline_after_days(current_app.config["APPEAL_DECISION_DAYS"]))
    db.session.add(appeal)
    db.session.flush()
    if evidence_file is not None:
        doc = store_upload(evidence_file, DocumentOwnerType.APPEAL, appeal.id, DocumentType.APPEAL_EVIDENCE)
        appeal.evidence_document_id = doc.id
    transition(req, S.APPEALED, "Appeal filed", user.id)
    req.closed_at = None
    decider = _appeal_decider_institution(req)
    if decider is not None:
        _notify_holders(decider, Permission.DECIDE_APPEAL, NotificationEvent.APPEAL_FILED,
                        _ctx(req, due=appeal.due_at.date().isoformat()))
    else:
        for pa in platform_admins():
            notify(pa, NotificationEvent.APPEAL_FILED, _ctx(req, due=appeal.due_at.date().isoformat()))
    activity.step("Appeal filed", entity="appeal", entity_id=appeal.id)
    db.session.commit()
    return appeal


def _appeal_decider_institution(req):
    """NOC refusals go to the source university; destination rejections to the destination university."""
    if req.rejected_at_status == S.SUBMITTED:
        return req.source_university
    return req.destination_university


# --------------------------------------------------------------- source side
def _require_source(actor, req, permission, allow_university_fallback=True):
    college = req.source_college
    for m in actor.memberships:
        if m.college_id == college.id and permission in actor.membership_permissions(m):
            return
    if actor.is_platform_admin:
        return
    fallback = (req.noc_escalated_at is not None or not is_operational(college)
                or not has_active_admin(college))
    if allow_university_fallback and fallback and req.source_university and actor.can(permission, req.source_university):
        return
    raise PermissionDenied("You cannot act for the student's current college")


def decide_noc(actor, req, approve, refusal_reason=None, note=None, version=None):
    _check_version(req, version)
    if req.request_status != S.SUBMITTED:
        raise BusinessRuleViolation("The NOC is not pending on this request", error_code="NOC_NOT_PENDING")
    _require_source(actor, req, Permission.DECIDE_NOC)
    req.noc_decided_at = utcnow()
    if approve:
        transition(req, S.NOC_GRANTED, note or "NOC granted")
        issue_letter(req, LetterType.NOC, actor.user)
        _notify_holders(req.destination_college, Permission.REVIEW_REQUEST, NotificationEvent.GENERIC,
                        {"subject": f"New migration request {req.reference_no}",
                         "message": f"{student_name(req.student)} has applied to your college. The NOC is granted."})
        decision = "granted"
    else:
        if not refusal_reason:
            raise ValidationError("Choose one of the listed reasons to refuse the NOC",
                                  details={"allowed": NocRefusalReason.values()})
        req.noc_refusal_reason = NocRefusalReason(refusal_reason)
        _reject(req, f"NOC refused: {req.noc_refusal_reason.value}. {note or ''}".strip(), actor)
        decision = "refused"
    notify(req.student.user_id, NotificationEvent.NOC_DECIDED, _ctx(req, decision=decision, note=note or ""))
    activity.set_institution(req.source_college)
    db.session.commit()
    return req


def upload_transfer_certificate(actor, req, file_storage):
    if req.request_status != S.OFFER_ACCEPTED:
        raise BusinessRuleViolation("The TC is issued after the student accepts an offer", error_code="NOT_AWAITING_RELEASE")
    _require_source(actor, req, Permission.ISSUE_RELEASE)
    doc = store_upload(file_storage, DocumentOwnerType.MIGRATION_REQUEST, req.id, DocumentType.TRANSFER_CERTIFICATE,
                       institution=req.source_college)
    doc.verification_status = VerificationStatus.VERIFIED
    req.tc_document_id = doc.id
    req.tc_uploaded_at = utcnow()
    _maybe_release(req, actor)
    db.session.commit()
    return req


def university_release(actor, req, file_storage):
    if req.migration_type != MigrationType.CROSS_UNIVERSITY:
        raise BusinessRuleViolation("A migration certificate is needed only between universities")
    if req.request_status != S.OFFER_ACCEPTED:
        raise BusinessRuleViolation("The release is issued after the student accepts an offer",
                                    error_code="NOT_AWAITING_RELEASE")
    if not req.source_university_id or not (actor.is_platform_admin or
                                            actor.can(Permission.ISSUE_RELEASE, req.source_university)):
        raise PermissionDenied("Only the source university can issue the migration certificate")
    doc = store_upload(file_storage, DocumentOwnerType.MIGRATION_REQUEST, req.id, DocumentType.MIGRATION_CERTIFICATE,
                       institution=req.source_university)
    doc.verification_status = VerificationStatus.VERIFIED
    req.migration_certificate_document_id = doc.id
    req.source_university_released_at = utcnow()
    _maybe_release(req, actor)
    db.session.commit()
    return req


def _maybe_release(req, actor):
    needs_university = req.migration_type == MigrationType.CROSS_UNIVERSITY and req.source_university_id
    if req.tc_uploaded_at and (not needs_university or req.source_university_released_at):
        req.released_at = utcnow()
        transition(req, S.RELEASED, "Released by the current institution")
        issue_letter(req, LetterType.RELEASE_CONFIRMATION, actor.user)
        _notify_holders(req.destination_college, Permission.CONFIRM_ADMISSION, NotificationEvent.GENERIC,
                        {"subject": f"{req.reference_no} released", "message":
                         f"{student_name(req.student)} has been released. Please confirm the admission."})
    else:
        activity.step("Release partly complete", tc=bool(req.tc_uploaded_at),
                      university=bool(req.source_university_released_at))


# ----------------------------------------------------------- destination side
def _require_dest_college(actor, req, permission):
    if not actor.can(permission, req.destination_college):
        raise PermissionDenied("You cannot act for the destination college")


def _require_dest_university(actor, req, permission):
    target = req.destination_university or req.destination_college
    if not actor.can(permission, target):
        raise PermissionDenied("Only the destination university can do this")


def start_review(actor, req):
    _require_dest_college(actor, req, Permission.REVIEW_REQUEST)
    if req.request_status != S.NOC_GRANTED:
        raise BusinessRuleViolation("Review starts after the NOC is granted", error_code="NOT_READY_FOR_REVIEW")
    transition(req, S.UNDER_REVIEW, "Review started")
    _sync_credit_rows(req)
    eligibility.run_check(req)
    activity.set_institution(req.destination_college)
    db.session.commit()
    return req


def _sync_credit_rows(req):
    """One row per subject the student passed, mapped to the destination syllabus when possible."""
    from app.models import AcademicRecord
    from app.services.academics import syllabus_for_batch
    from app.core.utils import normalize_name
    existing = {c.subject_code for c in active(CreditAssessment).filter_by(request_id=req.id)}
    course = req.destination_offering.course
    syllabus = syllabus_for_batch(course.id, req.student.batch_year or today_ist().year)
    dest_subjects = {}
    if syllabus:
        for s in syllabus.subjects:
            dest_subjects[s.code] = s
            dest_subjects[normalize_name(s.name)] = s
    added = 0
    for rec in active(AcademicRecord).filter_by(student_id=req.student_id, is_current=True):
        for sub in rec.subjects:
            if sub.subject_code in existing or not sub.passed:
                continue
            match = dest_subjects.get(sub.subject_code) or dest_subjects.get(normalize_name(sub.subject_name))
            db.session.add(CreditAssessment(request_id=req.id, semester=rec.semester, subject_code=sub.subject_code,
                                            subject_name=sub.subject_name, credits=sub.credits, grade=sub.grade,
                                            mapped_subject_code=match.code if match else None))
            existing.add(sub.subject_code)
            added += 1
    activity.step("Credit assessment rows prepared", added=added, syllabus_found=bool(syllabus))


def return_for_correction(actor, req, note, version=None):
    _check_version(req, version)
    _require_dest_college(actor, req, Permission.REVIEW_REQUEST)
    if not note:
        raise ValidationError("Say what needs correcting")
    req.correction_note = note
    transition(req, S.RETURNED_FOR_CORRECTION, note)
    notify(req.student.user_id, NotificationEvent.RETURNED_FOR_CORRECTION, _ctx(req, note=note))
    db.session.commit()
    return req


def assess_credits(actor, req, decisions):
    _require_dest_college(actor, req, Permission.ASSESS_CREDITS)
    if req.request_status not in (S.UNDER_REVIEW, S.INTERVIEW_SCHEDULED, S.RECOMMENDED):
        raise BusinessRuleViolation("Credits are assessed during review", error_code="NOT_UNDER_REVIEW")
    rows = {c.id: c for c in active(CreditAssessment).filter_by(request_id=req.id)}
    for d in decisions:
        row = rows.get(d["id"])
        if row is None:
            raise NotFound(f"Credit row {d['id']} is not on this request")
        decision = CreditDecision(d["decision"])
        if decision == CreditDecision.REJECTED and not d.get("reason"):
            raise ValidationError(f"A reason is required to reject credits for {row.subject_code}")
        if row.decision != CreditDecision.PENDING:
            row.decision_history = list(row.decision_history or []) + [{
                "decision": row.decision.value, "reason": row.reason, "by": row.decided_by_id,
                "at": row.decided_at.isoformat() if row.decided_at else None}]
        activity.change("credit_assessment", row.id, "decision", row.decision, decision)
        row.decision = decision
        row.reason = d.get("reason")
        row.mapped_subject_code = d.get("mapped_subject_code", row.mapped_subject_code)
        row.decided_by_id = actor.user.id
        row.decided_at = utcnow()
    activity.step("Credits assessed", count=len(decisions))
    db.session.commit()
    return list(rows.values())


def credit_summary(req):
    rows = active(CreditAssessment).filter_by(request_id=req.id).all()
    total = lambda d: float(sum(r.credits for r in rows if r.decision == d))  # noqa: E731
    return {"accepted": total(CreditDecision.ACCEPTED), "rejected": total(CreditDecision.REJECTED),
            "pending": total(CreditDecision.PENDING), "rows": len(rows)}


def _review_blockers(req):
    blockers = []
    for item in checklist(req):
        if item["required"] and not item["verified"]:
            blockers.append(f"Document not verified: {item['doc_type']}")
    if active(CreditAssessment).filter_by(request_id=req.id, decision=CreditDecision.PENDING).count():
        blockers.append("Some credits are still pending a decision")
    if req.eligibility_result in (EligibilityResult.INELIGIBLE, EligibilityResult.PENDING):
        blockers.append(f"Eligibility is {req.eligibility_result.value}; it must be resolved or overridden")
    if req.interview_required:
        passed = active(Interview).filter_by(request_id=req.id, interview_status=InterviewStatus.COMPLETED,
                                              outcome=InterviewOutcome.PASSED).first()
        if not passed:
            blockers.append("The interview has not been passed yet")
    return blockers


def recommend(actor, req, note=None, version=None):
    _check_version(req, version)
    _require_dest_college(actor, req, Permission.RECOMMEND_REQUEST)
    if req.request_status != S.UNDER_REVIEW:
        raise BusinessRuleViolation("Only requests under review can be recommended", error_code="NOT_UNDER_REVIEW")
    blockers = _review_blockers(req)
    if blockers:
        raise BusinessRuleViolation("The request cannot be recommended yet", error_code="REVIEW_INCOMPLETE",
                                    details={"problems": blockers})
    req.recommended_at = utcnow()
    transition(req, S.RECOMMENDED, note or "Recommended by the destination college")
    _notify_holders(req.destination_university or req.destination_college, Permission.APPROVE_ENTRY,
                    NotificationEvent.RECOMMENDED, _ctx(req))
    db.session.commit()
    return req


def _reject(req, reason, actor):
    req.rejected_at_status = req.request_status
    req.rejected_at = utcnow()
    req.rejection_reason = reason
    had_seat = req.seat_state is not None
    transition(req, S.REJECTED, reason)
    if had_seat:
        seats.release_seat(req, transition)
    else:
        seats.recompute_waitlist(req)
    cancel_letters(req, [LetterType.PROVISIONAL_OFFER])
    _cancel_open_interviews(req, "Request rejected")
    issue_letter(req, LetterType.REJECTION, actor.user if actor else None)
    notify(req.student.user_id, NotificationEvent.REQUEST_REJECTED,
           _ctx(req, note=reason, days=current_app.config["APPEAL_WINDOW_DAYS"]))


COLLEGE_REJECTABLE = {S.NOC_GRANTED, S.UNDER_REVIEW, S.RETURNED_FOR_CORRECTION, S.INTERVIEW_SCHEDULED}
UNIVERSITY_REJECTABLE = {S.RECOMMENDED, S.ENTRY_APPROVED}


def reject(actor, req, reason, version=None):
    _check_version(req, version)
    if not reason:
        raise ValidationError("A reason is required")
    if req.request_status in COLLEGE_REJECTABLE:
        _require_dest_college(actor, req, Permission.REVIEW_REQUEST)
    elif req.request_status in UNIVERSITY_REJECTABLE:
        _require_dest_university(actor, req, Permission.APPROVE_ENTRY)
    else:
        raise BusinessRuleViolation(f"A request in {req.request_status.value} cannot be rejected here")
    _reject(req, reason, actor)
    db.session.commit()
    return req


def override_eligibility(actor, req, reason):
    _require_dest_university(actor, req, Permission.OVERRIDE_ELIGIBILITY)
    if not reason:
        raise ValidationError("A reason is required for an override")
    if req.eligibility_rule_id:
        from app.models import EligibilityRule
        rule = db.session.get(EligibilityRule, req.eligibility_rule_id)
        if rule and rule.is_statutory:
            raise BusinessRuleViolation("This course's rule is statutory and cannot be overridden",
                                        error_code="STATUTORY_RULE")
    activity.change("migration_request", req.id, "eligibility_result", req.eligibility_result,
                    EligibilityResult.OVERRIDDEN)
    req.eligibility_result = EligibilityResult.OVERRIDDEN
    req.eligibility_notes = list(req.eligibility_notes or []) + [f"Overridden by {actor.user.full_name}: {reason}"]
    db.session.commit()
    return req


def rerun_eligibility(actor, req):
    _require_dest_college(actor, req, Permission.REVIEW_REQUEST)
    eligibility.run_check(req)
    db.session.commit()
    return req


def approve_entry(actor, req, decision, version=None):
    _check_version(req, version)
    _require_dest_university(actor, req, Permission.APPROVE_ENTRY)
    if req.request_status != S.RECOMMENDED:
        raise BusinessRuleViolation("Only recommended requests can be approved", error_code="NOT_RECOMMENDED")
    eq = EquivalenceDecision.query.filter_by(request_id=req.id).first() or EquivalenceDecision(request_id=req.id)
    eq.is_eligible = decision["is_eligible"]
    eq.credits_accepted = decision.get("credits_accepted", credit_summary(req)["accepted"])
    eq.additional_papers = decision.get("additional_papers", [])
    eq.remarks = decision.get("remarks")
    eq.decided_by_id = actor.user.id
    db.session.add(eq)
    db.session.flush()
    activity.step("Equivalence decision recorded", entity="equivalence_decision", entity_id=eq.id,
                  eligible=eq.is_eligible)
    if not eq.is_eligible:
        _reject(req, decision.get("remarks") or "Not eligible per the equivalence decision", actor)
        db.session.commit()
        return req
    req.entry_approved_at = utcnow()
    course = req.destination_offering.course
    merit_done = req.window and req.window.merit_round_completed_at is not None
    if course.offer_mode == OfferMode.ROLLING:
        transition(req, S.ENTRY_APPROVED, "Entry approved; allocating a seat")
        seats.try_allocate(req, transition)
    elif merit_done:
        transition(req, S.WAITLISTED, "Entry approved after the merit round; waitlisted by merit")
        vacancy = seats._vacancy_for(req)
        seats.promote_waitlist(vacancy, transition)
        if req.request_status == S.WAITLISTED:
            seats.recompute_waitlist(req)
            notify(req.student.user_id, NotificationEvent.WAITLISTED,
                   {"reference": req.reference_no, "rank": req.waitlist_rank})
    else:
        transition(req, S.ENTRY_APPROVED, "Entry approved; seats are allocated in the merit round")
    db.session.commit()
    return req


def confirm_admission(actor, req, fee_paid, version=None):
    _check_version(req, version)
    _require_dest_college(actor, req, Permission.CONFIRM_ADMISSION)
    if req.request_status != S.RELEASED:
        raise BusinessRuleViolation("Admission is confirmed after the release", error_code="NOT_RELEASED")
    seats.fill_seat(req)
    req.fee_paid = bool(fee_paid)
    req.admitted_at = utcnow()
    req.originals_due_at = deadline_after_days(current_app.config["ORIGINALS_REPORTING_DAYS"])
    transition(req, S.ADMITTED_ORIGINALS_PENDING, "Admission confirmed; originals pending")
    profile = req.student
    course = req.destination_offering.course
    activity.change("student_profile", profile.id, "current_college_id", profile.current_college_id,
                    req.destination_college_id)
    profile.current_college_id = req.destination_college_id
    profile.current_course_name = course.name
    profile.current_course_level = course.level
    profile.current_discipline = course.discipline
    profile.current_semester = req.entry_semester
    profile.enrolment_number = f"{req.reference_no}"  # replaced by the college's number later
    profile.enrolment_status = EnrolmentStatus.VERIFIED
    profile.enrolment_verified_by_id = actor.user.id
    profile.enrolment_verified_at = utcnow()
    issue_letter(req, LetterType.ADMISSION_CONFIRMATION, actor.user)
    notify(profile.user_id, NotificationEvent.ADMITTED, _ctx(req, due=req.originals_due_at.date().isoformat()))
    db.session.commit()
    return req


def verify_originals(actor, req):
    _require_dest_college(actor, req, Permission.VERIFY_ORIGINALS)
    if req.request_status != S.ADMITTED_ORIGINALS_PENDING:
        raise BusinessRuleViolation("Originals are verified after admission is confirmed")
    req.originals_verified_at = utcnow()
    transition(req, S.ADMITTED, "Original documents verified")
    db.session.commit()
    return req


def _cancel(req, stage, reason, actor, promote):
    req.cancellation_stage = stage
    req.cancellation_reason = reason
    transition(req, S.CANCELLED, reason)
    seats.release_seat(req, transition, promote=promote)
    cancel_letters(req)
    notify(req.student.user_id, NotificationEvent.REQUEST_CANCELLED, _ctx(req, note=reason))


def approve_cancellation(actor, req, reason=None):
    _require_dest_college(actor, req, Permission.APPROVE_CANCELLATION)
    if req.request_status != S.OFFER_ACCEPTED:
        raise BusinessRuleViolation("Only accepted offers awaiting release can be cancelled here")
    _cancel(req, CancellationStage.BEFORE_ADMISSION, reason or req.cancellation_reason or "Cancelled", actor, True)
    db.session.commit()
    return req


def cancel_for_non_reporting(actor, req, reason=None):
    _require_dest_college(actor, req, Permission.VERIFY_ORIGINALS)
    if req.request_status != S.ADMITTED_ORIGINALS_PENDING:
        raise BusinessRuleViolation("Only admissions awaiting originals can be cancelled for non-reporting")
    if ensure_aware(req.originals_due_at) > utcnow():
        raise BusinessRuleViolation("The reporting deadline has not passed yet", error_code="DEADLINE_NOT_PASSED")
    window_open = req.window is not None and req.window.opens_on <= today_ist() <= req.window.closes_on
    _cancel(req, CancellationStage.NON_REPORTING, reason or "Did not report with original documents", actor,
            window_open)
    db.session.commit()
    return req


def cancel_admission(actor, req, reason):
    _require_dest_university(actor, req, Permission.CANCEL_ADMISSION)
    if req.request_status not in (S.ADMITTED, S.ADMITTED_ORIGINALS_PENDING):
        raise BusinessRuleViolation("Only confirmed admissions can be cancelled here")
    if not reason:
        raise ValidationError("A reason is required")
    window_open = req.window is not None and req.window.opens_on <= today_ist() <= req.window.closes_on
    _cancel(req, CancellationStage.AFTER_ADMISSION, reason, actor, window_open)
    db.session.commit()
    return req


def decide_appeal(actor, appeal, uphold, note=None):
    req = db.session.get(MigrationRequest, appeal.request_id)
    decider = _appeal_decider_institution(req)
    if not (actor.is_platform_admin or (decider is not None and actor.can(Permission.DECIDE_APPEAL, decider))):
        raise PermissionDenied("You cannot decide this appeal")
    if appeal.decision != AppealDecision.PENDING:
        raise BusinessRuleViolation("This appeal was already decided", error_code="ALREADY_DECIDED")
    appeal.decided_by_id = actor.user.id
    appeal.decided_at = utcnow()
    appeal.decision_note = note
    if uphold:
        appeal.decision = AppealDecision.UPHELD
        back_to = {S.SUBMITTED: S.NOC_GRANTED, S.INTERVIEW_SCHEDULED: S.UNDER_REVIEW,
                   S.RETURNED_FOR_CORRECTION: S.UNDER_REVIEW, S.NOC_GRANTED: S.UNDER_REVIEW,
                   S.ENTRY_APPROVED: S.RECOMMENDED}.get(appeal.rejected_at_status, appeal.rejected_at_status)
        req.rejection_reason = None
        req.closed_at = None
        transition(req, back_to, f"Appeal upheld: {note or ''}".strip())
        if back_to == S.NOC_GRANTED:
            issue_letter(req, LetterType.NOC, actor.user)
        cancel_letters(req, [LetterType.REJECTION])
    else:
        appeal.decision = AppealDecision.REFUSED
        transition(req, S.REJECTED, f"Appeal refused: {note or ''}".strip())
    notify(req.student.user_id, NotificationEvent.APPEAL_DECIDED,
           _ctx(req, decision=appeal.decision.value.lower(), note=note or ""))
    activity.step("Appeal decided", entity="appeal", entity_id=appeal.id, decision=appeal.decision)
    db.session.commit()
    return appeal


def _cancel_open_interviews(req, reason):
    for iv in active(Interview).filter_by(request_id=req.id, interview_status=InterviewStatus.SCHEDULED):
        iv.interview_status = InterviewStatus.CANCELLED
        iv.cancellation_reason = reason


# ------------------------------------------------------------------- views
def summary(req):
    profile = req.student
    return {
        "id": req.id, "reference_no": req.reference_no, "request_status": req.request_status.value,
        "version": req.version, "migration_type": req.migration_type.value,
        "student_id": profile.id, "student_name": student_name(profile),
        "source_college": {"id": req.source_college_id, "name": req.source_college.name},
        "destination_college": {"id": req.destination_college_id, "name": req.destination_college.name},
        "course": {"id": req.destination_offering.course_id, "name": req.destination_offering.course.name},
        "academic_year": req.academic_year.label, "entry_semester": req.entry_semester,
        "eligibility_result": req.eligibility_result.value, "merit_score": float(req.merit_score) if req.merit_score else None,
        "waitlist_rank": req.waitlist_rank, "submitted_at": req.submitted_at.isoformat() if req.submitted_at else None,
        "offer_expires_at": req.offer_expires_at.isoformat() if req.offer_expires_at else None,
        "updated_at": req.updated_at.isoformat(),
    }


def detail(actor, req):
    from app.models import Letter
    conv = GradeConversion.query.filter_by(request_id=req.id).first()
    eq = EquivalenceDecision.query.filter_by(request_id=req.id).first()
    appeal = Appeal.query.filter_by(request_id=req.id).first()
    history = (MigrationStatusHistory.query.filter_by(request_id=req.id)
               .order_by(MigrationStatusHistory.created_at).all())
    interviews = active(Interview).filter_by(request_id=req.id).order_by(Interview.scheduled_start).all()
    return {
        **req.to_dict(),
        "summary": summary(req),
        "checklist": checklist(req),
        "conversion": conv.to_dict() if conv else None,
        "conversion_options": eligibility.conversion_options(req) if req.request_status in (
            S.DRAFT, S.RETURNED_FOR_CORRECTION) else None,
        "credits": [c.to_dict() for c in active(CreditAssessment).filter_by(request_id=req.id)
                    .order_by(CreditAssessment.semester, CreditAssessment.subject_code)],
        "credit_summary": credit_summary(req),
        "equivalence_decision": eq.to_dict() if eq else None,
        "appeal": appeal.to_dict() if appeal else None,
        "interviews": [i.to_dict() for i in interviews],
        "letters": [lt.to_dict(exclude=("storage_key", "qr_token")) for lt in active(Letter).filter_by(request_id=req.id)],
        "history": [h.to_dict() for h in history],
        "submission_problems": _submission_problems(req) if req.request_status == S.DRAFT else [],
        "review_blockers": _review_blockers(req) if req.request_status == S.UNDER_REVIEW else [],
    }


def staff_query(actor, direction=None):
    q = active(MigrationRequest).filter(MigrationRequest.request_status != S.DRAFT)
    if actor.is_platform_admin:
        return q
    colleges, universities = list(actor.college_ids()) or [-1], list(actor.university_ids()) or [-1]
    incoming = db.or_(MigrationRequest.destination_college_id.in_(colleges),
                      MigrationRequest.destination_university_id.in_(universities))
    outgoing = db.or_(MigrationRequest.source_college_id.in_(colleges),
                      MigrationRequest.source_university_id.in_(universities))
    if direction == "incoming":
        return q.filter(incoming)
    if direction == "outgoing":
        return q.filter(outgoing)
    return q.filter(db.or_(incoming, outgoing))


def student_requests(user):
    profile = StudentProfile.query.filter_by(user_id=user.id).first()
    if profile is None:
        return MigrationRequest.query.filter(db.false())
    return active(MigrationRequest).filter_by(student_id=profile.id)
