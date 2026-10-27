"""Demo seed for EVERY table of the app (development / demo databases only).

Run it once on a database where only the AISHE tables are filled (states, districts,
universities, colleges, from load_universities.py) and every other table is empty.
Start the app once before, so all tables exist.

    python scripts/seed.py

How it works
Apart from the first Platform Admin (there is nobody to create them), everything goes
through the app's real APIs, the same calls the screens make: onboarding by all three
paths, staff invitations and access requests, courses and windows, students, migration
requests at different stages, interviews, appeals, letters, support tickets, data
requests and a bulk upload. So every row follows the business rules, and the
notifications, OTP codes, sessions and activity log rows are produced the way real use
produces them.

Tables filled (all 50)
  states, districts, universities, colleges ........ AISHE rows (four are onboarded)
  pincodes, master_import_batches .................. a small PIN code list for Pune / Mumbai
  users, user_sessions, otp_codes, one_time_tokens, ip_blocks
  institution_memberships, invitations, registration_requests, college_onboarding_requests
  documents ....................................... real PDF / PNG files in the storage folder
  academic_years, courses, syllabi, syllabus_subjects, course_syllabi, course_equivalences,
  eligibility_rules, grading_schemes, migration_windows, offering_requests, course_offerings,
  vacancies, vacancy_categories, interview_working_hours, bulk_upload_jobs
  student_profiles, consent_records, academic_records, subject_results
  migration_requests, migration_status_history, grade_conversions, credit_assessments,
  equivalence_decisions, appeals, interviews, interview_panelists
  letter_templates, letters, notifications, support_tickets, support_ticket_replies,
  data_requests, user_activity

The logins are printed at the end. It refuses to run with the production config, and
stops if the demo users already exist (empty the tables to run it again).
"""
import argparse
import io
import os
import sys
from datetime import datetime, timedelta

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

PASSWORDS = {
    "PLATFORM_ADMIN": "Platform@123",
    "UNIVERSITY_ADMIN": "UniAdmin@123",
    "UNIVERSITY_USER": "UniUser@123",
    "COLLEGE_ADMIN": "CollegeAdmin@123",
    "COLLEGE_USER": "CollegeUser@123",
    "STUDENT": "Student@123",
}

# AISHE rows used: key -> (aishe_code, fallback name, district, state, parent key)
UNIVERSITIES = {
    "sppu": ("U-0323", "Savitribai Phule Pune University", "Pune", "Maharashtra"),
    "mu": ("U-0318", "University of Mumbai", "Mumbai", "Maharashtra"),
}
COLLEGES = {
    "fergusson": ("C-41431", "Deccan Education Societys Fergusson College, (Arts, Science), Pune", "Pune",
                  "Maharashtra", "sppu"),
    "ruia": ("C-33742", "Shikshana Prasark Mandals Ramnarain Ruia College", "Mumbai", "Maharashtra", "mu"),
    "xaviers": ("C-33555", "St Xaviers College Mahapalika Marg Mumbai", "Mumbai", "Maharashtra", "mu"),
    "jaihind": ("C-34083", "Jai Hind College, Churchgate, Mumbai", "Mumbai", "Maharashtra", "mu"),
}
STANDALONE = ("S-1808", "6013-Government Polytechnic, Pune", "Pune", "Maharashtra")

PINCODES_CSV = b"""pincode,officename,district,statename
411004,Deccan Gymkhana,Pune,Maharashtra
411005,Shivajinagar,Pune,Maharashtra
411007,Ganeshkhind,Pune,Maharashtra
411016,Gokhalenagar,Pune,Maharashtra
400001,Mumbai GPO,Mumbai,Maharashtra
400019,Matunga,Mumbai,Maharashtra
400020,Churchgate,Mumbai,Maharashtra
400032,Fort,Mumbai,Maharashtra
"""

SUBJECTS = [(1, "CS101", "Programming in C", 4), (1, "MA101", "Calculus", 4),
            (2, "CS201", "Data Structures", 4), (2, "MA201", "Discrete Mathematics", 4)]

# email, first, last, mobile, abc, dob, gender, category, enrolment no, college key, cgpa (sem 1, sem 2)
STUDENTS = [
    ("asha@student.demo.in", "Asha", "Patil", "9811000001", "123456789001", "2004-05-10", "FEMALE", "OPEN",
     "FC24CS001", "fergusson", (8.2, 8.4)),
    ("rohan@student.demo.in", "Rohan", "More", "9811000002", "123456789002", "2004-11-22", "MALE", "SC",
     "FC24CS002", "fergusson", (7.4, 7.6)),
    ("priya@student.demo.in", "Priya", "Nair", "9811000003", "123456789003", "2004-03-14", "FEMALE", "OPEN",
     "FC24CS003", "fergusson", (7.9, 8.1)),
    ("karan@student.demo.in", "Karan", "Shinde", "9811000004", "123456789004", "2004-07-01", "MALE", "OPEN",
     "FC24CS004", "fergusson", (7.0, 7.2)),
    ("sneha@student.demo.in", "Sneha", "Jadhav", "9811000005", "123456789005", "2004-09-18", "FEMALE", "OBC_NCL",
     "FC24CS005", "fergusson", (8.0, 7.8)),
    ("aman@student.demo.in", "Aman", "Kulkarni", "9811000006", "123456789006", "2004-01-27", "MALE", "OPEN",
     "FC24CS006", "fergusson", (7.3, 7.5)),
    ("vikram@student.demo.in", "Vikram", "Deshpande", "9811000007", "123456789007", "2004-12-05", "MALE", "EWS",
     "FC24CS007", "fergusson", (7.6, 7.7)),
    ("neha@student.demo.in", "Neha", "Gupta", "9811000008", "123456789008", "2004-08-30", "FEMALE", "OPEN",
     "JH24CS001", "jaihind", (8.5, 8.6)),
]


class SeedError(Exception):
    pass


# ======================================================================
# Files and the API client
# ======================================================================
def pdf(title, lines=()):
    """A small real PDF, so documents open properly in the app."""
    from reportlab.lib.pagesizes import A4
    from reportlab.pdfgen import canvas
    buf = io.BytesIO()
    c = canvas.Canvas(buf, pagesize=A4)
    c.setFont("Helvetica-Bold", 16)
    c.drawString(60, 780, title)
    c.setFont("Helvetica", 11)
    y = 750
    for line in ["Demo document created by scripts/seed.py", *lines]:
        c.drawString(60, y, line)
        y -= 18
    c.showPage()
    c.save()
    buf.seek(0)
    return buf


def png(color=(30, 90, 160)):
    from PIL import Image
    buf = io.BytesIO()
    Image.new("RGB", (240, 300), color).save(buf, format="PNG")
    buf.seek(0)
    return buf


def xlsx(rows):
    from openpyxl import Workbook
    wb = Workbook()
    ws = wb.active
    for r in rows:
        ws.append(r)
    buf = io.BytesIO()
    wb.save(buf)
    buf.seek(0)
    return buf


class Api:
    def __init__(self, app):
        self.client = app.test_client()
        self.prefix = app.config["API_PREFIX"]
        self.tokens = {}

    def call(self, method, path, token=None, json=None, data=None, expect=200, ip=None):
        kw = {"headers": {}}
        if token:
            kw["headers"]["Authorization"] = f"Bearer {token}"
        if ip:
            kw["environ_base"] = {"REMOTE_ADDR": ip}
            kw["headers"]["X-Forwarded-For"] = ip
        if json is not None:
            kw["json"] = json
        if data is not None:
            kw["data"] = data
            kw["content_type"] = "multipart/form-data"
        resp = getattr(self.client, method)(self.prefix + path, **kw)
        body = resp.get_json(silent=True) or {}
        if expect is not None and resp.status_code != expect:
            raise SeedError(f"{method.upper()} {path} -> {resp.status_code} (expected {expect}): "
                            f"{body.get('errors') or body.get('message')}")
        return body.get("data")

    def get(self, path, token=None, **kw):
        return self.call("get", path, token, **kw)

    def post(self, path, token=None, **kw):
        return self.call("post", path, token, **kw)

    def put(self, path, token=None, **kw):
        return self.call("put", path, token, **kw)

    def patch(self, path, token=None, **kw):
        return self.call("patch", path, token, **kw)

    def login(self, email, password):
        """Full login including the email OTP step. One login per user (a new login ends the old session)."""
        if email in self.tokens:
            return self.tokens[email]
        data = self.post("/auth/login", json={"email": email, "password": password})
        if data.get("two_factor_required"):
            data = self.post("/auth/login/verify-otp", token=data["pre_auth_token"], json={"otp": data["debug_otp"]})
        self.tokens[email] = data["access_token"]
        return self.tokens[email]

    def verify_email(self, email, otp):
        return self.post("/auth/verify-email", json={"email": email, "otp": otp})["account_state"]

    def accept(self, token, email, name, password, mobile, designation, letter=False):
        body = {"full_name": name, "password": password, "confirm_password": password, "mobile": mobile,
                "designation": designation}
        if letter:
            body["authorization_letter"] = (pdf("Authorization letter", [f"{name}, {designation}"]), "authorization.pdf")
            res = self.post(f"/onboarding/invitations/{token}/accept", data=body, expect=201)
        else:
            res = self.post(f"/onboarding/invitations/{token}/accept", json=body, expect=201)
        state = self.verify_email(email, res["debug_otp"])
        return res, state


# ======================================================================
# Steps
# ======================================================================
def check_empty():
    from app.models import User
    if User.query.filter(User.email.like("%demo.in")).first():
        raise SeedError("Demo users already exist. Empty the tables (except the AISHE ones) to run it again.")


def ensure_aishe_rows():
    """Use the AISHE rows; create any that are missing (when load_universities.py has not run)."""
    from app.extensions import db
    from app.models import College, District, State, University

    def geo(state_name, district_name):
        st = State.query.filter(db.func.lower(State.name) == state_name.lower()).first()
        if st is None:
            st = State(name=state_name)
            db.session.add(st)
            db.session.flush()
        dt = District.query.filter(District.state_id == st.id,
                                   db.func.lower(District.name) == district_name.lower()).first()
        if dt is None:
            dt = District(name=district_name, state_id=st.id)
            db.session.add(dt)
            db.session.flush()
        return st, dt

    out = {}
    for key, (code, name, district, state) in UNIVERSITIES.items():
        row = University.query.filter_by(aishe_code=code).first()
        if row is None:
            st, dt = geo(state, district)
            row = University(aishe_code=code, name=name, state_id=st.id, district_id=dt.id)
            db.session.add(row)
            db.session.flush()
            print(f"  (AISHE row {code} not found: created it)")
        out[key] = row
    for key, (code, name, district, state, parent) in COLLEGES.items():
        row = College.query.filter_by(aishe_code=code, institution_type="COLLEGE").first()
        if row is None:
            st, dt = geo(state, district)
            row = College(institution_type="COLLEGE", aishe_code=code, name=name, state_id=st.id,
                          district_id=dt.id, university_id=out[parent].id)
            db.session.add(row)
            db.session.flush()
            print(f"  (AISHE row {code} not found: created it)")
        out[key] = row
    code, name, district, state = STANDALONE
    row = College.query.filter_by(aishe_code=code, institution_type="STANDALONE").first()
    if row is None:
        st, dt = geo(state, district)
        row = College(institution_type="STANDALONE", aishe_code=code, name=name, state_id=st.id, district_id=dt.id)
        db.session.add(row)
        db.session.flush()
    out["poly"] = row
    db.session.commit()
    return {k: v.id for k, v in out.items()}, {k: (v.state_id, v.district_id) for k, v in out.items()}


def seed_master_data():
    from scripts.load_pincodes import import_pincodes
    from scripts.seed_data import seed_letter_templates
    batch = import_pincodes("demo_pincodes.csv", PINCODES_CSV)
    print(f"  pincodes: {batch.imported_rows} loaded")
    seed_letter_templates()


def bootstrap_platform_admin():
    """The first Platform Admin is inserted directly: nobody exists yet to invite them."""
    from app.core.security import hash_password
    from app.core.utils import utcnow
    from app.enums import AccountState, UserType
    from app.extensions import db
    from app.models import User
    db.session.add(User(email="pa1@demo.in", full_name="Anita Rao", designation="Platform Operations Lead",
                        mobile="9800000001", password_hash=hash_password(PASSWORDS["PLATFORM_ADMIN"]),
                        password_history=[], password_changed_at=utcnow(), user_type=UserType.PLATFORM_ADMIN,
                        account_state=AccountState.ACTIVE, email_verified_at=utcnow()))
    db.session.commit()


def onboard(api, ids):
    P = PASSWORDS
    pa = api.login("pa1@demo.in", P["PLATFORM_ADMIN"])

    # Second Platform Admin by invitation
    inv = api.post("/onboarding/platform-admins", pa, json={"email": "pa2@demo.in", "full_name": "Vivek Shah"},
                   expect=201)
    api.accept(inv["debug_token"], "pa2@demo.in", "Vivek Shah", P["PLATFORM_ADMIN"], "9800000002",
               "Platform Operations")

    # SPPU: path A (Platform Admin creates it, admin pre-approved)
    res = api.post("/onboarding/universities", pa, expect=201, json={
        "aishe_code": UNIVERSITIES["sppu"][0], "admin_email": "admin@unipune.demo.in",
        "admin_name": "Dr. Prafulla Pawar"})
    api.accept(res["debug_token"], "admin@unipune.demo.in", "Dr. Prafulla Pawar", P["UNIVERSITY_ADMIN"],
               "9800000011", "Registrar")

    # University of Mumbai: path B (signup link, authorization letter, Platform Admin approves)
    res = api.post("/onboarding/universities/signup-links", pa, expect=201, json={
        "aishe_code": UNIVERSITIES["mu"][0], "email": "admin@mu.demo.in", "note": "Please register the university"})
    acc, _ = api.accept(res["debug_token"], "admin@mu.demo.in", "Dr. Baliram Gaikwad", P["UNIVERSITY_ADMIN"],
                        "9800000012", "Registrar", letter=True)
    api.post(f"/onboarding/registrations/{acc['registration_request_id']}/approve", pa,
             json={"note": "Verified with the Registrar's office by phone"})

    sppu_admin = api.login("admin@unipune.demo.in", P["UNIVERSITY_ADMIN"])
    mu_admin = api.login("admin@mu.demo.in", P["UNIVERSITY_ADMIN"])

    # Fergusson: path A by SPPU
    res = api.post(f"/onboarding/universities/{ids['sppu']}/colleges", sppu_admin, expect=201, json={
        "aishe_code": COLLEGES["fergusson"][0], "admin_email": "principal@fergusson.demo.in",
        "admin_name": "Dr. Nitin Kulkarni"})
    api.accept(res["debug_token"], "principal@fergusson.demo.in", "Dr. Nitin Kulkarni", P["COLLEGE_ADMIN"],
               "9800000021", "Principal")

    # Ruia: path C (public registration form), approved by Mumbai University
    res = api.post("/onboarding/registrations", expect=201, data={
        "institution_type": "COLLEGE", "aishe_code": COLLEGES["ruia"][0], "full_name": "Dr. Anushree Lokur",
        "email": "principal@ruia.demo.in", "password": P["COLLEGE_ADMIN"], "confirm_password": P["COLLEGE_ADMIN"],
        "mobile": "9800000022", "designation": "Principal",
        "authorization_letter": (pdf("Authorization letter", ["Dr. Anushree Lokur, Principal"]), "letter.pdf")})
    api.verify_email("principal@ruia.demo.in", res["debug_otp"])
    api.post(f"/onboarding/registrations/{res['registration_request_id']}/approve", mu_admin,
             json={"note": "Affiliation confirmed"})

    # St Xavier's: path C, still waiting for Mumbai University (pending registration)
    res = api.post("/onboarding/registrations", expect=201, data={
        "institution_type": "COLLEGE", "aishe_code": COLLEGES["xaviers"][0], "full_name": "Fr. Joseph D'Souza",
        "email": "principal@xaviers.demo.in", "password": P["COLLEGE_ADMIN"], "confirm_password": P["COLLEGE_ADMIN"],
        "mobile": "9800000025", "designation": "Principal",
        "authorization_letter": (pdf("Authorization letter", ["Fr. Joseph D'Souza, Principal"]), "letter.pdf")})
    api.verify_email("principal@xaviers.demo.in", res["debug_otp"])

    # Government Polytechnic Pune (standalone): signup link sent, not used yet
    api.post("/onboarding/standalone-institutions/signup-links", pa, expect=201, json={
        "aishe_code": STANDALONE[0], "email": "principal@gppune.demo.in", "note": "Signup link for the principal"})

    ruia_admin = api.login("principal@ruia.demo.in", P["COLLEGE_ADMIN"])
    ferg_admin = api.login("principal@fergusson.demo.in", P["COLLEGE_ADMIN"])

    # University users (staff invitations)
    for kind_id, admin, email, name, designation, mobile, bundles in (
            (ids["sppu"], sppu_admin, "academics@unipune.demo.in", "Meera Kulkarni", "Deputy Registrar (Academics)",
             "9800000013", ["ACADEMIC_MANAGER", "REPORT_VIEWER"]),
            (ids["mu"], mu_admin, "approvals@mu.demo.in", "Sanjay Deshmukh", "Assistant Registrar (Eligibility)",
             "9800000014", ["UNIVERSITY_APPROVER", "ACADEMIC_MANAGER", "REPORT_VIEWER"])):
        inv = api.post(f"/institutions/university/{kind_id}/invitations", admin, expect=201,
                       json={"email": email, "full_name": name, "bundles": bundles})
        api.accept(inv["debug_token"], email, name, P["UNIVERSITY_USER"], mobile, designation)

    # College user at Fergusson by invitation
    inv = api.post(f"/institutions/college/{ids['fergusson']}/invitations", ferg_admin, expect=201, json={
        "email": "office@fergusson.demo.in", "full_name": "Rahul Joshi",
        "bundles": ["ENROLMENT_VERIFIER", "DOCUMENT_VERIFIER", "APPROVER"]})
    api.accept(inv["debug_token"], "office@fergusson.demo.in", "Rahul Joshi", P["COLLEGE_USER"], "9800000023",
               "Office Superintendent")

    # College user at Ruia by access request, approved by the principal
    res = api.post("/access-requests", expect=201, json={
        "institution_kind": "COLLEGE", "institution_id": ids["ruia"], "full_name": "Pooja Iyer",
        "email": "admissions@ruia.demo.in", "password": P["COLLEGE_USER"], "confirm_password": P["COLLEGE_USER"],
        "mobile": "9800000024", "designation": "Admissions Coordinator"})
    api.verify_email("admissions@ruia.demo.in", res["debug_otp"])
    api.post(f"/memberships/{res['membership_id']}/access-decision", ruia_admin, json={
        "approve": True, "bundles": ["DOCUMENT_VERIFIER", "REVIEWER", "CREDIT_ASSESSOR", "INTERVIEWER",
                                     "VACANCY_MANAGER"]})

    # A second access request at Ruia, still pending
    res = api.post("/access-requests", expect=201, json={
        "institution_kind": "COLLEGE", "institution_id": ids["ruia"], "full_name": "Manoj Pillai",
        "email": "clerk@ruia.demo.in", "password": P["COLLEGE_USER"], "confirm_password": P["COLLEGE_USER"],
        "mobile": "9800000026", "designation": "Clerk"})
    api.verify_email("clerk@ruia.demo.in", res["debug_otp"])

    # Letter branding for Mumbai University and a university-specific offer letter template
    api.put(f"/institutions/university/{ids['mu']}/branding", mu_admin, data={
        "letter_header": "University of Mumbai | Fort Campus, M G Road, Mumbai 400032",
        "letter_footer": "This letter is system generated and can be verified with the QR code.",
        "seal": (png((120, 20, 20)), "seal.png")})
    api.put("/letter-templates", mu_admin, json={
        "letter_type": "PROVISIONAL_OFFER", "university_id": ids["mu"],
        "subject": "Provisional offer of admission - {course_name}",
        "body": ("Dear {student_name},\n\nUniversity of Mumbai is pleased to offer you provisional admission to "
                 "{course_name} at {destination_name}, semester {entry_semester}, academic year {academic_year} "
                 "(reference {reference_no}). Please accept this offer by {offer_expires_on}.\n\n"
                 "Additional papers to clear: {additional_papers}\n\nRegistrar, University of Mumbai")})
    print("  onboarding: 2 universities, 2 colleges active; 1 college registration and 1 standalone signup pending")


def academics(api, ids):
    from app.core.utils import today_ist
    P = PASSWORDS
    today = today_ist()
    start = (today.replace(month=6, day=15) if today.month >= 6
             else today.replace(year=today.year - 1, month=6, day=15))
    label = f"{start.year}-{str(start.year + 1)[2:]}"
    end = start.replace(year=start.year + 1) - timedelta(days=1)

    mu_admin = api.login("admin@mu.demo.in", P["UNIVERSITY_ADMIN"])
    approvals = api.login("approvals@mu.demo.in", P["UNIVERSITY_USER"])
    sppu_academics = api.login("academics@unipune.demo.in", P["UNIVERSITY_USER"])
    ruia_admin = api.login("principal@ruia.demo.in", P["COLLEGE_ADMIN"])
    ruia_staff = api.login("admissions@ruia.demo.in", P["COLLEGE_USER"])

    years = {}
    for key, token in (("mu", mu_admin), ("sppu", sppu_academics)):
        years[key] = api.post(f"/academic-years?university_id={ids[key]}", token, expect=201, json={
            "label": label, "start_date": str(start), "end_date": str(end), "is_current": True})["id"]

    # Grading formulas (CGPA -> percentage)
    for key, token, url in (("sppu", sppu_academics, "https://www.unipune.ac.in/"),
                            ("mu", mu_admin, "https://mu.ac.in/")):
        api.post(f"/universities/{ids[key]}/grading-schemes", token, expect=201, json={
            "grade_system": "CGPA_10", "formula_expr": "cgpa * 9.5", "batch_from": 2020, "source_url": url})

    # Courses at Mumbai University
    courses = {}
    for code, name, interview in (("BSC-CS", "B.Sc Computer Science", False),
                                  ("BSC-IT", "B.Sc Information Technology", True),
                                  ("BSC-DS", "B.Sc Data Science", False)):
        courses[code] = api.post(f"/universities/{ids['mu']}/courses", mu_admin, expect=201, json={
            "code": code, "name": name, "level": "UNDERGRADUATE", "discipline": "Computer Science",
            "duration_years": 3, "total_semesters": 6, "offer_mode": "ROLLING", "interview_required": interview,
            "confirm_similar": True})["id"]

    syl = api.post(f"/universities/{ids['mu']}/syllabi", mu_admin, expect=201, json={
        "name": "B.Sc Computer Science (2023 pattern)", "regulation_year": 2023,
        "subjects": [{"semester": s, "code": c, "name": n, "credits": cr} for s, c, n, cr in SUBJECTS]})["id"]
    api.post(f"/syllabi/{syl}/publish", mu_admin)
    for code in ("BSC-CS", "BSC-IT"):
        api.post(f"/courses/{courses[code]}/syllabi", mu_admin, expect=201,
                 json={"syllabus_id": syl, "batch_from": 2020})
        api.post(f"/courses/{courses[code]}/eligibility-rules", mu_admin, expect=201, json={
            "effective_from": str(today - timedelta(days=30)), "min_percentage": 60, "min_completed_semesters": 2})
        api.post(f"/courses/{courses[code]}/migration-windows", mu_admin, expect=201, json={
            "academic_year_id": years["mu"], "opens_on": str(today), "closes_on": str(today + timedelta(days=45)),
            "entry_semesters": [3]})
    api.post(f"/courses/{courses['BSC-CS']}/equivalences", mu_admin, expect=201, json={
        "source_course_name": "B.Sc (Computer Science)", "source_university_id": ids["sppu"],
        "source_course_level": "UNDERGRADUATE", "note": "SPPU B.Sc (Computer Science) is equivalent"})

    # Offerings: CS assigned directly; IT requested by Ruia and approved; DS requested and pending
    cs_offering = api.post(f"/courses/{courses['BSC-CS']}/offerings", mu_admin, expect=201,
                           json={"college_id": ids["ruia"], "sanctioned_intake": 60})["id"]
    req = api.post(f"/colleges/{ids['ruia']}/offering-requests", ruia_admin, expect=201, json={
        "course_id": courses["BSC-IT"], "proposed_intake": 40, "justification": "Labs and faculty are in place"})
    decided = api.post(f"/offering-requests/{req['id']}/decision", approvals,
                       json={"approve": True, "sanctioned_intake": 40, "note": "Approved"})
    it_offering = decided["offering"]["id"]
    api.post(f"/colleges/{ids['ruia']}/offering-requests", ruia_admin, expect=201, json={
        "course_id": courses["BSC-DS"], "proposed_intake": 30, "justification": "Industry demand"})

    # Vacancies: CS typed in, IT by Excel bulk upload
    api.put("/vacancies", ruia_staff, json={"offering_id": cs_offering, "academic_year_id": years["mu"],
                                            "entry_semester": 3, "categories": {"OPEN": 2, "SC": 1, "OBC_NCL": 1}})
    api.post(f"/institutions/college/{ids['ruia']}/bulk-uploads/VACANCIES", ruia_staff, expect=201, data={
        "file": (xlsx([["course_code", "academic_year", "entry_semester", "OPEN", "SC"],
                       ["BSC-IT", label, 3, 2, 1],
                       ["BSC-XX", label, 3, 1, 0]]), "vacancies.xlsx")})

    # Interview hours at Ruia, Monday to Saturday
    api.put(f"/colleges/{ids['ruia']}/interview-hours", ruia_admin, json={"hours": [
        {"weekday": d, "start_time": "10:00:00", "end_time": "17:00:00", "slot_minutes": 30}
        for d in ("MON", "TUE", "WED", "THU", "FRI", "SAT")]})
    print(f"  academics: year {label}, 3 courses, windows open until {today + timedelta(days=45)}")
    return {"cs": cs_offering, "it": it_offering, "courses": courses, "years": years}


def students(api, ids, geo):
    P = PASSWORDS
    tokens = {}
    for email, first, last, mobile, abc, dob, gender, cat, enrol, college, cgpas in STUDENTS:
        res = api.post("/students", expect=201, json={
            "first_name": first, "last_name": last, "email": email, "mobile": mobile, "password": P["STUDENT"],
            "confirm_password": P["STUDENT"], "date_of_birth": dob, "gender": gender, "category": cat,
            "abc_account_id": abc, "consent": True, "whatsapp_opt_in": first in ("Asha", "Priya")})
        api.verify_email(email, res["debug_otp"])
        tok = api.login(email, P["STUDENT"])
        state_id, district_id = geo[college]
        api.patch("/students/me", tok, json={"address_line": "12 F C Road, Shivajinagar", "state_id": state_id,
                                             "district_id": district_id, "pincode": "411004",
                                             "guardian_name": f"Mr. {last}", "guardian_mobile": "9822000000"})
        api.post("/students/me/enrolment", tok, data={
            "current_college_id": str(ids[college]), "enrolment_number": enrol,
            "current_course_name": "B.Sc Computer Science", "current_course_level": "UNDERGRADUATE",
            "current_discipline": "Computer Science", "current_semester": "3", "batch_year": "2024",
            "proof": (pdf("College identity card", [f"{first} {last}", enrol]), "id_card.pdf")})
        for sem, cgpa in enumerate(cgpas, start=1):
            api.post("/students/me/academic-records", tok, expect=201, json={
                "semester": sem, "grade_system": "CGPA_10", "sgpa": cgpa, "cgpa": cgpa, "credits_earned": 20,
                "subjects": [{"subject_code": c, "subject_name": n, "credits": cr, "grade": "A"}
                             for s, c, n, cr in SUBJECTS if s == sem]})
        for doc_type, f in (("PHOTO", (png(), "photo.png")),
                            ("ID_PROOF", (pdf("Aadhaar (masked)", [f"{first} {last}"]), "id_proof.pdf")),
                            ("CLASS_10_MARKSHEET", (pdf("Class 10 marksheet", [f"{first} {last}"]), "class10.pdf")),
                            ("CLASS_12_MARKSHEET", (pdf("Class 12 marksheet", [f"{first} {last}"]), "class12.pdf")),
                            ("SEMESTER_MARKSHEET", (pdf("Semester 1-2 marksheet", [f"CGPA {cgpas[1]}"]),
                                                    "sem_marksheet.pdf")),
                            ("ABC_CREDIT_STATEMENT", (pdf("ABC credit statement", [abc]), "abc.pdf"))):
            api.post("/documents/mine", tok, expect=201, data={"doc_type": doc_type, "file": f})
        if cat != "OPEN":
            api.post("/documents/mine", tok, expect=201, data={
                "doc_type": "CATEGORY_CERTIFICATE",
                "file": (pdf(f"{cat} category certificate", [f"{first} {last}"]), "category.pdf")})
        tokens[email] = tok

    # Fergusson's office verifies its students' enrolment (Neha's college is not onboarded)
    office = api.login("office@fergusson.demo.in", P["COLLEGE_USER"])
    for row in api.get("/enrolment-verifications?page_size=50", office):
        api.post(f"/enrolment-verifications/{row['id']}/decision", office, json={"approve": True})

    # Neha asks for her college (Jai Hind) to be brought onto the platform
    api.post("/students/me/college-onboarding", tokens["neha@student.demo.in"], expect=201, json={
        "college_id": ids["jaihind"], "contact_email": "principal@jaihind.demo.in", "contact_name": "Principal",
        "note": "My college is not on the platform yet, so my enrolment cannot be verified."})
    print(f"  students: {len(STUDENTS)} registered, 7 enrolments verified, 1 college onboarding request")
    return tokens


def migration_requests(api, ids, setup, st):
    from app.core.utils import IST
    P = PASSWORDS
    ferg_office = api.login("office@fergusson.demo.in", P["COLLEGE_USER"])
    ruia_admin = api.login("principal@ruia.demo.in", P["COLLEGE_ADMIN"])
    ruia_staff = api.login("admissions@ruia.demo.in", P["COLLEGE_USER"])
    approvals = api.login("approvals@mu.demo.in", P["UNIVERSITY_USER"])
    sppu_admin = api.login("admin@unipune.demo.in", P["UNIVERSITY_ADMIN"])

    def create(student, offering):
        tok = st[student]
        rid = api.post("/migration-requests", tok, expect=201, json={"offering_id": offering, "entry_semester": 3})["id"]
        api.put(f"/migration-requests/{rid}/grade-conversion", tok, json={})
        return rid

    def submit(student, offering):
        rid = create(student, offering)
        api.post(f"/migration-requests/{rid}/submit", st[student])
        return rid

    def detail(rid, tok):
        return api.get(f"/migration-requests/{rid}", tok)

    def noc(rid):
        api.post(f"/migration-requests/{rid}/noc", ferg_office,
                 json={"approve": True, "note": "No dues", "version": detail(rid, ferg_office)["version"]})

    def review(rid):
        api.post(f"/migration-requests/{rid}/start-review", ruia_staff)
        d = detail(rid, ruia_staff)
        for item in d["checklist"]:
            for doc in item["documents"]:
                if doc["verification_status"] != "VERIFIED":
                    api.post(f"/documents/{doc['id']}/verify", ruia_staff, json={"decision": "VERIFIED"})
        api.put(f"/migration-requests/{rid}/credits", ruia_staff,
                json={"decisions": [{"id": c["id"], "decision": "ACCEPTED"} for c in d["credits"]]})

    def recommend_and_approve(rid):
        api.post(f"/migration-requests/{rid}/recommend", ruia_admin, json={"note": "Documents and credits in order"})
        api.post(f"/migration-requests/{rid}/approve-entry", approvals, json={
            "is_eligible": True, "additional_papers": ["Discrete Mathematics"], "remarks": "Eligible",
            "version": detail(rid, approvals)["version"]})

    # Asha: the whole way to ADMITTED (cross-university: TC + migration certificate)
    rid = submit("asha@student.demo.in", setup["cs"])
    noc(rid)
    review(rid)
    recommend_and_approve(rid)
    api.post(f"/migration-requests/{rid}/accept-offer", st["asha@student.demo.in"])
    api.post(f"/migration-requests/{rid}/transfer-certificate", ferg_office,
             data={"file": (pdf("Transfer certificate", ["Asha Patil"]), "tc.pdf")})
    api.post(f"/migration-requests/{rid}/university-release", sppu_admin,
             data={"file": (pdf("Migration certificate", ["Savitribai Phule Pune University"]), "mc.pdf")})
    api.post(f"/migration-requests/{rid}/confirm-admission", ruia_admin,
             json={"fee_paid": True, "version": detail(rid, ruia_admin)["version"]})
    api.post(f"/migration-requests/{rid}/verify-originals", ruia_admin)

    # Rohan: provisional offer, waiting for him to accept (SC seat blocked)
    rid = submit("rohan@student.demo.in", setup["cs"])
    noc(rid)
    review(rid)
    recommend_and_approve(rid)

    # Priya: B.Sc IT needs an interview; one is scheduled at Ruia (online, with the college's own link)
    if True:
        rid = submit("priya@student.demo.in", setup["it"])
        noc(rid)
        review(rid)
        day = datetime.now(IST).date() + timedelta(days=2)
        while day.weekday() == 6:  # no Sunday hours
            day += timedelta(days=1)
        start = datetime.combine(day, datetime.min.time(), tzinfo=IST).replace(hour=11)
        panel = [api.get("/auth/me", ruia_staff)["id"], api.get("/auth/me", ruia_admin)["id"]]
        api.post("/interviews", ruia_staff, expect=201, json={
            "request_id": rid, "scheduled_start": start.isoformat(),
            "scheduled_end": (start + timedelta(minutes=30)).isoformat(), "mode": "ONLINE",
            "meeting_url": "https://meet.google.com/abc-defg-hij", "meeting_info": "Join 5 minutes early",
            "panel_user_ids": panel})

    # Karan: NOC refused, he has appealed (waiting for SPPU)
    rid = submit("karan@student.demo.in", setup["cs"])
    api.post(f"/migration-requests/{rid}/noc", ferg_office,
             json={"approve": False, "refusal_reason": "DUES_PENDING", "note": "Library fine of Rs 450 pending"})
    api.post(f"/migration-requests/{rid}/appeal", st["karan@student.demo.in"], expect=201,
             json={"reason": "The library fine was paid on the same day. The receipt number is LIB/2026/0912."})

    # Sneha: submitted, waiting for the NOC
    submit("sneha@student.demo.in", setup["cs"])

    # Aman: under review, returned to him for a correction
    rid = submit("aman@student.demo.in", setup["cs"])
    noc(rid)
    api.post(f"/migration-requests/{rid}/start-review", ruia_staff)
    api.post(f"/migration-requests/{rid}/return-for-correction", ruia_staff, json={
        "note": "The semester 2 marksheet is not readable. Please upload a clear scan.",
        "version": detail(rid, ruia_staff)["version"]})

    # Vikram: a draft he has not submitted yet
    create("vikram@student.demo.in", setup["cs"])
    print("  migration requests: ADMITTED, PROVISIONAL_OFFER, INTERVIEW_SCHEDULED, REJECTED + appeal, "
          "SUBMITTED, RETURNED_FOR_CORRECTION, DRAFT")


def support_and_privacy(api, st):
    P = PASSWORDS
    pa = api.login("pa1@demo.in", P["PLATFORM_ADMIN"])
    t = api.post("/support-tickets", st["rohan@student.demo.in"], expect=201, json={
        "category": "TECHNICAL", "subject": "Offer letter does not download",
        "description": "When I click download on my offer letter nothing happens on my phone."})
    api.post(f"/support-tickets/{t['id']}/replies", pa, expect=201,
             json={"message": "Please try again from a browser; we are checking the mobile app."})
    api.post(f"/support-tickets/{t['id']}/replies", pa, expect=201,
             json={"message": "Checked logs: the download works on Chrome.", "is_internal": True})
    api.patch(f"/support-tickets/{t['id']}", pa, json={"ticket_state": "IN_PROGRESS"})
    api.post("/support-tickets", st["karan@student.demo.in"], expect=201, json={
        "category": "GRIEVANCE", "subject": "NOC refused for a fine I already paid",
        "description": "My NOC was refused for a library fine that I had paid. I have filed an appeal."})

    d = api.post("/privacy/requests", st["asha@student.demo.in"], expect=201,
                 json={"request_type": "DOWNLOAD", "details": "Please send me a copy of my data."})
    api.post(f"/privacy/requests/{d['id']}/complete", pa,
             json={"approve": True, "response_note": "Your data export is available under My data."})
    api.post("/privacy/requests", st["priya@student.demo.in"], expect=201,
             json={"request_type": "CORRECTION", "details": "My date of birth is shown as 14 March; it is 15 March."})

    # A password reset link requested (one_time_tokens) and a blocked IP after repeated failed logins
    api.post("/auth/forgot-password", json={"email": "vikram@student.demo.in"})
    for _ in range(10):
        api.post("/auth/login", json={"email": "someone@unknown.in", "password": "Wrong@1234"},
                 expect=None, ip="203.0.113.50")
    print("  support: 2 tickets, 2 data requests, 1 password reset link, 1 blocked IP")


def print_logins():
    rows = [
        ("Platform Admin", "pa1@demo.in", "PLATFORM_ADMIN", "Platform"),
        ("Platform Admin", "pa2@demo.in", "PLATFORM_ADMIN", "Platform"),
        ("University Admin", "admin@unipune.demo.in", "UNIVERSITY_ADMIN", "Savitribai Phule Pune University"),
        ("University Admin", "admin@mu.demo.in", "UNIVERSITY_ADMIN", "University of Mumbai"),
        ("University User", "academics@unipune.demo.in", "UNIVERSITY_USER", "Savitribai Phule Pune University"),
        ("University User", "approvals@mu.demo.in", "UNIVERSITY_USER", "University of Mumbai"),
        ("College Admin", "principal@fergusson.demo.in", "COLLEGE_ADMIN", "Fergusson College, Pune"),
        ("College Admin", "principal@ruia.demo.in", "COLLEGE_ADMIN", "Ruia College, Mumbai"),
        ("College User", "office@fergusson.demo.in", "COLLEGE_USER", "Fergusson College, Pune"),
        ("College User", "admissions@ruia.demo.in", "COLLEGE_USER", "Ruia College, Mumbai"),
    ]
    stage = {"asha": "ADMITTED at Ruia", "rohan": "offer waiting", "priya": "interview scheduled",
             "karan": "NOC refused, appealed", "sneha": "submitted", "aman": "returned for correction",
             "vikram": "draft", "neha": "Jai Hind (not onboarded)"}
    rows += [("Student", s[0], "STUDENT", stage[s[1].lower()]) for s in STUDENTS]
    rows += [("Pending (approval)", "principal@xaviers.demo.in", "COLLEGE_ADMIN", "St Xavier's registration"),
             ("Pending (approval)", "clerk@ruia.demo.in", "COLLEGE_USER", "Ruia access request")]
    print("\n" + "-" * 100)
    print(f"{'Role':<20}{'Email':<32}{'Password':<18}Notes")
    print("-" * 100)
    for label, email, role, note in rows:
        print(f"{label:<20}{email:<32}{PASSWORDS[role]:<18}{note}")
    print("-" * 100)
    print("Admins and approvers also get a login OTP: in development it is in the login response\n"
          "(debug_otp) and printed in the console.")


def main(argv=None):
    parser = argparse.ArgumentParser(description="Seed demo data into every table.")
    parser.add_argument("--env", default=os.environ.get("APP_ENV", "development"),
                        help="Config to use (default: APP_ENV, else development)")
    args = parser.parse_args(argv)
    if args.env == "production":
        sys.exit("This is demo data with known passwords. It will not run with the production config.")

    from app import create_app
    from app.extensions import db, limiter
    app = create_app(args.env, start_scheduler=False)
    # Demo run: OTPs in responses, messages to the console, no rate limits, notifications sent inline
    app.config.update(EXPOSE_DEBUG_OTP=True, NOTIFICATIONS_SEND_ASYNC=False, EMAIL_BACKEND="console",
                      SMS_BACKEND="console", WHATSAPP_BACKEND="console")
    limiter.enabled = False
    import logging
    import warnings
    warnings.filterwarnings("ignore")
    logging.getLogger("app").setLevel(logging.WARNING)

    started = datetime.now()
    with app.app_context():
        try:
            check_empty()
            print("1. Master data")
            ids, geo = ensure_aishe_rows()
            seed_master_data()
            bootstrap_platform_admin()
            print("2. Onboarding")
            api = Api(app)
            onboard(api, ids)
            print("3. Academic setup")
            setup = academics(api, ids)
            print("4. Students")
            st = students(api, ids, geo)
            print("5. Migration requests")
            migration_requests(api, ids, setup, st)
            print("6. Support and privacy")
            support_and_privacy(api, st)
        except SeedError as exc:
            db.session.rollback()
            sys.exit(f"Seed stopped: {exc}")
        print_logins()
        from sqlalchemy import inspect, text
        empty = [t for t in sorted(inspect(db.engine).get_table_names())
                 if db.session.execute(text(f'SELECT EXISTS (SELECT 1 FROM "{t}")')).scalar() is False]
        print("Tables still empty: " + (", ".join(empty) if empty else "none"))
    print(f"Done in {int((datetime.now() - started).total_seconds())} seconds.")


if __name__ == "__main__":
    main()
