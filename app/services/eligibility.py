"""Grade conversion (7.2) and the automatic eligibility check (M12, INT-05)."""
from app.core.activity import activity
from app.core.errors import ValidationError
from app.enums import ConversionSource, EligibilityResult, GradeSystem
from app.extensions import db
from app.models import AcademicRecord, GradeConversion
from app.services.academics import (accepts_source_course, evaluate_formula, rule_in_force, scheme_for)
from app.services.students import latest_cumulative
from app.core.records import active


def conversion_options(req):
    """What the student must supply for conversion: nothing, nothing (formula), or a certificate."""
    profile = req.student
    system, value = latest_cumulative(profile)
    if system is None:
        return {"needs": "ACADEMIC_RECORDS", "grade_system": None, "value": None}
    if system == GradeSystem.PERCENTAGE:
        return {"needs": "NOTHING", "source": ConversionSource.SAME_SYSTEM.value, "grade_system": system.value,
                "value": value, "converted_percentage": value}
    source_univ = req.source_university_id
    scheme = scheme_for(source_univ, system, profile.batch_year) if source_univ and profile.batch_year else None
    if scheme and scheme.formula_expr:
        return {"needs": "NOTHING", "source": ConversionSource.FORMULA.value, "grade_system": system.value,
                "value": value, "converted_percentage": evaluate_formula(scheme.formula_expr, value),
                "grading_scheme_id": scheme.id, "formula": scheme.formula_expr,
                "label": "Per the source university's published formula"}
    return {"needs": "CERTIFICATE", "source": ConversionSource.CERTIFICATE.value, "grade_system": system.value,
            "value": value}


def set_conversion(req, certificate_doc=None, certified_percentage=None):
    """Store the conversion; a typed percentage alone is never accepted (7.2)."""
    opts = conversion_options(req)
    if opts["needs"] == "ACADEMIC_RECORDS":
        raise ValidationError("Add your semester results first", error_code="ACADEMIC_RECORDS_REQUIRED")
    conv = GradeConversion.query.filter_by(request_id=req.id).first() or GradeConversion(request_id=req.id)
    conv.source_grade_system = GradeSystem(opts["grade_system"])
    conv.source_value = opts["value"]
    if opts["needs"] == "CERTIFICATE":
        if certificate_doc is None or certified_percentage is None:
            raise ValidationError("Upload the source university's conversion certificate and enter the "
                                  "percentage it states", error_code="CONVERSION_CERTIFICATE_REQUIRED")
        if not 0 <= float(certified_percentage) <= 100:
            raise ValidationError("Percentage must be between 0 and 100")
        conv.conversion_source = ConversionSource.CERTIFICATE
        conv.converted_percentage = certified_percentage
        conv.certificate_document_id = certificate_doc.id
        conv.grading_scheme_id = None
    else:
        conv.conversion_source = ConversionSource(opts["source"])
        conv.converted_percentage = opts["converted_percentage"]
        conv.grading_scheme_id = opts.get("grading_scheme_id")
        conv.certificate_document_id = None
    db.session.add(conv)
    db.session.flush()
    req.merit_score = conv.converted_percentage
    activity.step("Grade conversion recorded", entity="grade_conversion", entity_id=conv.id,
                  source=conv.conversion_source, converted=conv.converted_percentage)
    return conv


def run_check(req, on_date=None):
    """Sets eligibility_result + notes. Missing data gives PENDING, never a pass (M12-F05)."""
    if req.eligibility_result == EligibilityResult.OVERRIDDEN:
        return req.eligibility_result
    profile = req.student
    course = req.destination_offering.course
    on_date = on_date or (req.submitted_at.date() if req.submitted_at else None)
    from app.core.utils import today_ist
    rule = rule_in_force(course.id, on_date or today_ist())
    notes, missing = [], []
    conv = GradeConversion.query.filter_by(request_id=req.id).first()
    records = active(AcademicRecord).filter_by(student_id=profile.id, is_current=True).all()
    percentage = float(conv.converted_percentage) if conv and conv.converted_percentage is not None else None
    if percentage is None:
        missing.append("converted percentage")
    if not records:
        missing.append("academic records")
    failed = False
    if rule is not None:
        req.eligibility_rule_id = rule.id
        if rule.min_percentage is not None and percentage is not None and percentage < float(rule.min_percentage):
            failed = True
            notes.append(f"Percentage {percentage} is below the minimum {float(rule.min_percentage)}")
        completed = len({r.semester for r in records})
        if rule.min_completed_semesters is not None and completed < rule.min_completed_semesters:
            failed = True
            notes.append(f"{completed} semesters completed; {rule.min_completed_semesters} required")
        backlogs = max((r.active_backlogs for r in records), default=0)
        if rule.max_active_backlogs is not None and backlogs > rule.max_active_backlogs:
            failed = True
            notes.append(f"{backlogs} active backlogs; at most {rule.max_active_backlogs} allowed")
        need = (rule.min_credits_by_entry_semester or {}).get(str(req.entry_semester))
        if need is not None:
            credits = sum(float(r.credits_earned or 0) for r in records)
            if credits < float(need):
                failed = True
                notes.append(f"{credits} credits earned; {need} needed to enter semester {req.entry_semester}")
        if rule.same_discipline_required:
            if not profile.current_discipline or not course.discipline:
                missing.append("discipline")
            elif profile.current_discipline.strip().lower() != course.discipline.strip().lower():
                failed = True
                notes.append("Different discipline; the course requires the same discipline")
    else:
        notes.append("No eligibility rule configured; destination decides manually")
    if req.window and req.entry_semester not in (req.window.entry_semesters or []):
        failed = True
        notes.append(f"Entry into semester {req.entry_semester} is not open in this window")
    accepted = accepts_source_course(course, profile.current_course_name or "", req.source_university_id)
    manual = rule is None or accepted is None
    if accepted is False:
        manual = True
        notes.append(f"{profile.current_course_name} is not on the accepted source course list; manual review")
    if failed:
        result = EligibilityResult.INELIGIBLE
    elif missing:
        result = EligibilityResult.PENDING
        notes.append("Missing: " + ", ".join(missing))
    elif manual:
        result = EligibilityResult.MANUAL_REVIEW
    else:
        result = EligibilityResult.ELIGIBLE
    activity.change("migration_request", req.id, "eligibility_result", req.eligibility_result, result)
    req.eligibility_result = result
    req.eligibility_notes = notes
    activity.step("Eligibility checked", entity="migration_request", entity_id=req.id, result=result)
    return result
