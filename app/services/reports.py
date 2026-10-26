"""Dashboards (10.3) and reports (10.4). Every figure is limited to the caller's scope."""
import io
from collections import defaultdict
from datetime import datetime, time, timedelta, timezone

from openpyxl import Workbook
from openpyxl.styles import Font
from sqlalchemy import func

from app.core.utils import IST, today_ist, utcnow
from app.enums import (ApprovalStatus, CreditDecision, DataRequestState, DocumentOwnerType, EnrolmentStatus,
                       InterviewStatus, MembershipRole, MigrationStatus as S, MigrationType,
                       NotificationState, OnboardingStatus, Permission, RecordStatus,
                       TicketState, VerificationStatus)
from app.extensions import db
from app.models import (Appeal, CollegeOnboardingRequest, Course, CourseOffering,
                        College, CreditAssessment, DataRequest, Document, InstitutionMembership, Interview,
                        IpBlock, MigrationRequest, MigrationStatusHistory, Notification,
                        OfferingRequest, RegistrationRequest, StudentProfile, SupportTicket, Vacancy,
                        University, VacancyCategory)
from app.core.records import active, category_available, is_active, student_name, vacancy_available, vacancy_total_seats


# ----------------------------------------------------------------- scope
def scope_ids(actor, permission=None):
    """College ids the actor may report on (a university member gets all its colleges). None = everything.

    Every migration request, offering and interview belongs to a college, so a set of
    college ids is enough to scope every report.
    """
    if actor.is_platform_admin:
        return None
    ids = set()
    for m in actor.memberships:
        if permission and permission not in actor.membership_permissions(m):
            continue
        if m.college_id:
            ids.add(m.college_id)
        else:
            ids.update(cid for (cid,) in db.session.query(College.id).filter(College.university_id == m.university_id))
    return ids or {-1}


def _req_scope(q, ids, direction=None):
    if ids is None:
        return q
    inc = MigrationRequest.destination_college_id.in_(ids)
    out = MigrationRequest.source_college_id.in_(ids)
    if direction == "incoming":
        return q.filter(inc)
    if direction == "outgoing":
        return q.filter(out)
    return q.filter(db.or_(inc, out))


def _count(q):
    return q.order_by(None).count()


def _status_counts(q):
    rows = q.with_entities(MigrationRequest.request_status, func.count(MigrationRequest.id)) \
        .group_by(MigrationRequest.request_status).order_by(None).all()
    return {s.value: n for s, n in rows}


# -------------------------------------------------------------- dashboards
def student_dashboard(user):
    from app.services.migration import checklist
    from app.services.students import profile_view
    profile = StudentProfile.query.filter_by(user_id=user.id).first()
    if profile is None:
        return {}
    reqs = active(MigrationRequest).filter_by(student_id=profile.id).order_by(MigrationRequest.created_at.desc()).all()
    tasks = []
    view = profile_view(profile)
    if view["missing_fields"]:
        tasks.append({"task": "COMPLETE_PROFILE", "detail": view["missing_fields"]})
    if profile.enrolment_status in (EnrolmentStatus.NOT_SUBMITTED, EnrolmentStatus.REJECTED):
        tasks.append({"task": "SUBMIT_ENROLMENT", "detail": profile.enrolment_rejection_reason})
    upcoming = []
    for r in reqs:
        if r.request_status == S.RETURNED_FOR_CORRECTION:
            tasks.append({"task": "MAKE_CORRECTIONS", "request_id": r.id, "detail": r.correction_note})
        if r.request_status == S.PROVISIONAL_OFFER:
            tasks.append({"task": "ACCEPT_OR_DECLINE_OFFER", "request_id": r.id,
                          "deadline": r.offer_expires_at.isoformat()})
        if r.request_status == S.ADMITTED_ORIGINALS_PENDING:
            tasks.append({"task": "REPORT_WITH_ORIGINALS", "request_id": r.id,
                          "deadline": r.originals_due_at.isoformat()})
        if r.request_status in (S.DRAFT, S.RETURNED_FOR_CORRECTION):
            for item in checklist(r):
                if item["required"] and (not item["uploaded"] or item.get("needs_attention")):
                    tasks.append({"task": "UPLOAD_DOCUMENT", "request_id": r.id, "detail": item["doc_type"]})
        for iv in active(Interview).filter_by(request_id=r.id, interview_status=InterviewStatus.SCHEDULED):
            upcoming.append({"interview_id": iv.id, "request_id": r.id, "when": iv.scheduled_start.isoformat(),
                             "mode": iv.mode.value, "meeting_url": iv.meeting_url,
                             "meeting_info": iv.meeting_info, "location": iv.location})
    from app.services.migration import summary
    return {"profile": view, "pending_tasks": tasks, "requests": [summary(r) for r in reqs],
            "upcoming_interviews": upcoming,
            "documents_needing_attention": [d.to_dict(exclude=("storage_key",)) for d in active(Document).filter(
                Document.owner_type == DocumentOwnerType.USER, Document.owner_id == user.id,
                Document.is_current.is_(True),
                Document.verification_status.in_([VerificationStatus.REJECTED, VerificationStatus.CORRECTION_REQUESTED]))]}


def college_dashboard(actor, college):
    actor.require_view(college)
    cid = college.id
    base = active(MigrationRequest)
    incoming = base.filter(MigrationRequest.destination_college_id == cid)
    outgoing = base.filter(MigrationRequest.source_college_id == cid)
    start = datetime.combine(today_ist(), time.min, tzinfo=IST).astimezone(timezone.utc)
    pending = {
        "enrolment_verifications": _count(active(StudentProfile).filter_by(current_college_id=cid,
                                                                            enrolment_status=EnrolmentStatus.PENDING)),
        "noc_decisions": _count(outgoing.filter(MigrationRequest.request_status == S.SUBMITTED)),
        "requests_to_review": _count(incoming.filter(MigrationRequest.request_status.in_([S.NOC_GRANTED, S.UNDER_REVIEW]))),
        "interviews_today": _count(active(Interview).filter(Interview.college_id == cid,
                                                             Interview.interview_status == InterviewStatus.SCHEDULED,
                                                             Interview.scheduled_start >= start,
                                                             Interview.scheduled_start < start + timedelta(days=1))),
        "releases_to_issue": _count(outgoing.filter(MigrationRequest.request_status == S.OFFER_ACCEPTED,
                                                    MigrationRequest.tc_uploaded_at.is_(None))),
        "admissions_to_confirm": _count(incoming.filter(MigrationRequest.request_status == S.RELEASED)),
        "originals_pending": _count(incoming.filter(MigrationRequest.request_status == S.ADMITTED_ORIGINALS_PENDING)),
        "cancellation_requests": _count(incoming.filter(MigrationRequest.request_status == S.OFFER_ACCEPTED,
                                                        MigrationRequest.cancellation_requested_at.isnot(None))),
        "access_requests": _count(active(InstitutionMembership).filter_by(college_id=cid, role=MembershipRole.USER,
                                                                           approval_status=ApprovalStatus.PENDING)),
    }
    vacancies = (active(Vacancy).join(CourseOffering, CourseOffering.id == Vacancy.offering_id)
                 .filter(CourseOffering.college_id == cid).all())
    no_shows = _count(active(Interview).filter(Interview.college_id == cid,
                                                Interview.interview_status == InterviewStatus.NO_SHOW))
    return {"institution": {"id": cid, "name": college.name}, "pending_tasks": pending,
            "incoming_by_status": _status_counts(incoming), "outgoing_by_status": _status_counts(outgoing),
            "vacancies": {"rows": len(vacancies), "total_seats": sum(vacancy_total_seats(v) for v in vacancies),
                          "available": sum(vacancy_available(v) for v in vacancies),
                          "offerings_without_vacancy": _count(active(CourseOffering).filter(
                              CourseOffering.college_id == cid,
                              ~CourseOffering.id.in_(db.session.query(Vacancy.offering_id))))},
            "interviews": {"no_shows": no_shows},
            "users": _count(active(InstitutionMembership).filter_by(college_id=cid,
                                                                    approval_status=ApprovalStatus.APPROVED))}


def university_dashboard(actor, university):
    actor.require_view(university)
    uid = university.id
    colleges = College.query.filter_by(university_id=uid)
    base = active(MigrationRequest)
    incoming = base.filter(MigrationRequest.destination_university_id == uid)
    outgoing = base.filter(MigrationRequest.source_university_id == uid)
    pending = {
        "college_registrations": _count(active(RegistrationRequest).filter_by(approver_university_id=uid,
                                                                               approval_status=ApprovalStatus.PENDING)),
        "course_offering_requests": _count(active(OfferingRequest).join(Course, Course.id == OfferingRequest.course_id)
                                           .filter(Course.university_id == uid,
                                                   OfferingRequest.approval_status == ApprovalStatus.PENDING)),
        "entry_approvals": _count(incoming.filter(MigrationRequest.request_status == S.RECOMMENDED)),
        "leaving_approvals": _count(outgoing.filter(MigrationRequest.request_status == S.OFFER_ACCEPTED,
                                                    MigrationRequest.migration_type == MigrationType.CROSS_UNIVERSITY,
                                                    MigrationRequest.source_university_released_at.is_(None))),
        "appeals": _count(active(Appeal).join(MigrationRequest, MigrationRequest.id == Appeal.request_id).filter(
            Appeal.decision == "PENDING",
            db.or_(MigrationRequest.destination_university_id == uid, MigrationRequest.source_university_id == uid))),
        "escalated_nocs": _count(outgoing.filter(MigrationRequest.request_status == S.SUBMITTED,
                                                 MigrationRequest.noc_escalated_at.isnot(None))),
        "access_requests": _count(active(InstitutionMembership).filter_by(university_id=uid, role=MembershipRole.USER,
                                                                           approval_status=ApprovalStatus.PENDING)),
    }
    vac = (db.session.query(func.coalesce(func.sum(VacancyCategory.seats), 0),
                            func.coalesce(func.sum(VacancyCategory.blocked), 0),
                            func.coalesce(func.sum(VacancyCategory.filled), 0))
           .join(Vacancy, Vacancy.id == VacancyCategory.vacancy_id)
           .join(CourseOffering, CourseOffering.id == Vacancy.offering_id)
           .join(Course, Course.id == CourseOffering.course_id)
           .filter(Course.university_id == uid, VacancyCategory.status == RecordStatus.ACTIVE,
                   Vacancy.status == RecordStatus.ACTIVE).one())
    return {"institution": {"id": uid, "name": university.name}, "pending_tasks": pending,
            "colleges": {"total": _count(colleges),
                         "active": _count(colleges.filter(College.onboarding_status == OnboardingStatus.ACTIVE))},
            "courses": _count(active(Course).filter_by(university_id=uid)),
            "offerings": _count(active(CourseOffering).join(Course, Course.id == CourseOffering.course_id)
                                .filter(Course.university_id == uid)),
            "vacancies": {"seats": int(vac[0]), "blocked": int(vac[1]), "filled": int(vac[2]),
                          "available": max(int(vac[0] - vac[1] - vac[2]), 0)},
            "incoming_by_status": _status_counts(incoming), "outgoing_by_status": _status_counts(outgoing)}


def waiting_on_non_onboarded():
    rows = (db.session.query(College, func.count(StudentProfile.id))
            .join(StudentProfile, StudentProfile.current_college_id == College.id)
            .filter(StudentProfile.enrolment_status == EnrolmentStatus.PENDING,
                    College.onboarding_status != OnboardingStatus.ACTIVE)
            .group_by(College.id).order_by(func.count(StudentProfile.id).desc()).limit(50).all())
    return [{"college_id": i.id, "name": i.name, "aishe_code": i.aishe_code, "institution_type": i.institution_type,
             "onboarding_status": i.onboarding_status.value, "students_waiting": n} for i, n in rows]


def _onboarding_counts():
    """[(institution_type, onboarding_status, count)]: UNIVERSITY plus each colleges.institution_type."""
    rows = [("UNIVERSITY", status.value, n) for status, n in
            db.session.query(University.onboarding_status, func.count(University.id))
            .group_by(University.onboarding_status).all()]
    rows += [(itype, status.value, n) for itype, status, n in
             db.session.query(College.institution_type, College.onboarding_status, func.count(College.id))
             .group_by(College.institution_type, College.onboarding_status).all()]
    return rows


def platform_dashboard():
    by_type = defaultdict(dict)
    for itype, status, n in _onboarding_counts():
        by_type[itype][status] = n
    now = utcnow()
    return {
        "pending_tasks": {
            "registrations": _count(active(RegistrationRequest).filter(
                RegistrationRequest.approver_university_id.is_(None),
                RegistrationRequest.approval_status == ApprovalStatus.PENDING)),
            "second_admin_claims": _count(active(RegistrationRequest).filter_by(
                is_second_claim=True, approval_status=ApprovalStatus.PENDING)),
            "overdue_releases": _count(active(MigrationRequest).filter(
                MigrationRequest.request_status == S.OFFER_ACCEPTED, MigrationRequest.release_due_at < now)),
            "college_onboarding_requests": _count(active(CollegeOnboardingRequest).filter_by(
                approval_status=ApprovalStatus.PENDING)),
            "support_tickets_open": _count(active(SupportTicket).filter(
                SupportTicket.ticket_state.in_([TicketState.OPEN, TicketState.IN_PROGRESS]))),
            "data_requests_open": _count(active(DataRequest).filter_by(request_state=DataRequestState.OPEN)),
        },
        "institutions": dict(by_type),
        "students_waiting_on_colleges": waiting_on_non_onboarded(),
        "alerts": {
            "failed_notifications_24h": _count(Notification.query.filter(
                Notification.delivery_state == NotificationState.FAILED, Notification.updated_at >= now - timedelta(days=1))),
            "blocked_ips": _count(active(IpBlock).filter(IpBlock.blocked_until > now)),
            "unverified_pincodes": (_count(University.query.filter(University.pincode_verified.is_(False)))
                                    + _count(College.query.filter(College.pincode_verified.is_(False)))),
        },
        "users": {"active": _count(db.session.query(InstitutionMembership.user_id).distinct()),
                  "students": _count(active(StudentProfile))},
        "requests_by_status": _status_counts(active(MigrationRequest)),
    }


# ------------------------------------------------------------------ reports
def _date_filter(q, column, date_from, date_to):
    if date_from:
        q = q.filter(column >= datetime.combine(date_from, time.min, tzinfo=IST))
    if date_to:
        q = q.filter(column <= datetime.combine(date_to, time.max, tzinfo=IST))
    return q


def report_applications(actor, f):
    ids = scope_ids(actor, Permission.VIEW_REPORTS)
    q = _req_scope(active(MigrationRequest).filter(MigrationRequest.request_status != S.DRAFT), ids, f.get("direction"))
    q = _date_filter(q, MigrationRequest.submitted_at, f.get("date_from"), f.get("date_to"))
    if f.get("college_id"):
        q = q.filter(db.or_(MigrationRequest.destination_college_id == f["college_id"],
                            MigrationRequest.source_college_id == f["college_id"]))
    if f.get("course_id"):
        q = q.join(CourseOffering, CourseOffering.id == MigrationRequest.destination_offering_id) \
            .filter(CourseOffering.course_id == f["course_id"])
    if f.get("status"):
        q = q.filter(MigrationRequest.request_status == S(f["status"]))
    rows = []
    for r in q.order_by(MigrationRequest.submitted_at.desc()).limit(5000).all():
        rows.append({"reference_no": r.reference_no, "student": student_name(r.student), "status": r.request_status.value,
                     "type": r.migration_type.value, "source": r.source_college.name,
                     "destination": r.destination_college.name, "course": r.destination_offering.course.name,
                     "entry_semester": r.entry_semester, "academic_year": r.academic_year.label,
                     "merit_score": float(r.merit_score) if r.merit_score else None,
                     "submitted_at": r.submitted_at.astimezone(IST).strftime("%Y-%m-%d") if r.submitted_at else ""})
    totals = defaultdict(int)
    for row in rows:
        totals[row["status"]] += 1
    return {"rows": rows, "totals": dict(totals)}


def report_vacancies(actor, f):
    ids = scope_ids(actor, Permission.VIEW_REPORTS)
    q = active(Vacancy).join(CourseOffering, CourseOffering.id == Vacancy.offering_id)
    if ids is not None:
        q = q.filter(CourseOffering.college_id.in_(ids))
    if f.get("college_id"):
        q = q.filter(CourseOffering.college_id == f["college_id"])
    if f.get("course_id"):
        q = q.filter(CourseOffering.course_id == f["course_id"])
    if f.get("academic_year_id"):
        q = q.filter(Vacancy.academic_year_id == f["academic_year_id"])
    rows = []
    for v in q.all():
        for c in v.categories:
            if not is_active(c):
                continue
            rows.append({"college": v.offering.college.name, "course": v.offering.course.name,
                         "academic_year": v.academic_year.label, "entry_semester": v.entry_semester,
                         "sanctioned_intake": v.offering.sanctioned_intake, "category": c.category.value,
                         "seats": c.seats, "blocked": c.blocked, "filled": c.filled, "available": category_available(c),
                         "discrepancy": v.discrepancy_flag})
    totals = {k: sum(r[k] for r in rows) for k in ("seats", "blocked", "filled", "available")}
    return {"rows": rows, "totals": totals}


def report_migration_flow(actor, f):
    ids = scope_ids(actor, Permission.VIEW_REPORTS)
    q = _req_scope(active(MigrationRequest).filter(MigrationRequest.request_status != S.DRAFT), ids)
    q = _date_filter(q, MigrationRequest.submitted_at, f.get("date_from"), f.get("date_to"))
    flow = defaultdict(lambda: {"incoming_requests": 0, "incoming_admitted": 0, "outgoing_requests": 0,
                                "outgoing_admitted": 0})
    names = {}
    admitted = {S.ADMITTED, S.ADMITTED_ORIGINALS_PENDING}
    for r in q.all():
        for inst, key in ((r.destination_college, "incoming"), (r.source_college, "outgoing")):
            if ids is not None and inst.id not in ids:
                continue
            names[inst.id] = inst.name
            flow[inst.id][f"{key}_requests"] += 1
            if r.request_status in admitted:
                flow[inst.id][f"{key}_admitted"] += 1
    rows = [{"institution": names[k], **v, "net_admitted": v["incoming_admitted"] - v["outgoing_admitted"]}
            for k, v in flow.items()]
    return {"rows": sorted(rows, key=lambda r: -r["incoming_requests"]), "totals": {}}


STEPS = [("NOC decision", S.SUBMITTED, {S.NOC_GRANTED, S.REJECTED}),
         ("Destination review", S.NOC_GRANTED, {S.RECOMMENDED, S.REJECTED}),
         ("University entry approval", S.RECOMMENDED, {S.ENTRY_APPROVED, S.PROVISIONAL_OFFER, S.WAITLISTED, S.REJECTED}),
         ("Student offer decision", S.PROVISIONAL_OFFER, {S.OFFER_ACCEPTED, S.OFFER_DECLINED, S.OFFER_EXPIRED}),
         ("Source release", S.OFFER_ACCEPTED, {S.RELEASED}),
         ("Admission confirmation", S.RELEASED, {S.ADMITTED_ORIGINALS_PENDING})]


def report_turnaround(actor, f):
    ids = scope_ids(actor, Permission.VIEW_REPORTS)
    req_ids = [r.id for r in _req_scope(active(MigrationRequest), ids).with_entities(MigrationRequest.id)]
    hist = defaultdict(list)
    for h in (MigrationStatusHistory.query.filter(MigrationStatusHistory.request_id.in_(req_ids or [-1]))
              .order_by(MigrationStatusHistory.created_at)):
        hist[h.request_id].append(h)
    durations = defaultdict(list)
    for events in hist.values():
        for label, start_status, end_statuses in STEPS:
            start = next((e.created_at for e in events if e.to_status == start_status), None)
            end = next((e.created_at for e in events if start and e.created_at >= start and e.to_status in end_statuses), None)
            if start and end:
                durations[label].append((end - start).total_seconds() / 86400)
    rows = [{"step": label, "completed": len(durations[label]),
             "average_days": round(sum(durations[label]) / len(durations[label]), 1) if durations[label] else None,
             "max_days": round(max(durations[label]), 1) if durations[label] else None} for label, _, _ in STEPS]
    overdue = _req_scope(active(MigrationRequest), ids).filter(
        MigrationRequest.request_status == S.OFFER_ACCEPTED, MigrationRequest.release_due_at < utcnow()).all()
    return {"rows": rows, "totals": {"overdue_releases": len(overdue)},
            "overdue": [{"reference_no": r.reference_no, "source": r.source_college.name,
                         "due": r.release_due_at.isoformat()} for r in overdue]}


def report_interviews(actor, f):
    ids = scope_ids(actor, Permission.VIEW_REPORTS)
    q = active(Interview)
    if ids is not None:
        q = q.filter(Interview.college_id.in_(ids))
    q = _date_filter(q, Interview.scheduled_start, f.get("date_from"), f.get("date_to"))
    counts = defaultdict(lambda: defaultdict(int))
    names = {}
    for iv in q.all():
        names[iv.college_id] = iv.college.name
        counts[iv.college_id][iv.interview_status.value] += 1
    rows = [{"college": names[k], **{s.value: v.get(s.value, 0) for s in InterviewStatus}} for k, v in counts.items()]
    return {"rows": rows, "totals": {s.value: sum(r[s.value] for r in rows) for s in InterviewStatus}}


def report_credit_transfer(actor, f):
    ids = scope_ids(actor, Permission.VIEW_REPORTS)
    q = (db.session.query(MigrationRequest.destination_college_id, CreditAssessment.decision,
                          func.sum(CreditAssessment.credits), func.count(CreditAssessment.id))
         .join(MigrationRequest, MigrationRequest.id == CreditAssessment.request_id)
         .filter(CreditAssessment.status == RecordStatus.ACTIVE))
    if ids is not None:
        q = q.filter(MigrationRequest.destination_college_id.in_(ids))
    agg = defaultdict(dict)
    for inst_id, decision, credits, n in q.group_by(MigrationRequest.destination_college_id,
                                                    CreditAssessment.decision).all():
        agg[inst_id][decision.value] = float(credits or 0)
    rows = [{"college": db.session.get(College, k).name,
             **{d.value: v.get(d.value, 0.0) for d in CreditDecision}} for k, v in agg.items()]
    return {"rows": rows, "totals": {d.value: sum(r[d.value] for r in rows) for d in CreditDecision}}


def report_document_verification(actor, f):
    ids = scope_ids(actor, Permission.VIEW_REPORTS)
    req_q = _req_scope(active(MigrationRequest), ids, "incoming")
    req_ids = [r.id for r in req_q.with_entities(MigrationRequest.id)]
    student_users = [p.user_id for p in StudentProfile.query.filter(
        StudentProfile.id.in_(req_q.with_entities(MigrationRequest.student_id)))]
    q = active(Document).filter(Document.is_current.is_(True), db.or_(
        db.and_(Document.owner_type == DocumentOwnerType.MIGRATION_REQUEST, Document.owner_id.in_(req_ids or [-1])),
        db.and_(Document.owner_type == DocumentOwnerType.USER, Document.owner_id.in_(student_users or [-1]))))
    counts = defaultdict(lambda: defaultdict(int))
    for d in q.all():
        counts[d.doc_type.value][d.verification_status.value] += 1
    rows = [{"document_type": k, **{s.value: v.get(s.value, 0) for s in VerificationStatus}} for k, v in counts.items()]
    return {"rows": rows, "totals": {s.value: sum(r[s.value] for r in rows) for s in VerificationStatus}}


def report_onboarding(actor, f):
    rows = []
    for itype, status, n in _onboarding_counts():
        rows.append({"institution_type": itype, "onboarding_status": status, "count": n})
    return {"rows": rows, "totals": {"institutions": sum(r["count"] for r in rows)},
            "students_waiting": waiting_on_non_onboarded()}


REPORTS = {
    "applications": report_applications,
    "vacancies": report_vacancies,
    "migration-flow": report_migration_flow,
    "turnaround": report_turnaround,
    "interviews": report_interviews,
    "credit-transfer": report_credit_transfer,
    "document-verification": report_document_verification,
    "onboarding": report_onboarding,
}


def to_xlsx(title, rows):
    wb = Workbook()
    ws = wb.active
    ws.title = title[:31]
    if rows:
        headers = list(rows[0].keys())
        ws.append([h.replace("_", " ").title() for h in headers])
        for cell in ws[1]:
            cell.font = Font(bold=True)
        for row in rows:
            ws.append([row.get(h) if not isinstance(row.get(h), (list, dict)) else str(row.get(h)) for h in headers])
        for col in ws.columns:
            ws.column_dimensions[col[0].column_letter].width = min(max(len(str(c.value or "")) for c in col) + 2, 50)
    else:
        ws.append(["No data for these filters"])
    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()
