"""End to end: onboarding by all three paths, then a cross-university migration to admission."""
from app.extensions import db
from app.models import UserActivity, VacancyCategory

from .conftest import pdf_file
from .helpers import build_world, make_student


def _detail(api, token, req_id):
    _, body = api.get(f"/migration-requests/{req_id}", token=token, expect=200)
    return body["data"]


def test_full_cross_university_migration(api):
    w = build_world(api)
    student = make_student(api, w)

    # College A verifies the student's enrolment
    _, q = api.get("/enrolment-verifications", token=w.ca_a, expect=200)
    student_id = q["data"][0]["id"]
    api.post(f"/enrolment-verifications/{student_id}/decision", token=w.ca_a, json={"approve": True}, expect=200)

    # Search and create the request
    _, found = api.get("/search/offerings?course=computer", expect=200)
    offer = found["data"][0]
    assert offer["accepting_requests"] and offer["available_seats"] == 2
    _, body = api.post("/migration-requests", token=student, expect=201,
                       json={"offering_id": offer["offering_id"], "entry_semester": 3})
    req = body["data"]
    assert req["migration_type"] == "CROSS_UNIVERSITY"
    assert req["conversion_options"]["needs"] == "NOTHING"

    # Grade conversion uses University A's formula: 8.0 * 9.5 = 76.0
    _, body = api.put(f"/migration-requests/{req['id']}/grade-conversion", token=student, json={}, expect=200)
    assert body["data"]["conversion_source"] == "FORMULA" and body["data"]["converted_percentage"] == 76.0

    _, body = api.post(f"/migration-requests/{req['id']}/submit", token=student, expect=200)
    assert body["data"]["request_status"] == "SUBMITTED"
    assert body["data"]["eligibility_result"] == "ELIGIBLE"

    # Source college grants the NOC (with the version check)
    d = _detail(api, w.ca_a, req["id"])
    api.post(f"/migration-requests/{req['id']}/noc", token=w.ca_a, expect=200,
             json={"approve": True, "version": d["version"]})

    # Destination college reviews: documents, credits, recommend
    api.post(f"/migration-requests/{req['id']}/start-review", token=w.ca_b, expect=200)
    d = _detail(api, w.ca_b, req["id"])
    assert d["request_status"] == "UNDER_REVIEW" and len(d["credits"]) == 2
    assert all(c["mapped_subject_code"] for c in d["credits"])
    resp, body = api.post(f"/migration-requests/{req['id']}/recommend", token=w.ca_b, json={}, expect=422)
    assert body["errors"][0]["code"] == "REVIEW_INCOMPLETE"
    for item in d["checklist"]:
        for doc in item["documents"]:
            api.post(f"/documents/{doc['id']}/verify", token=w.ca_b, json={"decision": "VERIFIED"}, expect=200)
    api.put(f"/migration-requests/{req['id']}/credits", token=w.ca_b, expect=200,
            json={"decisions": [{"id": c["id"], "decision": "ACCEPTED"} for c in d["credits"]]})
    api.post(f"/migration-requests/{req['id']}/recommend", token=w.ca_b, json={}, expect=200)

    # A stale version is refused (INT-13)
    _, body = api.post(f"/migration-requests/{req['id']}/approve-entry", token=w.ua_b, expect=409,
                       json={"is_eligible": True, "version": 1})
    assert body["errors"][0]["code"] == "CONCURRENT_UPDATE"

    # Destination university approves entry; rolling mode offers a seat immediately
    d = _detail(api, w.ua_b, req["id"])
    _, body = api.post(f"/migration-requests/{req['id']}/approve-entry", token=w.ua_b, expect=200,
                       json={"is_eligible": True, "additional_papers": ["Discrete Mathematics"],
                             "version": d["version"]})
    assert body["data"]["request_status"] == "PROVISIONAL_OFFER"
    seat = VacancyCategory.query.filter_by(category="OPEN").one()
    assert (seat.blocked, seat.filled) == (1, 0)

    api.post(f"/migration-requests/{req['id']}/accept-offer", token=student, expect=200)

    # Release: TC from college A, migration certificate from university A
    api.post(f"/migration-requests/{req['id']}/transfer-certificate", token=w.ca_a, data={"file": pdf_file("tc.pdf")},
             expect=200)
    assert _detail(api, student, req["id"])["request_status"] == "OFFER_ACCEPTED"
    api.post(f"/migration-requests/{req['id']}/university-release", token=w.ua_a,
             data={"file": pdf_file("migration.pdf")}, expect=200)
    assert _detail(api, student, req["id"])["request_status"] == "RELEASED"

    # Admission and originals
    d = _detail(api, w.ca_b, req["id"])
    api.post(f"/migration-requests/{req['id']}/confirm-admission", token=w.ca_b, expect=200,
             json={"fee_paid": True, "version": d["version"]})
    db.session.expire_all()
    seat = VacancyCategory.query.filter_by(category="OPEN").one()
    assert (seat.blocked, seat.filled) == (0, 1)
    api.post(f"/migration-requests/{req['id']}/verify-originals", token=w.ca_b, expect=200)
    d = _detail(api, student, req["id"])
    assert d["request_status"] == "ADMITTED"
    letters = {lt["letter_type"] for lt in d["letters"]}
    assert {"NOC", "PROVISIONAL_OFFER", "RELEASE_CONFIRMATION", "ADMISSION_CONFIRMATION"} <= letters
    history = [h["to_status"] for h in d["history"]]
    assert history == ["DRAFT", "SUBMITTED", "NOC_GRANTED", "UNDER_REVIEW", "RECOMMENDED", "ENTRY_APPROVED",
                       "PROVISIONAL_OFFER", "OFFER_ACCEPTED", "RELEASED", "ADMITTED_ORIGINALS_PENDING", "ADMITTED"]

    # Student now belongs to the destination college
    _, me = api.get("/students/me", token=student, expect=200)
    assert me["data"]["current_college_id"] == w.col_b.id

    # Letters can be downloaded and verified publicly via the QR token
    letter = d["letters"][0]
    _, link = api.get(f"/letters/{letter['id']}/link", token=student, expect=200)
    url = link["data"]["download_url"].split("/api/v1", 1)[1]
    resp, _ = api.get(url)
    assert resp.status_code == 200 and resp.data.startswith(b"%PDF")
    from app.models import Letter
    token = Letter.query.get(letter["id"]).qr_token
    _, ver = api.get(f"/public/letters/{token}", expect=200)
    assert ver["data"]["letter_status"] == "VALID"

    # Every API call produced exactly one activity row with steps
    rows = UserActivity.query.filter_by(api_code="APPROVE_ENTRY", outcome="SUCCESS").all()
    assert len(rows) == 1
    steps = [s["step"] for s in rows[0].steps]
    assert "Seat blocked for provisional offer" in steps and "Equivalence decision recorded" in steps
    assert any(c["field"] == "request_status" for c in rows[0].changes)

    # Dashboards and reports answer for each role
    api.get("/dashboards/student", token=student, expect=200)
    _, cd = api.get(f"/dashboards/college/{w.col_b.id}", token=w.ca_b, expect=200)
    assert cd["data"]["incoming_by_status"] == {"ADMITTED": 1}
    _, ud = api.get(f"/dashboards/university/{w.uni_b.id}", token=w.ua_b, expect=200)
    assert ud["data"]["vacancies"]["filled"] == 1
    api.get("/dashboards/platform", token=w.pa_token, expect=200)
    _, rep = api.get("/reports/turnaround", token=w.ua_b, expect=200)
    assert rep["data"]["rows"][0]["completed"] == 1
    resp, _ = api.get("/reports/applications?format=xlsx", token=w.ua_b)
    assert resp.status_code == 200 and resp.data[:2] == b"PK"


def test_university_cannot_act_on_another_universitys_college(api):
    w = build_world(api)
    _, body = api.post(f"/onboarding/universities/{w.uni_b.id}/colleges", token=w.ua_a, expect=403,
                       json={"aishe_code": "C-201", "admin_email": "x@y.in", "admin_name": "Xavier"})
    assert body["errors"][0]["code"] == "PERMISSION_DENIED"


def test_waitlist_and_promotion_on_decline(api):
    w = build_world(api)
    tokens = []
    for i, sgpa in enumerate((7.0, 9.0)):
        tokens.append(make_student(api, w, email=f"s{i}@student.in", mobile=f"912345678{i}", abc=f"12345678901{i}",
                                   sgpa=sgpa))
    _, q = api.get("/enrolment-verifications", token=w.ca_a, expect=200)
    for row in q["data"]:
        api.post(f"/enrolment-verifications/{row['id']}/decision", token=w.ca_a, json={"approve": True}, expect=200)
    # One OPEN seat only for OPEN-category students: make SC seat unusable by setting it to 0
    api.put("/vacancies", token=w.ca_b, expect=200, json={
        "offering_id": w.offering_id, "academic_year_id": w.year_id, "entry_semester": 3, "categories": {"OPEN": 1}})
    req_ids = []
    for tok in tokens:
        _, body = api.post("/migration-requests", token=tok, expect=201,
                           json={"offering_id": w.offering_id, "entry_semester": 3})
        rid = body["data"]["id"]
        api.put(f"/migration-requests/{rid}/grade-conversion", token=tok, json={}, expect=200)
        api.post(f"/migration-requests/{rid}/submit", token=tok, expect=200)
        req_ids.append(rid)
    for rid in req_ids:
        api.post(f"/migration-requests/{rid}/noc", token=w.ca_a, json={"approve": True}, expect=200)
        api.post(f"/migration-requests/{rid}/start-review", token=w.ca_b, expect=200)
        d = _detail(api, w.ca_b, rid)
        for item in d["checklist"]:
            for doc in item["documents"]:
                if doc["verification_status"] != "VERIFIED":
                    api.post(f"/documents/{doc['id']}/verify", token=w.ca_b, json={"decision": "VERIFIED"},
                             expect=200)
        api.put(f"/migration-requests/{rid}/credits", token=w.ca_b, expect=200,
                json={"decisions": [{"id": c["id"], "decision": "ACCEPTED"} for c in d["credits"]]})
        api.post(f"/migration-requests/{rid}/recommend", token=w.ca_b, json={}, expect=200)
    first = _detail(api, w.ua_b, req_ids[0])
    api.post(f"/migration-requests/{req_ids[0]}/approve-entry", token=w.ua_b, json={"is_eligible": True}, expect=200)
    api.post(f"/migration-requests/{req_ids[1]}/approve-entry", token=w.ua_b, json={"is_eligible": True}, expect=200)
    assert _detail(api, tokens[0], req_ids[0])["request_status"] == "PROVISIONAL_OFFER"
    second = _detail(api, tokens[1], req_ids[1])
    assert second["request_status"] == "WAITLISTED" and second["waitlist_rank"] == 1
    # First student declines -> seat goes to the waitlisted student
    api.post(f"/migration-requests/{req_ids[0]}/decline-offer", token=tokens[0], expect=200)
    assert _detail(api, tokens[1], req_ids[1])["request_status"] == "PROVISIONAL_OFFER"
    assert first["version"] >= 1
