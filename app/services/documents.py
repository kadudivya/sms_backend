"""Uploading, versioning, verifying and authorising access to documents (M09)."""
import uuid

from sqlalchemy import func

from app.core.activity import activity
from app.core.errors import BusinessRuleViolation, NotFound, PermissionDenied, ValidationError
from app.core.storage import get_storage, signed_download_url, validate_upload
from app.core.utils import utcnow
from app.enums import (DocumentOwnerType, DocumentType, NotificationEvent, Permission,
                       VerificationStatus)
from app.extensions import db
from app.core.institutions import ref
from app.models import (Appeal, College, Document, MigrationRequest, RegistrationRequest,
                        StudentProfile, SupportTicket, University)
from app.services.notifications import notify
from app.core.records import active, is_active

# Types a student may hold several of at once (one per semester etc.)
MULTI_INSTANCE_TYPES = {DocumentType.SEMESTER_MARKSHEET, DocumentType.CATEGORY_CERTIFICATE,
                        DocumentType.OTHER, DocumentType.APPEAL_EVIDENCE,
                        DocumentType.SUPPORT_ATTACHMENT}


def _ext(filename):
    return filename.rsplit(".", 1)[-1].lower()


def store_upload(file_storage, owner_type, owner_id, doc_type, *, institution=None,
                 replace_document=None, description=None):
    """``institution``: the University / College uploading it, if any."""
    data, mime, checksum = validate_upload(file_storage)
    owner_type, doc_type = DocumentOwnerType(owner_type), DocumentType(doc_type)
    group_uuid, version = str(uuid.uuid4()), 1
    previous = replace_document
    if previous is None and doc_type not in MULTI_INSTANCE_TYPES:
        previous = (active(Document).filter_by(owner_type=owner_type, owner_id=owner_id,
                                                doc_type=doc_type, is_current=True).first())
    if previous is not None:
        if previous.owner_type != owner_type or previous.owner_id != owner_id:
            raise ValidationError("The document to replace belongs to someone else")
        group_uuid, version = previous.group_uuid, previous.version_no + 1
        previous.is_current = False
    if owner_type == DocumentOwnerType.MIGRATION_REQUEST:
        _enforce_request_quota(owner_id, len(data))
    key = f"{owner_type.value.lower()}/{owner_id}/{doc_type.value.lower()}/{uuid.uuid4()}.{_ext(file_storage.filename)}"
    get_storage().save(key, data)
    doc = Document(group_uuid=group_uuid, owner_type=owner_type, owner_id=owner_id, doc_type=doc_type,
                   version_no=version, is_current=True, storage_key=key,
                   original_filename=file_storage.filename[:300], mime_type=mime,
                   size_bytes=len(data), checksum_sha256=checksum,
                   **ref(institution, "uploaded_by_"), description=description,
                   verification_status=VerificationStatus.PENDING)
    db.session.add(doc)
    db.session.flush()
    activity.step("Stored document", entity="document", entity_id=doc.id,
                  doc_type=doc_type, version=version, size=len(data))
    return doc


def store_generated(data, filename, mime, owner_type, owner_id, doc_type, institution=None):
    from hashlib import sha256
    key = f"{DocumentOwnerType(owner_type).value.lower()}/{owner_id}/generated/{uuid.uuid4()}_{filename}"
    get_storage().save(key, data)
    doc = Document(group_uuid=str(uuid.uuid4()), owner_type=owner_type, owner_id=owner_id,
                   doc_type=doc_type, storage_key=key, original_filename=filename, mime_type=mime,
                   size_bytes=len(data), checksum_sha256=sha256(data).hexdigest(),
                   **ref(institution, "uploaded_by_"),
                   verification_status=VerificationStatus.VERIFIED)
    db.session.add(doc)
    db.session.flush()
    return doc


def _enforce_request_quota(request_id, incoming):
    from flask import current_app
    used = (db.session.query(func.coalesce(func.sum(Document.size_bytes), 0))
            .filter(Document.owner_type == DocumentOwnerType.MIGRATION_REQUEST,
                    Document.owner_id == request_id, Document.is_current.is_(True)).scalar())
    if used + incoming > current_app.config["MAX_REQUEST_DOCUMENTS_BYTES"]:
        raise BusinessRuleViolation("This request has reached its document size limit",
                                    error_code="REQUEST_STORAGE_FULL")


def student_documents(student_user_id, request_id=None):
    q = active(Document).filter(Document.is_current.is_(True))
    conds = [db.and_(Document.owner_type == DocumentOwnerType.USER, Document.owner_id == student_user_id)]
    if request_id:
        conds.append(db.and_(Document.owner_type == DocumentOwnerType.MIGRATION_REQUEST,
                             Document.owner_id == request_id))
    return q.filter(db.or_(*conds)).order_by(Document.created_at.desc())


# --------------------------------------------------------------- access
def _institutions_for_request(req):
    return [i for i in (req.source_college, req.destination_college) if i is not None]


def can_access(actor, doc):
    if actor.is_platform_admin:
        return True
    user = actor.user
    ot = doc.owner_type
    if ot == DocumentOwnerType.USER:
        if doc.owner_id == user.id:
            return True
        profile = StudentProfile.query.filter_by(user_id=doc.owner_id).first()
        if profile is None:
            return False
        insts = [profile.current_college] if profile.current_college else []
        for req in MigrationRequest.query.filter_by(student_id=profile.id):
            insts.extend(_institutions_for_request(req))
        return any(actor.can_view_institution(i) for i in insts)
    if ot == DocumentOwnerType.MIGRATION_REQUEST:
        req = db.session.get(MigrationRequest, doc.owner_id)
        if req is None:
            return False
        if req.student.user_id == user.id:
            return True
        return any(actor.can_view_institution(i) for i in _institutions_for_request(req))
    if ot in (DocumentOwnerType.UNIVERSITY, DocumentOwnerType.COLLEGE):
        inst = db.session.get(University if ot == DocumentOwnerType.UNIVERSITY else College, doc.owner_id)
        return inst is not None and actor.can_view_institution(inst)
    if ot == DocumentOwnerType.REGISTRATION_REQUEST:
        reg = db.session.get(RegistrationRequest, doc.owner_id)
        if reg is None:
            return False
        if reg.requester_id == user.id:
            return True
        if reg.approver_university_id:
            return actor.can(Permission.APPROVE_COLLEGE_REGISTRATION,
                             db.session.get(University, reg.approver_university_id))
        return False
    if ot == DocumentOwnerType.SUPPORT_TICKET:
        ticket = db.session.get(SupportTicket, doc.owner_id)
        return ticket is not None and ticket.raised_by_id == user.id
    if ot == DocumentOwnerType.APPEAL:
        appeal = db.session.get(Appeal, doc.owner_id)
        if appeal is None:
            return False
        req = db.session.get(MigrationRequest, appeal.request_id)
        return req.student.user_id == user.id or actor.can_view_institution(req.destination_university or req.destination_college)
    return False


def require_access(actor, doc):
    if not can_access(actor, doc):
        raise PermissionDenied("You cannot open this document")


def download_link(actor, doc):
    require_access(actor, doc)
    activity.step("Issued signed download link", entity="document", entity_id=doc.id)
    return signed_download_url(doc.storage_key, doc.original_filename, doc.mime_type)


def get_document(doc_id):
    doc = db.session.get(Document, doc_id)
    if doc is None or not is_active(doc):
        raise NotFound("Document not found")
    return doc


def verify_document(actor, doc, decision, reason=None):
    decision = VerificationStatus(decision)
    if decision == VerificationStatus.PENDING:
        raise ValidationError("Decision must be VERIFIED, REJECTED or CORRECTION_REQUESTED")
    if decision != VerificationStatus.VERIFIED and not reason:
        raise ValidationError("A reason is required when a document is not verified")
    # who verifies: the destination college of the request, or the current college for profile docs
    insts = []
    student_user_id = None
    if doc.owner_type == DocumentOwnerType.MIGRATION_REQUEST:
        req = db.session.get(MigrationRequest, doc.owner_id)
        insts = [req.destination_college]
        student_user_id = req.student.user_id
    elif doc.owner_type == DocumentOwnerType.USER:
        profile = StudentProfile.query.filter_by(user_id=doc.owner_id).first()
        student_user_id = doc.owner_id
        if profile:
            reqs = MigrationRequest.query.filter_by(student_id=profile.id).all()
            insts = [r.destination_college for r in reqs]
            if profile.current_college:
                insts.append(profile.current_college)
    if not any(actor.can(Permission.VERIFY_DOCUMENTS, i) or actor.can(Permission.VERIFY_ENROLMENT, i)
               for i in insts):
        raise PermissionDenied("You cannot verify this document")
    old = doc.verification_status
    doc.verification_status = decision
    doc.verified_by_id = actor.user.id
    doc.verified_at = utcnow()
    doc.rejection_reason = reason
    activity.change("document", doc.id, "verification_status", old, decision)
    activity.step("Document decided", entity="document", entity_id=doc.id, decision=decision)
    if student_user_id:
        notify(student_user_id, NotificationEvent.DOCUMENT_DECIDED,
               {"document": doc.doc_type.value.replace("_", " ").title(),
                "decision": decision.value.replace("_", " ").lower(), "note": reason or ""})
    return doc
