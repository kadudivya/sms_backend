"""Branches of the workflow: merit rounds, NOC refusal + appeal, interviews, jobs, isolation, imports."""
import io
from datetime import datetime, timedelta, timezone

from openpyxl import Workbook

from app.core.utils import IST, today_ist, utcnow
from app.extensions import db
from app.models import Interview, MigrationRequest, MigrationWindow, UserActivity
from app.services.jobs import JOBS

from .conftest import PASSWORD, login, pdf_file
from .helpers import build_world, make_student, verify_email


def _verified_student(api, w, **kw):
    tok = make_student(api, w, **kw)
    _, q = api.get("/enrolment-verifications", token=w.ca_a, expect=200)
    for row in q["data"]:
        api.post(f"/enrolment-verifications/{row['id']}/decision", token=w.ca_a, json={"approve": True}, expect=200)
    return tok


def _submit(api, w, tok):
    _, body = api.post("/migration-requests", token=tok, expect=201, json={"offering_id": w.offering_id,
                                                                            "entry_semester": 3})
    rid = body["data"]["id"]
    api.put(f"/migration-requests/{rid}/grade-conversion", token=tok, json={}, expect=200)
    api.post(f"/migration-requests/{rid}/submit", token=tok, expect=200)
    return rid


def _to_recommended(api, w, rid, interview=False):
    api.post(f"/migration-requests/{rid}/noc", token=w.ca_a, json={"approve": True}, expect=200)
    api.post(f"/migration-requests/{rid}/start-review", token=w.ca_b, expect=200)
    _, body = api.get(f"/migration-requests/{rid}", token=w.ca_b, expect=200)
    d = body["data"]
    for item in d["checklist"]:
        for doc in item["documents"]:
            if doc["verification_status"] != "VERIFIED":
                api.post(f"/documents/{doc['id']}/verify", token=w.ca_b, json={"decision": "VERIFIED"}, expect=200)
    api.put(f"/migration-requests/{rid}/credits", token=w.ca_b, expect=200,
            json={"decisions": [{"id": c["id"], "decision": "ACCEPTED"} for c in d["credits"]]})
    if not interview:
        api.post(f"/migration-requests/{rid}/recommend", token=w.ca_b, json={}, expect=200)


def _status(api, tok, rid):
    _, body = api.get(f"/migration-requests/{rid}", token=tok, expect=200)
    return body["data"]


def test_merit_round_offers_highest_merit_first(api, app):
    w = build_world(api, offer_mode="MERIT_ROUND")
    api.put("/vacancies", token=w.ca_b, expect=200, json={"offering_id": w.offering_id, "academic_year_id": w.year_id,
                                                           "entry_semester": 3, "categories": {"OPEN": 1}})
    low = _verified_student(api, w, email="low@s.in", mobile="9000000001", abc="100000000001", sgpa=7.0)
    high = _verified_student(api, w, email="high@s.in", mobile="9000000002", abc="100000000002", sgpa=9.0)
    rid_low, rid_high = _submit(api, w, low), _submit(api, w, high)
    for rid in (rid_low, rid_high):
        _to_recommended(api, w, rid)
        api.post(f"/migration-requests/{rid}/approve-entry", token=w.ua_b, json={"is_eligible": True}, expect=200)
    assert _status(api, low, rid_low)["request_status"] == "ENTRY_APPROVED"
    window = db.session.get(MigrationWindow, w.window_id)
    window.opens_on, window.closes_on = today_ist() - timedelta(days=10), today_ist() - timedelta(days=1)
    db.session.commit()
    result = JOBS["merit_rounds"]()
    assert result["windows"][w.window_id]["offered"] == 1
    assert _status(api, high, rid_high)["request_status"] == "PROVISIONAL_OFFER"
    low_detail = _status(api, low, rid_low)
    assert low_detail["request_status"] == "WAITLISTED" and low_detail["waitlist_rank"] == 1


def test_noc_refusal_then_appeal_upheld_by_source_university(api):
    w = build_world(api)
    tok = _verified_student(api, w)
    rid = _submit(api, w, tok)
    _, body = api.post(f"/migration-requests/{rid}/noc", token=w.ca_a, expect=422, json={"approve": False})
    assert body["errors"][0]["code"] == "VALIDATION_ERROR"
    api.post(f"/migration-requests/{rid}/noc", token=w.ca_a, expect=200,
             json={"approve": False, "refusal_reason": "DUES_PENDING", "note": "Library fine"})
    assert _status(api, tok, rid)["request_status"] == "REJECTED"
    _, body = api.post(f"/migration-requests/{rid}/appeal", token=tok, expect=201,
                       json={"reason": "The library fine was paid on the same day; receipt attached."})
    appeal_id = body["data"]["id"]
    api.post(f"/migration-requests/{rid}/appeal", token=tok, expect=422,
             json={"reason": "Trying a second appeal on the same request."})
    # Destination university cannot decide an appeal against a NOC refusal; source university can
    api.post(f"/migration-requests/appeals/{appeal_id}/decision", token=w.ua_b, json={"uphold": True}, expect=403)
    api.post(f"/migration-requests/appeals/{appeal_id}/decision", token=w.ua_a, json={"uphold": True,
                                                                                    "note": "Receipt seen"}, expect=200)
    assert _status(api, tok, rid)["request_status"] == "NOC_GRANTED"


def test_interviews_working_hours_outcome_and_no_shows(api, app):
    w = build_world(api, interview_required=True)
    api.put(f"/colleges/{w.col_b.id}/interview-hours", token=w.ca_b, expect=200, json={"hours": [
        {"weekday": d, "start_time": "09:00:00", "end_time": "17:00:00"} for d in
        ("MON", "TUE", "WED", "THU", "FRI", "SAT", "SUN")]})
    tok = _verified_student(api, w)
    rid = _submit(api, w, tok)
    _to_recommended(api, w, rid, interview=True)
    _, body = api.post(f"/migration-requests/{rid}/recommend", token=w.ca_b, json={}, expect=422)
    assert "interview" in str(body["errors"][0]["details"]).lower()
    day = (datetime.now(IST) + timedelta(days=2)).date()
    late = datetime.combine(day, datetime.min.time(), tzinfo=IST).replace(hour=20)
    _, body = api.post("/interviews", token=w.ca_b, expect=422, json={
        "request_id": rid, "scheduled_start": late.isoformat(), "scheduled_end": (late + timedelta(minutes=30)).isoformat()})
    assert body["errors"][0]["code"] == "OUTSIDE_WORKING_HOURS"
    start = late.replace(hour=10)
    # Online interviews need the college's own meeting link, and it must be https
    _, body = api.post("/interviews", token=w.ca_b, expect=422, json={
        "request_id": rid, "scheduled_start": start.isoformat(),
        "scheduled_end": (start + timedelta(minutes=30)).isoformat()})
    assert "meeting_url" in body["errors"][0]["details"]["fields"]
    api.post("/interviews", token=w.ca_b, expect=422, json={
        "request_id": rid, "scheduled_start": start.isoformat(), "meeting_url": "http://zoom.us/j/1",
        "scheduled_end": (start + timedelta(minutes=30)).isoformat()})
    _, body = api.post("/interviews", token=w.ca_b, expect=201, json={
        "request_id": rid, "scheduled_start": start.isoformat(), "meeting_url": "https://meet.google.com/abc-defg-hij",
        "scheduled_end": (start + timedelta(minutes=30)).isoformat()})
    iv_id = body["data"]["id"]
    assert body["data"]["meeting_url"] == "https://meet.google.com/abc-defg-hij"
    # Move it into the past to record a no-show, then reschedule, then a pass
    iv = db.session.get(Interview, iv_id)
    iv.scheduled_start, iv.scheduled_end = utcnow() - timedelta(hours=2), utcnow() - timedelta(hours=1)
    db.session.commit()
    api.post(f"/interviews/{iv_id}/no-show", token=w.ca_b, json={}, expect=200)
    assert _status(api, tok, rid)["request_status"] == "UNDER_REVIEW"
    # Second slot in person: needs a room, no link
    api.post("/interviews", token=w.ca_b, expect=422, json={
        "request_id": rid, "scheduled_start": start.isoformat(), "mode": "IN_PERSON",
        "scheduled_end": (start + timedelta(minutes=30)).isoformat()})
    _, body = api.post("/interviews", token=w.ca_b, expect=201, json={
        "request_id": rid, "scheduled_start": start.isoformat(), "mode": "ONLINE",
        "meeting_url": "https://zoom.us/j/111", "meeting_info": "Passcode 1234",
        "scheduled_end": (start + timedelta(minutes=30)).isoformat()})
    iv2 = body["data"]["id"]
    # The college changes the link; the student can see the new one
    api.put(f"/interviews/{iv2}/meeting-link", token=w.ca_b, expect=200,
            json={"meeting_url": "https://teams.microsoft.com/l/meetup-join/xyz", "meeting_info": None})
    _, body = api.get(f"/interviews/{iv2}", token=tok, expect=200)
    assert body["data"]["meeting_url"].startswith("https://teams.microsoft.com") and body["data"]["meeting_info"] is None
    api.put(f"/interviews/{iv2}/meeting-link", token=tok, expect=403, json={"meeting_url": "https://x.example/1"})
    iv = db.session.get(Interview, iv2)
    iv.scheduled_start, iv.scheduled_end = utcnow() - timedelta(minutes=40), utcnow() - timedelta(minutes=10)
    db.session.commit()
    api.post(f"/interviews/{iv2}/outcome", token=w.ca_b, json={"outcome": "PASSED"}, expect=200)
    api.post(f"/migration-requests/{rid}/recommend", token=w.ca_b, json={}, expect=200)


def test_two_no_shows_reject_the_request(api):
    w = build_world(api, interview_required=True)
    tok = _verified_student(api, w)
    rid = _submit(api, w, tok)
    _to_recommended(api, w, rid, interview=True)
    for _ in range(2):
        start = utcnow() + timedelta(days=1)
        _, body = api.post("/interviews", token=w.ca_b, expect=201, json={
            "request_id": rid, "scheduled_start": start.isoformat(),
            "scheduled_end": (start + timedelta(minutes=30)).isoformat(), "extra_slot_reason": "Special slot",
            "mode": "IN_PERSON", "location": "Room 204"})
        iv = db.session.get(Interview, body["data"]["id"])
        iv.scheduled_start, iv.scheduled_end = utcnow() - timedelta(hours=2), utcnow() - timedelta(hours=1)
        db.session.commit()
        api.post(f"/interviews/{iv.id}/no-show", token=w.ca_b, json={}, expect=200)
    d = _status(api, tok, rid)
    assert d["request_status"] == "REJECTED" and "attend" in d["rejection_reason"]


def test_same_university_needs_only_tc(api):
    w = build_world(api)
    # Second college under University B becomes the student's current college
    from .conftest import seed_master_institution
    other = seed_master_institution("C-202", "Jai Hind College", "COLLEGE", parent=w.uni_b, district=w.mumbai)
    _, body = api.post(f"/onboarding/universities/{w.uni_b.id}/colleges", token=w.ua_b, expect=201,
                       json={"aishe_code": "C-202", "admin_email": "p@jaihind.edu", "admin_name": "Jai Hind Admin"})
    from .helpers import accept_invite
    res = accept_invite(api, body["data"]["debug_token"])
    verify_email(api, "p@jaihind.edu", res["debug_otp"])
    ca_other = login(api, "p@jaihind.edu")
    tok = make_student(api, w)
    api.post("/students/me/enrolment", token=tok, expect=200, data={
        "current_college_id": str(other.id), "enrolment_number": "JH001", "current_course_name": "B.Sc Computer Science",
        "current_semester": "3", "batch_year": "2024", "proof": pdf_file()})
    _, q = api.get("/enrolment-verifications", token=ca_other, expect=200)
    api.post(f"/enrolment-verifications/{q['data'][0]['id']}/decision", token=ca_other, json={"approve": True},
             expect=200)
    _, body = api.post("/migration-requests", token=tok, expect=201, json={"offering_id": w.offering_id,
                                                                            "entry_semester": 3})
    rid = body["data"]["id"]
    assert body["data"]["migration_type"] == "SAME_UNIVERSITY"
    # University B has no grading scheme: conversion needs a certificate
    _, body = api.put(f"/migration-requests/{rid}/grade-conversion", token=tok, json={}, expect=422)
    assert body["errors"][0]["code"] == "CONVERSION_CERTIFICATE_REQUIRED"
    api.put(f"/migration-requests/{rid}/grade-conversion", token=tok, expect=200,
            data={"certificate": pdf_file("conv.pdf"), "certified_percentage": "74.5"})
    api.post(f"/migration-requests/{rid}/submit", token=tok, expect=200)
    api.post(f"/migration-requests/{rid}/noc", token=ca_other, json={"approve": True}, expect=200)
    api.post(f"/migration-requests/{rid}/start-review", token=w.ca_b, expect=200)
    d = _status(api, w.ca_b, rid)
    for item in d["checklist"]:
        for doc in item["documents"]:
            api.post(f"/documents/{doc['id']}/verify", token=w.ca_b, json={"decision": "VERIFIED"}, expect=200)
    api.put(f"/migration-requests/{rid}/credits", token=w.ca_b, expect=200,
            json={"decisions": [{"id": c["id"], "decision": "ACCEPTED"} for c in d["credits"]]})
    api.post(f"/migration-requests/{rid}/recommend", token=w.ca_b, json={}, expect=200)
    api.post(f"/migration-requests/{rid}/approve-entry", token=w.ua_b, json={"is_eligible": True}, expect=200)
    api.post(f"/migration-requests/{rid}/accept-offer", token=tok, expect=200)
    api.post(f"/migration-requests/{rid}/university-release", token=w.ua_b, data={"file": pdf_file()}, expect=422)
    api.post(f"/migration-requests/{rid}/transfer-certificate", token=ca_other, data={"file": pdf_file()}, expect=200)
    assert _status(api, tok, rid)["request_status"] == "RELEASED"


def test_isolation_between_colleges(api):
    w = build_world(api)
    tok = _verified_student(api, w)
    rid = _submit(api, w, tok)
    # College B cannot decide College A's NOC; a student cannot read another's request
    _, body = api.post(f"/migration-requests/{rid}/noc", token=w.ca_b, json={"approve": True}, expect=403)
    other = make_student(api, w, email="other@s.in", mobile="9000000009", abc="999999999999")
    api.get(f"/migration-requests/{rid}", token=other, expect=403)
    # A student cannot call staff APIs
    api.get("/enrolment-verifications", token=tok, expect=403)


def test_staff_access_request_and_bundles(api):
    w = build_world(api)
    _, body = api.post("/access-requests", expect=201, json={
        "institution_id": w.col_b.id, "full_name": "Clerk One", "email": "clerk@ruia.edu", "password": PASSWORD,
        "confirm_password": PASSWORD, "designation": "Clerk"})
    assert verify_email(api, "clerk@ruia.edu", body["data"]["debug_otp"]) == "PENDING_APPROVAL"
    membership_id = body["data"]["membership_id"]
    _, body = api.post(f"/memberships/{membership_id}/access-decision", token=w.ca_b, expect=422,
                       json={"approve": True, "permissions": ["MANAGE_INSTITUTION_USERS"]})
    assert body["errors"][0]["code"] == "PERMISSION_NOT_GRANTABLE"
    api.post(f"/memberships/{membership_id}/access-decision", token=w.ca_b, expect=200,
             json={"approve": True, "bundles": ["VACANCY_MANAGER"]})
    clerk = login(api, "clerk@ruia.edu")
    api.put("/vacancies", token=clerk, expect=200, json={"offering_id": w.offering_id, "academic_year_id": w.year_id,
                                                          "entry_semester": 3, "categories": {"OPEN": 3}})
    api.get("/enrolment-verifications", token=clerk, expect=403)
    # Revoking takes effect on the next request (INT-11)
    api.put(f"/memberships/{membership_id}/permissions", token=w.ca_b, expect=200,
            json={"bundles": ["REPORT_VIEWER"]})
    api.put("/vacancies", token=clerk, expect=403, json={"offering_id": w.offering_id, "academic_year_id": w.year_id,
                                                          "entry_semester": 3, "categories": {"OPEN": 3}})


def test_vacancy_rules(api):
    w = build_world(api)
    _, body = api.put("/vacancies", token=w.ca_b, expect=422, json={
        "offering_id": w.offering_id, "academic_year_id": w.year_id, "entry_semester": 3, "categories": {"OPEN": 61}})
    assert body["errors"][0]["code"] == "VACANCY_EXCEEDS_INTAKE"
    api.put("/vacancies", token=w.ca_b, expect=422, json={
        "offering_id": w.offering_id, "academic_year_id": w.year_id, "entry_semester": 3, "categories": {"OPEN": -1}})


def test_jobs_expire_offers_and_escalate_noc(api):
    w = build_world(api)
    tok = _verified_student(api, w)
    rid = _submit(api, w, tok)
    req = db.session.get(MigrationRequest, rid)
    req.noc_due_at = utcnow() - timedelta(hours=1)
    db.session.commit()
    assert JOBS["noc_escalation_check"]()["escalated"] == 1
    # After escalation the source university may decide the NOC
    api.post(f"/migration-requests/{rid}/noc", token=w.ua_a, json={"approve": True}, expect=200)
    _to_recommended_after_noc(api, w, rid)
    api.post(f"/migration-requests/{rid}/approve-entry", token=w.ua_b, json={"is_eligible": True}, expect=200)
    req = db.session.get(MigrationRequest, rid)
    req.offer_expires_at = utcnow() - timedelta(minutes=1)
    db.session.commit()
    assert JOBS["expire_offers"]()["expired"] == 1
    assert _status(api, tok, rid)["request_status"] == "OFFER_EXPIRED"


def _to_recommended_after_noc(api, w, rid):
    api.post(f"/migration-requests/{rid}/start-review", token=w.ca_b, expect=200)
    _, body = api.get(f"/migration-requests/{rid}", token=w.ca_b, expect=200)
    d = body["data"]
    for item in d["checklist"]:
        for doc in item["documents"]:
            if doc["verification_status"] != "VERIFIED":
                api.post(f"/documents/{doc['id']}/verify", token=w.ca_b, json={"decision": "VERIFIED"}, expect=200)
    api.put(f"/migration-requests/{rid}/credits", token=w.ca_b, expect=200,
            json={"decisions": [{"id": c["id"], "decision": "ACCEPTED"} for c in d["credits"]]})
    api.post(f"/migration-requests/{rid}/recommend", token=w.ca_b, json={}, expect=200)


def _xlsx(rows):
    wb = Workbook()
    ws = wb.active
    for r in rows:
        ws.append(r)
    buf = io.BytesIO()
    wb.save(buf)
    buf.seek(0)
    return buf


def test_imported_institutions_are_found_by_the_lookup_api(api):
    from .conftest import seed_geo, seed_master_institution
    _, pune, _ = seed_geo()
    uni = seed_master_institution("U-0500", "Test University", "UNIVERSITY", district=pune)
    seed_master_institution("C-9001", "Good College", "COLLEGE", parent=uni, district=pune)
    seed_master_institution("C-9001", "Good College (PM Vidyalaxmi list)", "PM_VIDYALAXMI", district=pune)
    seed_master_institution("S-7001", "Govt Polytechnic", "STANDALONE", district=pune)
    _, body = api.get("/onboarding/lookup?aishe_code=C-9001", expect=200)
    inst = body["data"]["institution"]
    # the same code is in two files: the COLLEGE row wins over the PM Vidyalaxmi row
    assert body["data"]["found"] and inst["parent_aishe_code"] == "U-0500" and inst["institution_type"] == "COLLEGE"
    _, body = api.get("/onboarding/lookup?aishe_code=S-7001&institution_type=STANDALONE", expect=200)
    assert body["data"]["institution"]["registration_type"] == "STANDALONE"
    api.get("/onboarding/lookup?aishe_code=S-7001&institution_type=COLLEGE", expect=422)
    _, body = api.get("/onboarding/lookup?aishe_code=U-0500", expect=200)
    assert body["data"]["institution"]["kind"] == "UNIVERSITY"


def test_bulk_vacancy_upload_with_error_file(api):
    w = build_world(api)
    f = _xlsx([["course_code", "academic_year", "entry_semester", "OPEN", "SC"], ["BSC-CS", "2026-27", 3, 4, 1],
               ["NOPE", "2026-27", 3, 1, 0]])
    _, body = api.post(f"/institutions/college/{w.col_b.id}/bulk-uploads/VACANCIES", token=w.ca_b,
                       data={"file": (f, "v.xlsx")}, expect=201)
    assert body["data"]["success_rows"] == 1 and body["data"]["error_rows"] == 1
    assert "error_file_url" in body["data"]


def test_support_ticket_and_data_export(api):
    w = build_world(api)
    tok = make_student(api, w)
    _, body = api.post("/support-tickets", token=tok, expect=201, json={
        "subject": "Cannot upload", "description": "The upload button does nothing on my phone."})
    tid = body["data"]["id"]
    api.post(f"/support-tickets/{tid}/replies", token=w.pa_token, json={"message": "Please try again"}, expect=201)
    _, body = api.get(f"/support-tickets/{tid}", token=tok, expect=200)
    assert len(body["data"]["replies"]) == 1
    resp, _ = api.get("/privacy/my-data", token=tok)
    assert resp.status_code == 200 and b'"academic_records"' in resp.data


def test_suspended_college_blocks_new_registrations(api):
    w = build_world(api)
    api.post(f"/institutions/college/{w.col_b.id}/suspend", token=w.ua_b, json={"reason": "Affiliation under review"},
             expect=200)
    _, body = api.get("/search/offerings", expect=200)
    assert body["meta"]["total"] == 0
    api.post(f"/institutions/college/{w.col_b.id}/reactivate", token=w.ua_b, expect=200)
    _, body = api.get("/search/offerings", expect=200)
    assert body["meta"]["total"] == 1


def test_second_admin_claim_goes_to_review(api):
    w = build_world(api)
    _, body = api.post("/onboarding/registrations", expect=201, data={
        "institution_type": "UNIVERSITY", "aishe_code": "U-0001", "full_name": "Second Claimant",
        "email": "claim@unipune.ac.in", "password": PASSWORD, "confirm_password": PASSWORD,
        "mobile": "9876511111", "designation": "Deputy Registrar", "authorization_letter": pdf_file()})
    _, body = api.get("/onboarding/registrations?second_claim=true", token=w.pa_token, expect=200)
    assert len(body["data"]) == 1 and body["data"][0]["is_second_claim"]


def test_activity_rows_are_one_per_request(api):
    build_world(api)
    total_rows = UserActivity.query.count()
    distinct = db.session.query(db.func.count(db.func.distinct(UserActivity.request_id))).scalar()
    assert total_rows == distinct and total_rows > 20
    assert UserActivity.query.filter(UserActivity.created_at > datetime.now(timezone.utc)).count() == 0
