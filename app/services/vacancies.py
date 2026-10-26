"""Vacancy upload and correction by reservation category (M06, 12.1) and interview hours."""
from app.core.activity import activity
from app.core.errors import BusinessRuleViolation, ValidationError
from app.enums import Permission, ReservationCategory, Weekday
from app.extensions import db
from app.models import InterviewWorkingHours, MigrationWindow, Vacancy, VacancyCategory
from app.services.academics import window_is_open
from app.services.directory import institution_admins
from app.services.notifications import notify
from app.core.records import activate, active, category_available, deactivate, is_active, vacancy_available, vacancy_total_seats


def _parse_categories(categories):
    if not categories:
        raise ValidationError("Give seats for at least one category (OPEN if there is no split)")
    parsed = {}
    for key, seats in categories.items():
        cat = ReservationCategory(key)
        if not isinstance(seats, int) or seats < 0:
            raise ValidationError(f"Seats for {cat.value} must be a whole number of 0 or more",
                                  error_code="INVALID_VACANCY")
        parsed[cat] = seats
    return parsed


def upsert_vacancy(actor, offering, year, entry_semester, categories):
    college = offering.college
    actor.require(Permission.MANAGE_VACANCIES, college)
    if not is_active(offering):
        raise BusinessRuleViolation("The course offering is inactive", error_code="OFFERING_INACTIVE")
    course = offering.course
    if entry_semester < 2 or entry_semester > course.total_semesters:
        raise ValidationError(f"Entry semester must be between 2 and {course.total_semesters}")
    if year.university_id not in (None, course.university_id):
        raise ValidationError("The academic year belongs to another university")
    parsed = _parse_categories(categories)
    total = sum(parsed.values())
    if total > offering.sanctioned_intake:
        raise ValidationError(f"Total vacancies ({total}) exceed the sanctioned intake ({offering.sanctioned_intake})",
                              error_code="VACANCY_EXCEEDS_INTAKE")
    vacancy = (Vacancy.query.filter_by(offering_id=offering.id, academic_year_id=year.id,
                                       entry_semester=entry_semester).with_for_update().first())
    created = vacancy is None
    if created:
        vacancy = Vacancy(offering_id=offering.id, academic_year_id=year.id, entry_semester=entry_semester)
        db.session.add(vacancy)
        db.session.flush()
    else:
        activate(vacancy)
    existing = {c.category: c for c in
                VacancyCategory.query.filter_by(vacancy_id=vacancy.id).with_for_update().all()}
    reduced = False
    for cat in set(existing) | set(parsed):
        new_seats = parsed.get(cat, 0)
        row = existing.get(cat)
        if row is None:
            if new_seats:
                db.session.add(VacancyCategory(vacancy_id=vacancy.id, category=cat, seats=new_seats))
            continue
        if new_seats < row.blocked + row.filled:
            raise BusinessRuleViolation(
                f"{cat.value}: cannot go below {row.blocked + row.filled} seats already offered or filled",
                error_code="VACANCY_BELOW_COMMITTED")
        if new_seats < row.seats:
            reduced = True
        activity.change("vacancy_category", row.id, "seats", row.seats, new_seats)
        row.seats = new_seats
        if new_seats == 0 and row.blocked == 0 and row.filled == 0 and cat not in parsed:
            deactivate(row)
        else:
            activate(row)
    vacancy.discrepancy_flag = False
    db.session.flush()
    activity.step("Vacancy " + ("uploaded" if created else "corrected"), entity="vacancy", entity_id=vacancy.id,
                  total=total)
    activity.set_institution(college)
    if reduced and not created:
        window = active(MigrationWindow).filter_by(course_id=course.id, academic_year_id=year.id).first()
        if window and window_is_open(window):
            for u in institution_admins(course.university):
                from app.enums import NotificationEvent
                notify(u, NotificationEvent.GENERIC,
                       {"subject": "Vacancy reduced during an open window",
                        "message": f"{college.name} reduced vacancies for {course.name} "
                                   f"(semester {entry_semester})."})
    db.session.commit()
    return vacancy


def vacancy_view(v):
    cats = [{"category": c.category.value, "seats": c.seats, "blocked": c.blocked, "filled": c.filled,
             "available": category_available(c)} for c in v.categories if is_active(c)]
    return {
        "id": v.id, "offering_id": v.offering_id, "college_id": v.offering.college_id,
        "college_name": v.offering.college.name, "course_id": v.offering.course_id,
        "course_name": v.offering.course.name, "academic_year_id": v.academic_year_id,
        "academic_year": v.academic_year.label, "entry_semester": v.entry_semester,
        "total_seats": vacancy_total_seats(v), "available": vacancy_available(v), "categories": cats,
        "discrepancy_flag": v.discrepancy_flag, "status": v.status.value,
        "updated_at": v.updated_at.isoformat(),
    }


def find_vacancy(offering_id, academic_year_id, entry_semester, lock=False):
    q = active(Vacancy).filter_by(offering_id=offering_id, academic_year_id=academic_year_id,
                                   entry_semester=entry_semester)
    if lock:
        q = q.with_for_update()
    return q.first()


def set_working_hours(actor, college, rows):
    actor.require(Permission.MANAGE_INTERVIEW_HOURS, college)
    for r in rows:
        if r["end_time"] <= r["start_time"]:
            raise ValidationError(f"{r['weekday']}: end time must be after start time")
        if r.get("slot_minutes", 30) < 10:
            raise ValidationError("Slots must be at least 10 minutes")
    for old in active(InterviewWorkingHours).filter_by(college_id=college.id).all():
        deactivate(old)
    created = []
    for r in rows:
        row = InterviewWorkingHours(college_id=college.id, weekday=Weekday(r["weekday"]), start_time=r["start_time"],
                                    end_time=r["end_time"], slot_minutes=r.get("slot_minutes", 30))
        db.session.add(row)
        created.append(row)
    activity.step("Interview working hours replaced", entity="college", entity_id=college.id, rows=len(rows))
    db.session.commit()
    return created
