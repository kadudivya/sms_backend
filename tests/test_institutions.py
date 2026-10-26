"""Universities and colleges as two tables: kind-aware routes, scoping, and a college
with no university (STANDALONE) taking part in a migration."""
import pytest
from sqlalchemy.exc import IntegrityError

from app.extensions import db
from app.models import InstitutionMembership, MigrationRequest

from .conftest import login, pdf_file
from .helpers import accept_invite, build_world, make_student, verify_email


def test_kind_aware_institution_routes(api):
    w = build_world(api)
    _, body = api.get("/institutions?kind=university", token=w.pa_token, expect=200)
    assert {i["aishe_code"] for i in body["data"]} == {"U-0001", "U-0002"}
    _, body = api.get("/institutions?kind=college&institution_type=COLLEGE", token=w.pa_token, expect=200)
    assert {i["aishe_code"] for i in body["data"]} == {"C-101", "C-201"}
    # University B sees itself and its own colleges only
    _, body = api.get("/institutions?kind=university", token=w.ua_b, expect=200)
    assert [i["id"] for i in body["data"]] == [w.uni_b.id]
    _, body = api.get("/institutions?kind=college", token=w.ua_b, expect=200)
    assert [i["parent_name"] for i in body["data"]] == ["University of Mumbai"]
    api.get("/institutions?kind=school", token=w.ua_b, expect=422)

    _, body = api.get(f"/institutions/university/{w.uni_b.id}", token=w.ua_b, expect=200)
    assert body["data"]["kind"] == "UNIVERSITY" and body["data"]["has_active_admin"]
    api.get(f"/institutions/college/{w.col_b.id}", token=w.ua_b, expect=200)   # its own college
    api.get(f"/institutions/college/{w.col_a.id}", token=w.ua_b, expect=403)   # another university's college
    api.get("/institutions/college/999999", token=w.pa_token, expect=404)

    # staff management on a university uses the university route
    _, body = api.post(f"/institutions/university/{w.uni_b.id}/invitations", token=w.ua_b, expect=201,
                       json={"email": "dean@mu.ac.in", "full_name": "Dean", "bundles": ["ACADEMIC_MANAGER"]})
    _, body = api.get(f"/institutions/university/{w.uni_b.id}/invitations", token=w.ua_b, expect=200)
    assert body["data"][0]["university_id"] == w.uni_b.id and body["data"][0]["college_id"] is None
    _, body = api.get(f"/institutions/university/{w.uni_b.id}/members", token=w.ua_b, expect=200)
    assert [m["email"] for m in body["data"]] == ["registrar@mu.ac.in"]
    api.get(f"/institutions/university/{w.uni_a.id}/members", token=w.ua_b, expect=403)
    api.get(f"/institutions/college/{w.col_b.id}/access-requests", token=w.ca_b, expect=200)
    api.get(f"/institutions/college/{w.col_b.id}/bulk-uploads", token=w.ca_b, expect=200)

    # who-am-i says which table each membership points at
    _, body = api.get("/auth/me", token=w.ca_b, expect=200)
    m = body["data"]["memberships"][0]
    assert (m["institution_kind"], m["institution_id"], m["institution_type"], m["parent_university_id"]) == \
        ("COLLEGE", w.col_b.id, "COLLEGE", w.uni_b.id)

    # affiliation and university policy have their own routes
    api.put(f"/colleges/{w.col_b.id}/affiliation", token=w.pa_token, expect=200,
            json={"parent_university_id": w.uni_a.id})
    _, body = api.get(f"/institutions/college/{w.col_b.id}", token=w.pa_token, expect=200)
    assert body["data"]["university_id"] == w.uni_a.id and body["data"]["parent_aishe_code"] == "U-0001"
    api.put(f"/colleges/{w.col_b.id}/affiliation", token=w.pa_token, expect=200,
            json={"parent_university_id": w.uni_b.id})
    _, body = api.put(f"/universities/{w.uni_b.id}/reservation-policy", token=w.ua_b, expect=200,
                      json={"release_unfilled_reserved_to_open": True})
    assert body["data"]["release_unfilled_reserved_to_open"] is True

    # geography is the loader's plain states / districts
    _, body = api.get(f"/states/{w.state.id}/districts", expect=200)
    assert {d["name"] for d in body["data"]} == {"Pune", "Mumbai"}


def test_reports_and_logs_are_scoped(api):
    w = build_world(api)
    for name in ("applications", "vacancies", "migration-flow", "turnaround", "interviews", "credit-transfer",
                 "document-verification", "onboarding"):
        api.get(f"/reports/{name}", token=w.pa_token, expect=200)
        api.get(f"/reports/{name}", token=w.ua_b, expect=403 if name == "onboarding" else 200)
    _, body = api.get("/reports/vacancies", token=w.ua_a, expect=200)
    assert body["data"]["rows"] == []  # University A cannot see University B's college vacancies
    _, body = api.get("/reports/vacancies", token=w.ua_b, expect=200)
    assert body["data"]["totals"]["seats"] == 2
    _, body = api.get("/reports/onboarding", token=w.pa_token, expect=200)
    by_type = {(r["institution_type"], r["onboarding_status"]): r["count"] for r in body["data"]["rows"]}
    assert by_type[("UNIVERSITY", "ACTIVE")] == 2 and by_type[("COLLEGE", "ACTIVE")] == 2
    _, body = api.get("/user-activity", token=w.ua_b, expect=200)
    assert body["data"] and all(r["college_id"] in (None, w.col_b.id) for r in body["data"])
    api.get("/notifications/history", token=w.ua_b, expect=200)
    api.get("/offering-requests", token=w.ca_b, expect=200)
    api.get("/migration-requests/appeals", token=w.ua_b, expect=200)
    api.get("/onboarding/college-requests", token=w.pa_token, expect=200)
    _, body = api.get("/dashboards/platform", token=w.pa_token, expect=200)
    assert body["data"]["institutions"]["UNIVERSITY"]["ACTIVE"] == 2


def test_membership_points_at_exactly_one_institution(app):
    from .conftest import make_platform_admin, seed_geo, seed_master_institution
    _, pune, _ = seed_geo()
    uni = seed_master_institution("U-0003", "Uni", "UNIVERSITY", district=pune)
    col = seed_master_institution("C-0003", "Col", "COLLEGE", parent=uni, district=pune)
    user = make_platform_admin("x@y.in")
    db.session.add(InstitutionMembership(user_id=user.id, university_id=uni.id, college_id=col.id, role="ADMIN"))
    with pytest.raises(IntegrityError):
        db.session.commit()
    db.session.rollback()
    db.session.add(InstitutionMembership(user_id=user.id, role="ADMIN"))
    with pytest.raises(IntegrityError):
        db.session.commit()
    db.session.rollback()


def test_student_at_a_standalone_institution_can_migrate(api):
    from .conftest import seed_master_institution
    w = build_world(api)
    poly = seed_master_institution("S-5001", "Govt Polytechnic Pune", "STANDALONE", district=w.pune)
    # only the Platform Admin can bring in an institution with no university
    api.post("/onboarding/standalone-institutions", token=w.ua_a, expect=403,
             json={"aishe_code": "S-5001", "admin_email": "head@gpp.ac.in", "admin_name": "Head"})
    _, body = api.post("/onboarding/standalone-institutions", token=w.pa_token, expect=201,
                       json={"aishe_code": "S-5001", "admin_email": "head@gpp.ac.in", "admin_name": "Head"})
    assert body["data"]["institution"]["registration_type"] == "STANDALONE"
    res = accept_invite(api, body["data"]["debug_token"])
    verify_email(api, "head@gpp.ac.in", res["debug_otp"])
    head = login(api, "head@gpp.ac.in")
    _, me = api.get("/auth/me", token=head, expect=200)
    assert me["data"]["roles"] == ["COLLEGE_ADMIN"]
    assert me["data"]["memberships"][0]["institution_type"] == "STANDALONE"

    student = make_student(api, w, college=poly)
    _, q = api.get("/enrolment-verifications", token=head, expect=200)
    api.post(f"/enrolment-verifications/{q['data'][0]['id']}/decision", token=head, json={"approve": True},
             expect=200)
    api.get("/enrolment-verifications", token=w.ua_a, expect=200)  # not University A's student
    _, body = api.post("/migration-requests", token=student, expect=201,
                       json={"offering_id": w.offering_id, "entry_semester": 3})
    req = db.session.get(MigrationRequest, body["data"]["id"])
    assert req.source_college_id == poly.id and req.source_university_id is None
    assert body["data"]["migration_type"] == "CROSS_UNIVERSITY"
    # no university grading formula for this institution: the student uploads its conversion certificate
    api.put(f"/migration-requests/{req.id}/grade-conversion", token=student, expect=200,
            data={"certified_percentage": "72", "certificate": pdf_file("conversion.pdf")})
    api.post(f"/migration-requests/{req.id}/submit", token=student, expect=200)
    _, d = api.get(f"/migration-requests/{req.id}", token=head, expect=200)
    api.post(f"/migration-requests/{req.id}/noc", token=head, expect=200,
             json={"approve": True, "version": d["data"]["version"]})
    _, d = api.get(f"/migration-requests/{req.id}", token=w.ca_b, expect=200)
    assert d["data"]["request_status"] == "NOC_GRANTED"
    api.get(f"/migration-requests/{req.id}", token=w.ua_a, expect=403)
