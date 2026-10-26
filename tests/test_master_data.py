"""The AISHE loader (scripts/load_universities.py) writing into the app's tables, and
the PIN code loader (scripts/load_pincodes.py)."""
import os
import re

from openpyxl import Workbook

from app.extensions import db
from app.models import College, District, Pincode, State, University

SCRIPTS = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "scripts")
TITLE = [["All India Survey on Higher Education"], ["Downloaded on 23-10-2026"]]


def _xlsx(path, header, rows):
    wb = Workbook()
    ws = wb.active
    for r in TITLE + [header] + rows:
        ws.append(r)
    wb.save(path)


def _write_aishe_files(folder):
    _xlsx(folder / "University.xlsx",
          ["Aishe Code", "Name", "State", "District", "Website", "Year Of Establishment", "Location"],
          [["U-0450", "University of Mumbai", "MAHARASHTRA", "Mumbai", "mu.ac.in", 1857, "Urban"],
           ["U-0001", "Savitribai Phule Pune University", "Maharashtra", "Pune", "", 1949, "Urban"]])
    _xlsx(folder / "College.xlsx",
          ["Aishe Code", "Name", "State", "District", "Website", "Year Of Establishment", "Location",
           "College Type", "Manegement", "University Aishe Code", "University Name", "University Type"],
          [["C-201", "Ruia College", "Maharashtra", "Mumbai", "", 1937, "Urban", "AFFILIATED COLLEGE",
            "PRIVATE AIDED ", "U-0450", "University of Mumbai", "State Public"],
           ["C-101", "Fergusson College", "Maharashtra", "Pune", "", 1885, "Urban", "Affiliated College",
            "Private Aided", "U-0001", "", ""],
           ["C-999", "No University College", "Maharashtra", "Pune", "", "", "", "", "", "U-", "", ""]])
    _xlsx(folder / "Standalone.xlsx",
          ["Aishe Code", "Name", "State", "District", "Year Of Establishment", "Location", "Standalone Type",
           "Manegement"],
          [["S-5001", "Govt Polytechnic Pune", "Maharashtra", "Pune", 1960, "Urban", "DIPLOMA LEVEL TECHNICAL",
            "State Government"]])
    _xlsx(folder / "pm_vidya_lakshmi.xlsx",
          ["AISHE Code", "Institute Name", "State", "District", "Website", "Year Of Establishment", "Location",
           "Management Type"],
          [["C-201", "Ruia College", "Maharashtra", "Mumbai", "", 1937, "Urban", "Private Aided"]])
    _xlsx(folder / "RD.xlsx", ["AISHE Code", "Institute Name", "State Name", "District Name",
                               "Administrative Ministry"],
          [["R-0007", "National Chemical Laboratory", "Maharashtra", "Pune", "MINISTRY OF SCIENCE"]])


def _run_aishe_loader(app, folder, monkeypatch):
    """Run the user's loader unchanged, pointed at the test database."""
    with open(os.path.join(SCRIPTS, "load_universities.py")) as fh:
        source = fh.read()
    url = app.config["SQLALCHEMY_DATABASE_URI"]
    source = re.sub(r'^DATABASE_URL = .*$', f'DATABASE_URL = "{url}"', source, count=1, flags=re.M)
    monkeypatch.chdir(folder)
    db.session.commit()  # release locks held by the test session
    ns = {"__name__": "aishe_loader"}
    exec(compile(source, "load_universities.py", "exec"), ns)
    try:
        ns["load"]()
    finally:
        with ns["app"].app_context():
            ns["db"].engine.dispose()
    db.session.expire_all()


def test_aishe_loader_fills_the_app_tables(app, tmp_path, monkeypatch, capsys):
    _write_aishe_files(tmp_path)
    _run_aishe_loader(app, tmp_path, monkeypatch)

    assert University.query.count() == 2
    mu = University.query.filter_by(aishe_code="U-0450").one()
    # app columns got their server defaults
    assert mu.uuid and mu.status.value == "ACTIVE" and mu.onboarding_status.value == "MASTER"
    assert mu.source.value == "AISHE_IMPORT" and mu.email_domains == [] and mu.location.value == "Urban"
    assert mu.release_unfilled_reserved_to_open is False and mu.created_at is not None

    ruia = College.query.filter_by(aishe_code="C-201", institution_type="COLLEGE").one()
    assert ruia.university_id == mu.id and ruia.type == "Affiliated College" and ruia.management_type == "Private Aided"
    assert College.query.filter_by(aishe_code="C-201").count() == 2  # also in the PM Vidyalaxmi list
    assert College.query.filter_by(aishe_code="C-999").count() == 0  # 'U-' university code is skipped
    assert College.query.filter_by(institution_type="R&D").one().university_id is None
    assert {c.institution_type for c in College.query} == {"COLLEGE", "STANDALONE", "PM_VIDYALAXMI", "R&D"}
    assert State.query.count() == 1 and District.query.count() == 2

    # a re-run updates, never duplicates
    _run_aishe_loader(app, tmp_path, monkeypatch)
    assert University.query.count() == 2 and College.query.count() == 5


def test_aishe_rows_can_be_onboarded(api, app, tmp_path, monkeypatch):
    from .conftest import login, make_platform_admin
    _write_aishe_files(tmp_path)
    _run_aishe_loader(app, tmp_path, monkeypatch)
    make_platform_admin()
    tok = login(api, "pa@platform.in")
    _, body = api.get("/onboarding/lookup?aishe_code=C-201", expect=200)
    assert body["data"]["institution"]["institution_type"] == "COLLEGE"  # preferred over the PM row
    _, body = api.post("/onboarding/standalone-institutions", token=tok, expect=201,
                       json={"aishe_code": "S-5001", "admin_email": "head@gpp.ac.in", "admin_name": "Head"})
    assert body["data"]["institution"]["registration_type"] == "STANDALONE"
    assert College.query.filter_by(aishe_code="S-5001").one().onboarding_status.value == "PENDING"


def test_pincode_loader_links_to_aishe_states(app, tmp_path, capsys):
    from scripts.load_pincodes import main as load_pincodes
    from .conftest import seed_geo, seed_master_institution
    _, pune, _ = seed_geo()
    uni = seed_master_institution("U-0001", "SPPU", "UNIVERSITY", district=pune)
    uni.pincode, uni.pincode_verified = "411004", False
    db.session.commit()
    uni_id, pune_id, mh_id = uni.id, pune.id, pune.state_id
    pins = tmp_path / "pins.csv"
    pins.write_bytes(b"pincode,officename,district,statename\n"
                     b"411004,Deccan Gymkhana,PUNE,MAHARASHTRA\n"
                     b"400019,Matunga,Mumbai,Maharashtra\n"
                     b"110001,Connaught Place,New Delhi,Delhi\n"
                     b"012345,Bad,X,Y\n")
    load_pincodes(["--env", "testing", str(pins)])
    out = capsys.readouterr().out
    assert "3 new" in out and "1 invalid" in out and "1 without a matching state/district" in out
    db.session.remove()
    deccan = Pincode.query.filter_by(pincode="411004").one()
    assert deccan.district_id == pune_id and deccan.state_id == mh_id
    assert Pincode.query.filter_by(pincode="110001").one().state_id is None  # Delhi is not in the AISHE data here
    assert db.session.get(University, uni_id).pincode_verified
    load_pincodes(["--env", "testing", str(pins)])
    assert "0 new, 3 already present" in capsys.readouterr().out
