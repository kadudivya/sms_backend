"""Builders that drive the real APIs to set up a realistic world."""
from datetime import timedelta

from app.core.utils import today_ist

from .conftest import PASSWORD, login, make_platform_admin, pdf_file, png_file, seed_geo, seed_master_institution


class World:
    pass


def accept_invite(api, token, extra=None, letter=False):
    data = {"password": PASSWORD, "confirm_password": PASSWORD, "full_name": "Admin Person",
            "mobile": "9876543210", "designation": "Registrar"}
    data.update(extra or {})
    if letter:
        data["authorization_letter"] = pdf_file("letter.pdf")
        _, body = api.post(f"/onboarding/invitations/{token}/accept", data=data, expect=201)
    else:
        _, body = api.post(f"/onboarding/invitations/{token}/accept", json=data, expect=201)
    return body["data"]


def verify_email(api, email, otp):
    _, body = api.post("/auth/verify-email", json={"email": email, "otp": otp}, expect=200)
    return body["data"]["account_state"]


def build_world(api, offer_mode="ROLLING", interview_required=False):
    w = World()
    w.pa = make_platform_admin()
    make_platform_admin("pa2@platform.in", "Second Admin")
    w.state, w.pune, w.mumbai = seed_geo()
    w.uni_a = seed_master_institution("U-0001", "Savitribai Phule Pune University", "UNIVERSITY", district=w.pune)
    w.uni_b = seed_master_institution("U-0002", "University of Mumbai", "UNIVERSITY", district=w.mumbai)
    w.col_a = seed_master_institution("C-101", "Fergusson College", "COLLEGE", parent=w.uni_a, district=w.pune)
    w.col_b = seed_master_institution("C-201", "Ruia College", "COLLEGE", parent=w.uni_b, district=w.mumbai)
    pa_token = login(api, "pa@platform.in")
    w.pa_token = pa_token

    # University A via path A (direct creation, pre-approved admin)
    _, body = api.post("/onboarding/universities", token=pa_token, expect=201,
                       json={"aishe_code": "U-0001", "admin_email": "admin@unipune.ac.in", "admin_name": "UA A"})
    res = accept_invite(api, body["data"]["debug_token"])
    assert verify_email(api, "admin@unipune.ac.in", res["debug_otp"]) == "ACTIVE"
    w.ua_a = login(api, "admin@unipune.ac.in")

    # University B via path B (signup link, needs approval)
    _, body = api.post("/onboarding/universities/signup-links", token=pa_token, expect=201,
                       json={"aishe_code": "U-0002", "email": "registrar@mu.ac.in"})
    res = accept_invite(api, body["data"]["debug_token"], letter=True)
    assert verify_email(api, "registrar@mu.ac.in", res["debug_otp"]) == "PENDING_APPROVAL"
    api.post(f"/onboarding/registrations/{res['registration_request_id']}/approve", token=pa_token,
             json={"note": "Verified by phone"}, expect=200)
    w.ua_b = login(api, "registrar@mu.ac.in")

    # College A via path A by University A
    _, body = api.post(f"/onboarding/universities/{w.uni_a.id}/colleges", token=w.ua_a, expect=201,
                       json={"aishe_code": "C-101", "admin_email": "principal@fergusson.edu", "admin_name": "CA A"})
    res = accept_invite(api, body["data"]["debug_token"])
    verify_email(api, "principal@fergusson.edu", res["debug_otp"])
    w.ca_a = login(api, "principal@fergusson.edu")

    # College B via path C (public request) approved by University B
    _, body = api.post("/onboarding/registrations", expect=201, data={
        "institution_type": "COLLEGE", "aishe_code": "C-201", "full_name": "CA B", "email": "principal@ruia.edu",
        "password": PASSWORD, "confirm_password": PASSWORD, "mobile": "9876500000", "designation": "Principal",
        "authorization_letter": pdf_file()})
    verify_email(api, "principal@ruia.edu", body["data"]["debug_otp"])
    reg_id = body["data"]["registration_request_id"]
    _, lst = api.get("/onboarding/registrations", token=w.ua_b, expect=200)
    assert [r["id"] for r in lst["data"]] == [reg_id]
    api.post(f"/onboarding/registrations/{reg_id}/approve", token=w.ua_b, json={}, expect=200)
    w.ca_b = login(api, "principal@ruia.edu")

    # Academic setup at University B (destination)
    today = today_ist()
    _, body = api.post(f"/academic-years?university_id={w.uni_b.id}", token=w.ua_b, expect=201,
                       json={"label": "2026-27", "start_date": str(today - timedelta(days=60)),
                             "end_date": str(today + timedelta(days=300)), "is_current": True})
    w.year_id = body["data"]["id"]
    _, body = api.post(f"/universities/{w.uni_b.id}/courses", token=w.ua_b, expect=201, json={
        "code": "BSC-CS", "name": "B.Sc Computer Science", "level": "UNDERGRADUATE", "discipline": "Computer Science",
        "duration_years": 3, "total_semesters": 6, "offer_mode": offer_mode, "interview_required": interview_required})
    w.course_id = body["data"]["id"]
    _, body = api.post(f"/universities/{w.uni_b.id}/syllabi", token=w.ua_b, expect=201, json={
        "name": "B.Sc CS", "regulation_year": 2023, "subjects": [
            {"semester": 1, "code": "CS101", "name": "Programming in C", "credits": 4},
            {"semester": 2, "code": "CS201", "name": "Data Structures", "credits": 4}]})
    syl_id = body["data"]["id"]
    api.post(f"/syllabi/{syl_id}/publish", token=w.ua_b, expect=200)
    api.post(f"/courses/{w.course_id}/syllabi", token=w.ua_b, json={"syllabus_id": syl_id, "batch_from": 2020},
             expect=201)
    api.post(f"/courses/{w.course_id}/eligibility-rules", token=w.ua_b, expect=201, json={
        "effective_from": str(today - timedelta(days=30)), "min_percentage": 60, "min_completed_semesters": 2})
    _, body = api.post(f"/courses/{w.course_id}/migration-windows", token=w.ua_b, expect=201, json={
        "academic_year_id": w.year_id, "opens_on": str(today), "closes_on": str(today + timedelta(days=30)),
        "entry_semesters": [3]})
    w.window_id = body["data"]["id"]
    _, body = api.post(f"/courses/{w.course_id}/offerings", token=w.ua_b, expect=201,
                       json={"college_id": w.col_b.id, "sanctioned_intake": 60})
    w.offering_id = body["data"]["id"]
    api.put("/vacancies", token=w.ca_b, expect=200, json={
        "offering_id": w.offering_id, "academic_year_id": w.year_id, "entry_semester": 3,
        "categories": {"OPEN": 1, "SC": 1}})

    # Grading scheme at University A (source) so conversion is automatic
    api.post(f"/universities/{w.uni_a.id}/grading-schemes", token=w.ua_a, expect=201, json={
        "grade_system": "CGPA_10", "formula_expr": "cgpa * 9.5", "batch_from": 2020,
        "source_url": "https://example.in/circular.pdf"})
    return w


def make_student(api, w, email="asha@student.in", mobile="9123456780", abc="123456789012", sgpa=8.0, college=None):
    _, body = api.post("/students", expect=201, json={
        "first_name": "Asha", "last_name": "Patil", "email": email, "mobile": mobile, "password": PASSWORD,
        "confirm_password": PASSWORD, "date_of_birth": "2004-05-10", "abc_account_id": abc, "consent": True})
    verify_email(api, email, body["data"]["debug_otp"])
    token = login(api, email)
    api.patch("/students/me", token=token, expect=200, json={
        "address_line": "12 FC Road", "state_id": w.state.id, "district_id": w.pune.id, "pincode": "411004"})
    api.post("/students/me/enrolment", token=token, expect=200, data={
        "current_college_id": str((college or w.col_a).id), "enrolment_number": f"FC{abc[-4:]}",
        "current_course_name": "B.Sc Computer Science", "current_semester": "3", "batch_year": "2024",
        "proof": pdf_file("idcard.pdf")})
    for sem, code, name in ((1, "CS101", "Programming in C"), (2, "CS201", "Data Structures")):
        api.post("/students/me/academic-records", token=token, expect=201, json={
            "semester": sem, "grade_system": "CGPA_10", "sgpa": sgpa, "cgpa": sgpa, "credits_earned": 20,
            "subjects": [{"subject_code": code, "subject_name": name, "credits": 4, "grade": "A"}]})
    for doc_type, f in (("PHOTO", png_file("photo.png")), ("ID_PROOF", pdf_file()), ("CLASS_10_MARKSHEET", pdf_file()),
                        ("CLASS_12_MARKSHEET", pdf_file()), ("SEMESTER_MARKSHEET", pdf_file()),
                        ("ABC_CREDIT_STATEMENT", pdf_file())):
        api.post("/documents/mine", token=token, expect=201, data={"doc_type": doc_type, "file": f})
    return token
