"""Student registration, consent, profile, enrolment verification and academic records (4.4, M07, M08)."""
from flask import current_app, request
from sqlalchemy import func

from app.core.activity import activity
from app.core.errors import (BusinessRuleViolation, Conflict, PermissionDenied, ValidationError)
from app.core.security import hash_password, validate_password_policy
from app.core.utils import age_on, utcnow, valid_abc_id
from app.enums import (AccountState, CourseLevel, DocumentOwnerType, DocumentType, EnrolmentStatus,
                       Gender, GradeSystem, MigrationStatus, NotificationEvent,
                       OtpPurpose, Permission, ReservationCategory, UserType, VerificationStatus)
from app.extensions import db
from app.models import (AcademicRecord, College, ConsentRecord, MigrationRequest, StudentProfile,
                        SubjectResult, User)
from app.services import otp as otp_service
from app.services.directory import responsible_users
from app.services.documents import store_upload
from app.services.notifications import notify
from app.core.records import active, deactivate, is_operational, student_name

EDITABLE_PROFILE = ("first_name", "last_name", "date_of_birth", "gender", "category", "guardian_name",
                    "guardian_email", "guardian_mobile", "address_line", "state_id", "district_id", "pincode")
ENROLMENT_FIELDS = ("current_college_id", "enrolment_number", "current_course_name", "current_course_level",
                    "current_discipline", "current_semester", "batch_year")
LOCKING_STATUSES = {MigrationStatus.SUBMITTED, MigrationStatus.NOC_GRANTED, MigrationStatus.UNDER_REVIEW,
                    MigrationStatus.INTERVIEW_SCHEDULED, MigrationStatus.RECOMMENDED,
                    MigrationStatus.ENTRY_APPROVED, MigrationStatus.WAITLISTED, MigrationStatus.PROVISIONAL_OFFER,
                    MigrationStatus.OFFER_ACCEPTED, MigrationStatus.RELEASED,
                    MigrationStatus.ADMITTED_ORIGINALS_PENDING, MigrationStatus.APPEALED}


def _client_ip():
    return (request.headers.get("X-Forwarded-For", request.remote_addr) or "")[:100]


def is_minor(profile):
    return bool(profile.date_of_birth) and age_on(profile.date_of_birth) < current_app.config["AGE_OF_MAJORITY"]


def register_student(data):
    """CREATE_STUDENT: account + profile + consent; no approval needed (M07-F01)."""
    if not data.get("consent"):
        raise ValidationError("You must accept the consent notice to register", error_code="CONSENT_REQUIRED")
    email = data["email"].strip().lower()
    if User.query.filter(func.lower(User.email) == email).first():
        raise Conflict("An account with this email already exists", error_code="EMAIL_TAKEN")
    abc = data.get("abc_account_id")
    if abc:
        if not valid_abc_id(abc):
            raise ValidationError("ABC account ID must be 12 digits", error_code="INVALID_ABC_ID")
        if StudentProfile.query.filter_by(abc_account_id=abc).first():
            raise Conflict("This ABC account ID is already registered", error_code="DUPLICATE_ABC_ID")
    possible_dupe = User.query.filter(User.mobile == data["mobile"], User.user_type == UserType.STUDENT).first()
    validate_password_policy(data["password"])
    user = User(email=email, full_name=f"{data['first_name']} {data.get('last_name') or ''}".strip(),
                mobile=data["mobile"], password_hash=hash_password(data["password"]), password_changed_at=utcnow(),
                user_type=UserType.STUDENT, account_state=AccountState.PENDING_VERIFICATION,
                whatsapp_opt_in=bool(data.get("whatsapp_opt_in")), language=data.get("language", "en"))
    db.session.add(user)
    db.session.flush()
    profile = StudentProfile(user_id=user.id, first_name=data["first_name"], last_name=data.get("last_name"),
                             date_of_birth=data.get("date_of_birth"),
                             gender=Gender(data.get("gender", Gender.UNDISCLOSED.value)),
                             abc_account_id=abc,
                             category=ReservationCategory(data.get("category", ReservationCategory.OPEN.value)))
    db.session.add(profile)
    db.session.add(ConsentRecord(user_id=user.id, notice_version=current_app.config["CONSENT_NOTICE_VERSION"],
                                 consented_at=utcnow(), whatsapp_opt_in=bool(data.get("whatsapp_opt_in")),
                                 ip_address=_client_ip()))
    db.session.flush()
    activity.step("Student account, profile and consent created", entity="student_profile", entity_id=profile.id)
    if possible_dupe:
        activity.step("Possible duplicate student flagged (same mobile)", entity="user", entity_id=possible_dupe.id)
    code = otp_service.issue_otp(user, OtpPurpose.EMAIL_VERIFICATION)
    db.session.commit()
    out = {"user_id": user.id, "student_id": profile.id, "next_step": "VERIFY_EMAIL",
           "parental_consent_required": is_minor(profile), **otp_service.debug_payload(code)}
    if possible_dupe:
        out["warning"] = "Another student account uses this mobile number"
    return out


def get_profile(user):
    profile = StudentProfile.query.filter_by(user_id=user.id).first()
    if profile is None:
        raise PermissionDenied("Student profile not found")
    return profile


def profile_view(profile):
    inst = profile.current_college
    out = profile.to_dict()
    out.update({
        "email": profile.user.email, "mobile": profile.user.mobile,
        "current_college_name": inst.name if inst else None,
        "current_college_aishe_code": inst.aishe_code if inst else None,
        "current_college_onboarded": bool(inst and is_operational(inst)),
        "is_minor": is_minor(profile),
        "parental_consent_required": is_minor(profile) and not profile.parental_consent_at,
        "profile_complete": not missing_profile_fields(profile),
        "missing_fields": missing_profile_fields(profile),
    })
    return out


def missing_profile_fields(profile):
    required = ["first_name", "date_of_birth", "abc_account_id", "address_line", "state_id", "district_id",
                "pincode", "current_college_id", "enrolment_number", "current_course_name",
                "current_semester", "batch_year"]
    missing = [f for f in required if not getattr(profile, f)]
    if is_minor(profile) and not profile.parental_consent_at:
        missing.append("parental_consent")
    return missing


def _has_locking_request(profile):
    return MigrationRequest.query.filter(MigrationRequest.student_id == profile.id,
                                         MigrationRequest.request_status.in_(list(LOCKING_STATUSES))).first() is not None


def update_profile(user, data):
    profile = get_profile(user)
    if "abc_account_id" in data and data["abc_account_id"] != profile.abc_account_id:
        if not valid_abc_id(data["abc_account_id"]):
            raise ValidationError("ABC account ID must be 12 digits", error_code="INVALID_ABC_ID")
        clash = StudentProfile.query.filter(StudentProfile.abc_account_id == data["abc_account_id"],
                                            StudentProfile.id != profile.id).first()
        if clash:
            raise Conflict("This ABC account ID is already registered", error_code="DUPLICATE_ABC_ID")
        if _has_locking_request(profile):
            raise BusinessRuleViolation("ABC ID cannot change while a request is in progress")
        profile.abc_account_id = data["abc_account_id"]
    for key in EDITABLE_PROFILE:
        if key in data:
            value = data[key]
            if key == "gender":
                value = Gender(value)
            if key == "category":
                value = ReservationCategory(value)
                if _has_locking_request(profile) and value != profile.category:
                    raise BusinessRuleViolation("Category cannot change while a request is in progress")
                profile.category_verified = False
            activity.change("student_profile", profile.id, key, getattr(profile, key), value)
            setattr(profile, key, value)
    if "first_name" in data or "last_name" in data:
        user.full_name = student_name(profile)
    db.session.commit()
    return profile


def submit_enrolment(user, data, proof_file):
    """Student claims their current college; the college verifies it (4.4)."""
    profile = get_profile(user)
    if profile.enrolment_status == EnrolmentStatus.VERIFIED and _has_locking_request(profile):
        raise BusinessRuleViolation("Enrolment cannot change while a request is in progress")
    inst = db.session.get(College, int(data["current_college_id"]))
    if inst is None:
        raise ValidationError("Choose a college or standalone institution", error_code="INVALID_INSTITUTION")
    clash = StudentProfile.query.filter(StudentProfile.current_college_id == inst.id,
                                        StudentProfile.enrolment_number == data["enrolment_number"],
                                        StudentProfile.id != profile.id).first()
    if clash:
        raise Conflict("This enrolment number is already registered at this institution",
                       error_code="DUPLICATE_ENROLMENT")
    for key in ENROLMENT_FIELDS:
        if key in data:
            value = CourseLevel(data[key]) if key == "current_course_level" and data[key] else data[key]
            setattr(profile, key, value)
    if proof_file is None and not profile.enrolment_proof_document_id:
        raise ValidationError("Upload proof of enrolment (ID card or fee receipt)", error_code="PROOF_REQUIRED")
    if proof_file is not None:
        doc = store_upload(proof_file, DocumentOwnerType.USER, user.id, DocumentType.ENROLMENT_PROOF)
        profile.enrolment_proof_document_id = doc.id
    profile.enrolment_status = EnrolmentStatus.PENDING
    profile.enrolment_verified_by_id = None
    profile.enrolment_verified_at = None
    profile.enrolment_rejection_reason = None
    activity.step("Enrolment submitted for verification", entity="student_profile", entity_id=profile.id,
                  college_id=inst.id)
    activity.set_institution(inst)
    for u in responsible_users(inst, Permission.VERIFY_ENROLMENT):
        notify(u, NotificationEvent.ENROLMENT_VERIFICATION_REQUESTED,
               {"student": student_name(profile), "institution": inst.name})
    db.session.commit()
    return profile


def _can_verify_enrolment(actor, inst):
    """College verifier; the university or platform only when the college has no active admin (4.4)."""
    from app.services.directory import has_active_admin
    if actor.is_platform_admin:
        return True
    direct = any(m.college_id == inst.id and Permission.VERIFY_ENROLMENT in actor.membership_permissions(m)
                 for m in actor.memberships)
    if direct:
        return True
    if inst.university_id and not (is_operational(inst) and has_active_admin(inst)):
        return actor.can(Permission.VERIFY_ENROLMENT, inst)
    return False


def decide_enrolment(actor, profile, approve, reason=None):
    inst = profile.current_college
    if inst is None or not _can_verify_enrolment(actor, inst):
        raise PermissionDenied("You cannot verify enrolments for this institution")
    if profile.enrolment_status != EnrolmentStatus.PENDING:
        raise BusinessRuleViolation("There is no pending enrolment to decide", error_code="NOT_PENDING")
    if approve:
        profile.enrolment_status = EnrolmentStatus.VERIFIED
        profile.enrolment_verified_by_id = actor.user.id
        profile.enrolment_verified_at = utcnow()
        if profile.enrolment_proof_document_id:
            from app.models import Document
            doc = db.session.get(Document, profile.enrolment_proof_document_id)
            doc.verification_status = VerificationStatus.VERIFIED
            doc.verified_by_id, doc.verified_at = actor.user.id, utcnow()
        decision = "verified"
    else:
        if not reason:
            raise ValidationError("A reason is required to reject")
        profile.enrolment_status = EnrolmentStatus.REJECTED
        profile.enrolment_rejection_reason = reason
        decision = "rejected"
    activity.step(f"Enrolment {decision}", entity="student_profile", entity_id=profile.id)
    activity.set_institution(inst)
    notify(profile.user_id, NotificationEvent.ENROLMENT_VERIFICATION_DECIDED,
           {"institution": inst.name, "decision": decision, "note": reason or ""})
    db.session.commit()
    return profile


def enrolment_queue(actor):
    q = active(StudentProfile).filter(StudentProfile.enrolment_status == EnrolmentStatus.PENDING)
    if actor.is_platform_admin:
        return q
    ids = set()
    for m in actor.memberships:
        if Permission.VERIFY_ENROLMENT in actor.membership_permissions(m):
            if m.college_id:
                ids.add(m.college_id)
            else:
                ids.update(cid for (cid,) in db.session.query(College.id).filter(College.university_id == m.university_id))
    return q.filter(StudentProfile.current_college_id.in_(ids or {-1}))


# ------------------------------------------------------- parental consent
def request_parental_consent(user):
    profile = get_profile(user)
    if not is_minor(profile):
        raise BusinessRuleViolation("Parental consent is needed only for students under 18")
    if not profile.guardian_email:
        raise ValidationError("Add the parent or guardian's email first", error_code="GUARDIAN_EMAIL_REQUIRED")
    code = otp_service.issue_otp(user, OtpPurpose.PARENT_CONSENT, target=profile.guardian_email,
                                 send_to_email=profile.guardian_email)
    db.session.commit()
    return otp_service.debug_payload(code)


def confirm_parental_consent(user, otp, declaration):
    profile = get_profile(user)
    if not declaration:
        raise ValidationError("The parent must accept the declaration", error_code="DECLARATION_REQUIRED")
    otp_service.verify_otp(user, OtpPurpose.PARENT_CONSENT, otp)
    profile.parental_consent_at = utcnow()
    db.session.add(ConsentRecord(user_id=user.id, notice_version=current_app.config["CONSENT_NOTICE_VERSION"],
                                 consented_at=utcnow(), is_parental=True, parent_identifier=profile.guardian_email,
                                 ip_address=_client_ip()))
    activity.step("Parental consent recorded", entity="student_profile", entity_id=profile.id)
    db.session.commit()
    return profile


# -------------------------------------------------------- academic records
def _records_locked(profile):
    """Editable until submission; afterwards only when returned for correction (M08-F05)."""
    in_progress = MigrationRequest.query.filter(MigrationRequest.student_id == profile.id,
                                                MigrationRequest.request_status.in_(list(LOCKING_STATUSES))).all()
    return bool(in_progress)


def save_academic_record(user, data, marksheet_file=None, record=None):
    profile = get_profile(user)
    if _records_locked(profile):
        raise BusinessRuleViolation("Academic records are locked while a request is in progress. "
                                    "They reopen if the request is returned for correction.",
                                    error_code="RECORDS_LOCKED")
    system = GradeSystem(data["grade_system"])
    if system == GradeSystem.PERCENTAGE and data.get("percentage") is None:
        raise ValidationError("Percentage is required for the PERCENTAGE system")
    if system in (GradeSystem.CGPA_10, GradeSystem.CGPA_4) and data.get("sgpa") is None:
        raise ValidationError("SGPA is required for CGPA systems")
    top = {GradeSystem.CGPA_10: 10, GradeSystem.CGPA_4: 4}.get(system)
    for key in ("sgpa", "cgpa"):
        if top and data.get(key) is not None and not 0 <= float(data[key]) <= top:
            raise ValidationError(f"{key.upper()} must be between 0 and {top}")
    existing = record or (active(AcademicRecord).filter_by(student_id=profile.id, semester=data["semester"],
                                                            is_current=True).first())
    version = 1
    if existing is not None:
        if existing.student_id != profile.id:
            raise PermissionDenied("Not your record")
        version = existing.version_no + 1 if existing.verification_status == VerificationStatus.VERIFIED else existing.version_no
        if existing.verification_status == VerificationStatus.VERIFIED:
            existing.is_current = False  # keep the verified version, new one needs re-verification
        else:
            for s in list(existing.subjects):
                db.session.delete(s)
            deactivate(existing)
            existing.is_current = False
    rec = AcademicRecord(student_id=profile.id, semester=data["semester"], grade_system=system,
                         sgpa=data.get("sgpa"), cgpa=data.get("cgpa"), percentage=data.get("percentage"),
                         credits_earned=data.get("credits_earned"), active_backlogs=data.get("active_backlogs", 0),
                         version_no=version, is_current=True)
    db.session.add(rec)
    db.session.flush()
    for s in data.get("subjects", []):
        db.session.add(SubjectResult(academic_record_id=rec.id, subject_code=s["subject_code"].strip().upper(),
                                     subject_name=s["subject_name"], credits=s["credits"], grade=s.get("grade"),
                                     marks_obtained=s.get("marks_obtained"), max_marks=s.get("max_marks"),
                                     passed=s.get("passed", True)))
    if rec.credits_earned is None and data.get("subjects"):
        rec.credits_earned = sum(float(s["credits"]) for s in data["subjects"] if s.get("passed", True))
    if marksheet_file is not None:
        doc = store_upload(marksheet_file, DocumentOwnerType.USER, user.id, DocumentType.SEMESTER_MARKSHEET,
                           description=f"Semester {rec.semester}")
        rec.marksheet_document_id = doc.id
    elif existing is not None:
        rec.marksheet_document_id = existing.marksheet_document_id
    activity.step("Academic record saved", entity="academic_record", entity_id=rec.id,
                  semester=rec.semester, version=version)
    db.session.commit()
    return rec


def record_view(rec):
    return {**rec.to_dict(), "subjects": [s.to_dict() for s in rec.subjects]}


def deactivate_record(user, rec):
    profile = get_profile(user)
    if rec.student_id != profile.id:
        raise PermissionDenied("Not your record")
    if _records_locked(profile):
        raise BusinessRuleViolation("Academic records are locked while a request is in progress",
                                    error_code="RECORDS_LOCKED")
    rec.is_current = False
    deactivate(rec)
    db.session.commit()
    return rec


def latest_cumulative(profile):
    """(grade_system, value) of the latest semester's cumulative score, for conversion."""
    recs = (active(AcademicRecord).filter_by(student_id=profile.id, is_current=True)
            .order_by(AcademicRecord.semester.desc()).all())
    if not recs:
        return None, None
    last = recs[0]
    if last.grade_system == GradeSystem.PERCENTAGE:
        values = [float(r.percentage) for r in recs if r.percentage is not None]
        return GradeSystem.PERCENTAGE, round(sum(values) / len(values), 2) if values else None
    value = last.cgpa if last.cgpa is not None else (
        sum(float(r.sgpa) for r in recs if r.sgpa is not None) / max(len([r for r in recs if r.sgpa is not None]), 1))
    return last.grade_system, round(float(value), 2)
