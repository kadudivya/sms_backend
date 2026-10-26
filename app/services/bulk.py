"""Excel bulk uploads (12.2): vacancies, courses, syllabus subjects, course equivalences.

Each row is checked and saved on its own; valid rows are kept and an error
file lists the rest with the reason.
"""
import io

from flask import current_app
from openpyxl import Workbook
from openpyxl.styles import Font

from app.core.activity import activity
from app.core.errors import AppError, PermissionDenied, ValidationError
from app.core.storage import get_storage
from app.enums import BulkUploadType, Permission, ReservationCategory
from app.extensions import db
from app.core.institutions import find_by_aishe, kind_of, ref
from app.enums import InstitutionKind
from app.models import AcademicYear, BulkUploadJob, Course, CourseOffering, Syllabus
from app.services import academics, vacancies
from app.core.spreadsheets import read_rows
from app.core.records import active

CATEGORY_COLUMNS = [c.value for c in ReservationCategory]

TEMPLATES = {
    BulkUploadType.VACANCIES: ["course_code", "academic_year", "entry_semester"] + CATEGORY_COLUMNS,
    BulkUploadType.COURSES: ["code", "name", "level", "discipline", "duration_years", "total_semesters",
                             "offer_mode", "interview_required"],
    BulkUploadType.SYLLABUS_SUBJECTS: ["semester", "code", "name", "credits", "is_elective"],
    BulkUploadType.COURSE_EQUIVALENCES: ["course_code", "source_course_name", "source_university_aishe_code",
                                         "source_course_level"],
}
EXAMPLES = {
    BulkUploadType.VACANCIES: ["BSC-CS", "2026-27", 3, 10, 2, 1, 3, 1, 0],
    BulkUploadType.COURSES: ["BSC-CS", "B.Sc Computer Science", "UNDERGRADUATE", "Computer Science", 3, 6,
                             "MERIT_ROUND", "false"],
    BulkUploadType.SYLLABUS_SUBJECTS: [1, "CS101", "Programming in C", 4, "false"],
    BulkUploadType.COURSE_EQUIVALENCES: ["BSC-CS", "BCA", "", "UNDERGRADUATE"],
}


def template_bytes(upload_type):
    upload_type = BulkUploadType(upload_type)
    wb = Workbook()
    ws = wb.active
    ws.title = upload_type.value.lower()
    ws.append(TEMPLATES[upload_type])
    for cell in ws[1]:
        cell.font = Font(bold=True)
    ws.append(EXAMPLES[upload_type])
    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


def _bool(v):
    return str(v).strip().lower() in ("1", "true", "yes", "y")


def _int(v, field):
    try:
        return int(float(v))
    except (TypeError, ValueError) as exc:
        raise ValidationError(f"{field} must be a whole number") from exc


def _row_vacancy(actor, institution, row):
    course = active(Course).filter_by(university_id=getattr(institution, "university_id", None),
                                       code=str(row.get("course_code", "")).strip().upper()).first()
    if course is None:
        raise ValidationError("Unknown course_code for your university")
    offering = active(CourseOffering).filter_by(college_id=institution.id, course_id=course.id).first()
    if offering is None:
        raise ValidationError("Your college does not offer this course")
    year = active(AcademicYear).filter(AcademicYear.label == str(row.get("academic_year", "")).strip(),
                                        (AcademicYear.university_id == course.university_id) |
                                        AcademicYear.university_id.is_(None)).first()
    if year is None:
        raise ValidationError("Unknown academic_year")
    cats = {c: _int(row[c], c) for c in CATEGORY_COLUMNS if row.get(c) not in (None, "")}
    vacancies.upsert_vacancy(actor, offering, year, _int(row.get("entry_semester"), "entry_semester"), cats)


def _row_course(actor, institution, row):
    academics.create_course(actor, institution, {
        "code": str(row.get("code", "")), "name": str(row.get("name", "")), "level": str(row.get("level", "")).upper(),
        "discipline": row.get("discipline"), "duration_years": _int(row.get("duration_years"), "duration_years"),
        "total_semesters": _int(row.get("total_semesters"), "total_semesters"),
        "offer_mode": (row.get("offer_mode") or "MERIT_ROUND").upper(),
        "interview_required": _bool(row.get("interview_required")), "confirm_similar": True})


def _row_equivalence(actor, institution, row):
    course = active(Course).filter_by(university_id=institution.id,
                                       code=str(row.get("course_code", "")).strip().upper()).first()
    if course is None:
        raise ValidationError("Unknown course_code")
    src_uni = None
    if row.get("source_university_aishe_code"):
        src = find_by_aishe(str(row["source_university_aishe_code"]), InstitutionKind.UNIVERSITY)
        if src is None:
            raise ValidationError("Unknown source university AISHE code")
        src_uni = src.id
    academics.create_equivalence(actor, course, {"source_course_name": str(row.get("source_course_name", "")),
                                                 "source_university_id": src_uni,
                                                 "source_course_level": (row.get("source_course_level") or None)})


def process(actor, upload_type, institution, file_storage, syllabus_id=None):
    upload_type = BulkUploadType(upload_type)
    needs = {BulkUploadType.VACANCIES: Permission.MANAGE_VACANCIES,
             BulkUploadType.COURSES: Permission.MANAGE_COURSES,
             BulkUploadType.SYLLABUS_SUBJECTS: Permission.MANAGE_SYLLABUS,
             BulkUploadType.COURSE_EQUIVALENCES: Permission.MANAGE_ACADEMIC_RULES}[upload_type]
    actor.require(Permission.BULK_UPLOAD, institution)
    actor.require(needs, institution)
    rows = read_rows(file_storage)
    if len(rows) > current_app.config["BULK_UPLOAD_MAX_ROWS"]:
        raise ValidationError(f"At most {current_app.config['BULK_UPLOAD_MAX_ROWS']} rows per file")
    job = BulkUploadJob(upload_type=upload_type, **ref(institution), filename=file_storage.filename,
                        total_rows=len(rows), errors=[])
    db.session.add(job)
    db.session.commit()
    job_id = job.id
    errors, success = [], 0
    if upload_type == BulkUploadType.SYLLABUS_SUBJECTS:
        syl = db.session.get(Syllabus, syllabus_id) if syllabus_id else None
        if syl is None or syl.university_id != institution.id:
            raise ValidationError("syllabus_id of one of your draft syllabi is required")
        subjects = []
        for n, row in enumerate(rows, start=2):
            try:
                subjects.append({"semester": _int(row.get("semester"), "semester"), "code": str(row.get("code", "")),
                                 "name": str(row.get("name", "")), "credits": float(row.get("credits") or 0),
                                 "is_elective": _bool(row.get("is_elective"))})
            except AppError as exc:
                errors.append({"row": n, "error": exc.message, **{k: row.get(k) for k in TEMPLATES[upload_type]}})
        if not errors:
            academics.update_syllabus(actor, syl, {"subjects": subjects})
            success = len(subjects)
    else:
        handler = {BulkUploadType.VACANCIES: _row_vacancy, BulkUploadType.COURSES: _row_course,
                   BulkUploadType.COURSE_EQUIVALENCES: _row_equivalence}[upload_type]
        for n, row in enumerate(rows, start=2):
            try:
                handler(actor, institution, row)
                success += 1
            except (AppError, PermissionDenied) as exc:
                db.session.rollback()
                errors.append({"row": n, "error": exc.message, **{k: row.get(k) for k in TEMPLATES[upload_type]}})
    job = db.session.get(BulkUploadJob, job_id)
    job.success_rows, job.error_rows, job.errors = success, len(errors), errors[:500]
    if errors:
        wb = Workbook()
        ws = wb.active
        headers = ["row"] + TEMPLATES[upload_type] + ["error"]
        ws.append(headers)
        for e in errors:
            ws.append([e.get(h) for h in headers])
        buf = io.BytesIO()
        wb.save(buf)
        key = f"bulk/{kind_of(institution).value.lower()}-{institution.id}/{job.uuid}_errors.xlsx"
        get_storage().save(key, buf.getvalue())
        job.error_file_key = key
    activity.step("Bulk upload processed", entity="bulk_upload_job", entity_id=job.id, success=success,
                  errors=len(errors))
    db.session.commit()
    return job
