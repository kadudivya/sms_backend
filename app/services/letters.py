"""Generated PDF letters with a QR verification code (12.2)."""
import io
import secrets
from datetime import datetime

import qrcode
from flask import current_app
from reportlab.lib.pagesizes import A4
from reportlab.lib.units import mm
from reportlab.lib.utils import ImageReader
from reportlab.pdfgen import canvas

from app.core.activity import activity
from app.core.storage import get_storage
from app.core.utils import IST, reference_number, utcnow
from app.enums import LetterStatus, LetterType
from app.extensions import db
from app.core.institutions import ref as institution_ref, university_of
from app.models import Document, Letter, LetterTemplate
from app.core.records import active, student_name

DEFAULT_TEMPLATES = {
    LetterType.NOC: ("No Objection Certificate",
                     "This is to certify that {student_name} (Enrolment No. {enrolment_number}), a student of "
                     "{source_name}, has applied for migration to {destination_name} for {course_name}, "
                     "semester {entry_semester}, in academic year {academic_year}. This institution has no "
                     "objection to the student applying. Request reference: {reference_no}."),
    LetterType.PROVISIONAL_OFFER: ("Provisional Offer of Admission",
                                   "{student_name} is offered provisional admission to {course_name}, semester "
                                   "{entry_semester}, at {destination_name} for academic year {academic_year}. "
                                   "Accept this offer by {offer_expires_on}. Admission is confirmed only after "
                                   "release by the current institution. {additional_papers}"),
    LetterType.ADMISSION_CONFIRMATION: ("Confirmation of Admission",
                                        "The admission of {student_name} to {course_name}, semester {entry_semester}, "
                                        "at {destination_name} is confirmed. Report with original documents by "
                                        "{originals_due_on}. {additional_papers}"),
    LetterType.REJECTION: ("Decision on Migration Request",
                           "The migration request {reference_no} of {student_name} to {destination_name} has not "
                           "been approved. Reason: {rejection_reason}. The student may appeal once within the "
                           "appeal period."),
    LetterType.RELEASE_CONFIRMATION: ("Release Confirmation",
                                      "{source_name} confirms the release of {student_name} (Enrolment No. "
                                      "{enrolment_number}) to join {destination_name}. Transfer certificate issued."
                                      " {migration_certificate_note}"),
}


class _Safe(dict):
    def __missing__(self, key):
        return ""


def _context(req):
    from app.models import EquivalenceDecision
    profile = req.student
    eq = EquivalenceDecision.query.filter_by(request_id=req.id).first()
    papers = ""
    if eq and eq.additional_papers:
        papers = "Additional papers to clear: " + ", ".join(map(str, eq.additional_papers)) + "."
    return _Safe(
        reference_no=req.reference_no, student_name=student_name(profile), enrolment_number=profile.enrolment_number or "",
        source_name=req.source_college.name, destination_name=req.destination_college.name,
        course_name=req.destination_offering.course.name, entry_semester=req.entry_semester,
        academic_year=req.academic_year.label,
        offer_expires_on=req.offer_expires_at.astimezone(IST).strftime("%d %b %Y") if req.offer_expires_at else "",
        originals_due_on=req.originals_due_at.astimezone(IST).strftime("%d %b %Y") if req.originals_due_at else "",
        additional_papers=papers, rejection_reason=req.rejection_reason or "",
        migration_certificate_note=("Migration certificate issued by the university."
                                    if req.migration_certificate_document_id else ""),
    )


def _template(letter_type, university_id):
    row = None
    if university_id:
        row = active(LetterTemplate).filter_by(letter_type=letter_type, university_id=university_id).first()
    if row is None:
        row = active(LetterTemplate).filter_by(letter_type=letter_type, university_id=None).first()
    if row is not None:
        return row.subject, row.body
    return DEFAULT_TEMPLATES[letter_type]


def _issuer(req, letter_type):
    from app.enums import MigrationStatus
    if letter_type in (LetterType.NOC, LetterType.RELEASE_CONFIRMATION):
        return req.source_college
    if letter_type == LetterType.REJECTION and req.rejected_at_status == MigrationStatus.SUBMITTED:
        return req.source_college  # NOC refused by the current college
    return req.destination_college


def _wrap(text, width_chars=95):
    words, lines, line = text.split(), [], ""
    for w in words:
        if len(line) + len(w) + 1 > width_chars:
            lines.append(line)
            line = w
        else:
            line = f"{line} {w}".strip()
    if line:
        lines.append(line)
    return lines


def render_pdf(inst, subject, body, reference_no, verify_url, signatory, designation):
    buf = io.BytesIO()
    pdf = canvas.Canvas(buf, pagesize=A4)
    width, height = A4
    y = height - 25 * mm
    pdf.setFont("Helvetica-Bold", 14)
    pdf.drawString(20 * mm, y, inst.name[:80])
    y -= 6 * mm
    pdf.setFont("Helvetica", 9)
    header = inst.letter_header or f"AISHE Code: {inst.aishe_code}"
    for line in _wrap(header, 110)[:3]:
        pdf.drawString(20 * mm, y, line)
        y -= 4.5 * mm
    pdf.line(20 * mm, y, width - 20 * mm, y)
    y -= 10 * mm
    pdf.setFont("Helvetica", 10)
    pdf.drawString(20 * mm, y, f"Ref: {reference_no}")
    pdf.drawRightString(width - 20 * mm, y, datetime.now(IST).strftime("Date: %d %b %Y"))
    y -= 12 * mm
    pdf.setFont("Helvetica-Bold", 12)
    pdf.drawCentredString(width / 2, y, subject)
    y -= 12 * mm
    pdf.setFont("Helvetica", 11)
    for line in _wrap(body):
        pdf.drawString(20 * mm, y, line)
        y -= 6 * mm
    y -= 15 * mm
    if inst.seal_document_id:
        seal = db.session.get(Document, inst.seal_document_id)
        try:
            pdf.drawImage(ImageReader(io.BytesIO(get_storage().read(seal.storage_key))), 20 * mm, y - 10 * mm,
                          width=28 * mm, height=28 * mm, mask="auto")
        except Exception:
            current_app.logger.warning("Seal image could not be drawn for institution %s", inst.id)
    pdf.drawString(width - 80 * mm, y, signatory or "Authorised Signatory")
    pdf.drawString(width - 80 * mm, y - 5 * mm, designation or "")
    qr = qrcode.make(verify_url)
    qr_buf = io.BytesIO()
    qr.save(qr_buf, format="PNG")
    qr_buf.seek(0)
    pdf.drawImage(ImageReader(qr_buf), width - 45 * mm, 15 * mm, width=28 * mm, height=28 * mm)
    pdf.setFont("Helvetica", 7)
    pdf.drawString(20 * mm, 15 * mm, f"Verify this letter: {verify_url}")
    if inst.letter_footer:
        pdf.drawString(20 * mm, 11 * mm, inst.letter_footer[:150])
    pdf.showPage()
    pdf.save()
    return buf.getvalue()


def issue_letter(req, letter_type, signatory=None):
    letter_type = LetterType(letter_type)
    inst = _issuer(req, letter_type)
    university = university_of(inst)  # templates are per university; None -> default template
    subject, body = _template(letter_type, university.id if university else None)
    text = body.format_map(_context(req))
    token = secrets.token_urlsafe(24)
    ref = reference_number(f"LTR-{letter_type.value[:3]}")
    base = current_app.config["PUBLIC_BASE_URL"].rstrip("/")
    verify_url = f"{base}{current_app.config['API_PREFIX']}/public/letters/{token}"
    user = signatory
    pdf = render_pdf(inst, subject, text, ref, verify_url, user.full_name if user else None,
                     user.designation if user else None)
    key = f"letters/{req.id}/{letter_type.value.lower()}_{token[:8]}.pdf"
    get_storage().save(key, pdf)
    for old in active(Letter).filter_by(request_id=req.id, letter_type=letter_type,
                                         letter_status=LetterStatus.VALID).all():
        old.letter_status = LetterStatus.CANCELLED
        old.cancelled_at = utcnow()
    letter = Letter(request_id=req.id, **institution_ref(inst), letter_type=letter_type, reference_no=ref,
                    qr_token=token, storage_key=key, signatory_name=user.full_name if user else None,
                    signatory_designation=user.designation if user else None, issued_at=utcnow())
    db.session.add(letter)
    db.session.flush()
    activity.step("Letter generated", entity="letter", entity_id=letter.id, letter_type=letter_type, reference=ref)
    return letter


def cancel_letters(req, letter_types=None):
    q = active(Letter).filter_by(request_id=req.id, letter_status=LetterStatus.VALID)
    for letter in q.all():
        if letter_types is None or letter.letter_type in letter_types:
            letter.letter_status = LetterStatus.CANCELLED
            letter.cancelled_at = utcnow()
