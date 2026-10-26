"""Seat allocation (M06, 9.2 M06-F16 / INT-04, 12.1 merit rounds and reservation).

* A seat is BLOCKED when a provisional offer is issued and FILLED on admission.
* Issuing an offer locks the vacancy-category rows (SELECT ... FOR UPDATE).
* No seat free -> WAITLISTED; the waitlist is ordered by merit, then submission.
* A reserved-category student takes a seat of their category first, then OPEN.
"""
from flask import current_app

from app.core.activity import activity
from app.core.utils import deadline_after_days, utcnow
from app.enums import (LetterType, MigrationStatus, NotificationEvent, OfferMode,
                       ReservationCategory, SeatState)
from app.extensions import db
from app.models import (MigrationRequest, MigrationWindow, Vacancy, VacancyCategory)
from app.services.notifications import notify
from app.core.records import active, category_available, vacancy_available


def _locked_categories(vacancy_id):
    return {c.category: c for c in
            active(VacancyCategory).filter_by(vacancy_id=vacancy_id).with_for_update().all()}


def _vacancy_for(req, lock=False):
    q = active(Vacancy).filter_by(offering_id=req.destination_offering_id, academic_year_id=req.academic_year_id,
                                   entry_semester=req.entry_semester)
    if lock:
        q = q.with_for_update()
    return q.first()


def _pick_category(req, cats):
    order = []
    if req.category != ReservationCategory.OPEN and req.student.category_verified:
        order.append(req.category)
    order.append(ReservationCategory.OPEN)
    for cat in order:
        row = cats.get(cat)
        if row is not None and category_available(row) > 0:
            return row
    return None


def try_allocate(req, transition):
    """Offer a seat to ``req`` if one is free; otherwise waitlist it. Returns True when offered."""
    vacancy = _vacancy_for(req, lock=True)
    row = _pick_category(req, _locked_categories(vacancy.id)) if vacancy else None
    if row is None:
        if req.request_status != MigrationStatus.WAITLISTED:
            transition(req, MigrationStatus.WAITLISTED, "No seat free; waitlisted by merit")
        recompute_waitlist(req)
        notify(req.student.user_id, NotificationEvent.WAITLISTED,
               {"reference": req.reference_no, "rank": req.waitlist_rank})
        return False
    row.blocked += 1
    req.vacancy_category_id = row.id
    req.seat_state = SeatState.BLOCKED
    req.waitlist_rank = None
    req.offer_issued_at = utcnow()
    req.offer_expires_at = deadline_after_days(current_app.config["OFFER_ACCEPTANCE_DAYS"])
    transition(req, MigrationStatus.PROVISIONAL_OFFER, f"Seat blocked in {row.category.value}")
    activity.step("Seat blocked for provisional offer", entity="vacancy_category", entity_id=row.id,
                  category=row.category, request_id=req.id)
    from app.services.letters import issue_letter
    issue_letter(req, LetterType.PROVISIONAL_OFFER)
    notify(req.student.user_id, NotificationEvent.OFFER_ISSUED,
           {"reference": req.reference_no, "destination": req.destination_college.name,
            "expires_on": req.offer_expires_at.date().isoformat()})
    if vacancy:
        recompute_waitlist_for_vacancy(vacancy)
    return True


def fill_seat(req):
    row = db.session.get(VacancyCategory, req.vacancy_category_id, with_for_update=True)
    if row is None or req.seat_state != SeatState.BLOCKED:
        return
    row.blocked = max(row.blocked - 1, 0)
    row.filled += 1
    req.seat_state = SeatState.FILLED
    activity.step("Seat filled", entity="vacancy_category", entity_id=row.id)


def release_seat(req, transition=None, promote=True):
    """Give the seat back (decline, expiry, cancellation) and offer it to the waitlist."""
    if not req.vacancy_category_id or req.seat_state is None:
        return
    row = db.session.get(VacancyCategory, req.vacancy_category_id, with_for_update=True)
    if req.seat_state == SeatState.BLOCKED:
        row.blocked = max(row.blocked - 1, 0)
    elif req.seat_state == SeatState.FILLED:
        row.filled = max(row.filled - 1, 0)
    activity.step("Seat released", entity="vacancy_category", entity_id=row.id, previous=req.seat_state)
    req.seat_state = None
    req.vacancy_category_id = None
    if promote and transition is not None:
        promote_waitlist(row.vacancy, transition)


def _allocation_open(vacancy):
    """Offers can be issued now: rolling courses, or merit courses after their round."""
    course = vacancy.offering.course
    if course.offer_mode == OfferMode.ROLLING:
        return True
    window = active(MigrationWindow).filter_by(course_id=course.id, academic_year_id=vacancy.academic_year_id).first()
    return window is not None and window.merit_round_completed_at is not None


def waitlist_query(vacancy):
    return (MigrationRequest.query.filter(
        MigrationRequest.destination_offering_id == vacancy.offering_id,
        MigrationRequest.academic_year_id == vacancy.academic_year_id,
        MigrationRequest.entry_semester == vacancy.entry_semester,
        MigrationRequest.request_status == MigrationStatus.WAITLISTED)
        .order_by(MigrationRequest.merit_score.desc().nullslast(), MigrationRequest.submitted_at.asc()))


def promote_waitlist(vacancy, transition):
    if vacancy is None or not _allocation_open(vacancy):
        return 0
    offered = 0
    while vacancy_available(vacancy) > 0:
        nxt = waitlist_query(vacancy).with_for_update(skip_locked=True).first()
        if nxt is None:
            break
        if not try_allocate(nxt, transition):
            break
        offered += 1
        db.session.flush()
        db.session.refresh(vacancy)
    recompute_waitlist_for_vacancy(vacancy)
    return offered


def recompute_waitlist_for_vacancy(vacancy):
    for rank, r in enumerate(waitlist_query(vacancy).all(), start=1):
        r.waitlist_rank = rank


def recompute_waitlist(req):
    vacancy = _vacancy_for(req)
    if vacancy is not None:
        recompute_waitlist_for_vacancy(vacancy)


def run_merit_round(window, transition):
    """At window close: offer seats in merit order for each offering of the course (12.1)."""
    if window.merit_round_completed_at is not None:
        return {"skipped": True}
    course = window.course
    pending = (MigrationRequest.query.filter(MigrationRequest.window_id == window.id,
                                             MigrationRequest.request_status.in_(
                                                 [MigrationStatus.ENTRY_APPROVED, MigrationStatus.WAITLISTED]))
               .order_by(MigrationRequest.merit_score.desc().nullslast(), MigrationRequest.submitted_at.asc())
               .all())
    window.merit_round_completed_at = utcnow()
    db.session.flush()
    offered = waitlisted = 0
    for req in pending:
        if try_allocate(req, transition):
            offered += 1
        else:
            waitlisted += 1
    university = course.university
    released = 0
    if university.release_unfilled_reserved_to_open and window.reserved_released_at is None:
        released = release_reserved_to_open(window, transition)
        window.reserved_released_at = utcnow()
    activity.step("Merit round completed", entity="migration_window", entity_id=window.id,
                  offered=offered, waitlisted=waitlisted, reserved_released=released)
    return {"offered": offered, "waitlisted": waitlisted, "reserved_released_to_open": released}


def release_reserved_to_open(window, transition):
    """Move unfilled reserved seats to OPEN when no student of that category is waiting."""
    moved = 0
    from app.models import CourseOffering
    vacancies = (active(Vacancy).join(CourseOffering, CourseOffering.id == Vacancy.offering_id)
                 .filter(CourseOffering.course_id == window.course_id,
                         Vacancy.academic_year_id == window.academic_year_id).all())
    for vac in vacancies:
        cats = _locked_categories(vac.id)
        waiting = {r.category for r in waitlist_query(vac).all()}
        open_row = cats.get(ReservationCategory.OPEN)
        for cat, row in cats.items():
            if cat == ReservationCategory.OPEN or cat in waiting or category_available(row) <= 0:
                continue
            spare = category_available(row)
            row.seats -= spare
            if open_row is None:
                open_row = VacancyCategory(vacancy_id=vac.id, category=ReservationCategory.OPEN, seats=0)
                db.session.add(open_row)
                db.session.flush()
            open_row.seats += spare
            moved += spare
        promote_waitlist(vac, transition)
    return moved


def university_of_institution(inst):
    from app.core.institutions import university_of
    return university_of(inst)
