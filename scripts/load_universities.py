"""
Load AISHE data into the SMS database: universities in one table, everything else
(colleges, standalone, PM Vidyalaxmi, R&D institutes) in a colleges table.

Excel files (keep all five in the folder you run this script from):
  University.xlsx          -> universities
  College.xlsx             -> colleges   institution_type = COLLEGE
  Standalone.xlsx          -> colleges   institution_type = STANDALONE
  pm_vidya_lakshmi.xlsx    -> colleges   institution_type = PM_VIDYALAXMI
  RD.xlsx                  -> colleges   institution_type = R&D

Tables
  states        id, name
  districts     id, name, state_id
  universities  id, aishe_code, name, state_id, district_id, website,
                year_of_establishment, location
  colleges      id, institution_type, aishe_code, name, state_id, district_id, website,
                year_of_establishment, location, type, management_type, university_id

Column mapping into colleges
  file                    type column         management_type column    university_id
  College.xlsx            College Type        Manegement               from University Aishe Code
  Standalone.xlsx         Standalone Type     Manegement               -
  pm_vidya_lakshmi.xlsx   -                   Management Type          -
  RD.xlsx                 -                   Administrative Ministry  -
  (University Name and University Type in College.xlsx are ignored.)

Rules
  * The only enum is location (Urban / Rural). institution_type, type and
    management_type are plain text columns.
  * type and management_type are cleaned on entry: lower-cased, stripped, then the
    first letter of each word is made capital ("PRIVATE UN-AIDED " -> "Private Un-aided").
  * Blank, "-" and "NA" values are stored as NULL.
  * Colleges whose University Aishe Code is blank or "U-" are not inserted (reported).
  * The same institution can appear in two files (most PM Vidyalaxmi institutes are
    also in University / College / Standalone), so a college row is unique on
    (institution_type, aishe_code), not on aishe_code alone.
  * Re-running is safe: rows are matched on their key and updated, never duplicated.
  * Data fixes (see DATA FIXES below) are applied before anything is saved:
      - district New Delhi always belongs to state Delhi
      - the cut-off state "The Dadra and Nagar" is read as
        "The Dadra and Nagar Haveli and Daman and Diu" (so there is one Daman district)
      - Government Arts College, Tiruppur is in district Tiruppur (not Kachchh)
  * States and districts that no row uses any more are deleted at the end
    (for example ones created by an earlier run before these fixes).

Setup:
  pip install flask flask-sqlalchemy openpyxl psycopg2-binary

Create the DB manually first:
  CREATE DATABASE "SMS";

Run:
  python load_universities.py
"""

import enum
import os
import sys
from datetime import datetime

from flask import Flask
from flask_sqlalchemy import SQLAlchemy
from openpyxl import load_workbook

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------
DATABASE_URL = "postgresql+psycopg2://postgres:root@localhost:5432/sms"  # change to your creds
UNIVERSITY_EXCEL = "University.xlsx"
COLLEGE_EXCEL = "College.xlsx"
STANDALONE_EXCEL = "Standalone.xlsx"
PM_VIDYALAXMI_EXCEL = "pm_vidya_lakshmi.xlsx"
RD_EXCEL = "RD.xlsx"
BATCH_SIZE = 1000
HEADER_SEARCH_ROWS = 10  # the files have a title and a date row above the header

# Drop and recreate states, districts, universities and colleges before loading.
# Set to True once if you ran the previous version of this script (its tables had
# a different structure and enum columns), then set it back to False.
RESET_TABLES = False

# ---------------------------------------------------------------------------
# DATA FIXES (applied to every file before saving; keys are matched case-insensitively)
# ---------------------------------------------------------------------------
# A district that always belongs to one state, whatever the file says.
STATE_BY_DISTRICT = {
    "new delhi": "Delhi",
}
# State names that are wrong / cut off in a file -> the correct state name.
STATE_NAME_FIXES = {
    "the dadra and nagar": "The Dadra and Nagar Haveli and Daman and Diu",
}
# Institution name -> correct district (for rows filed under the wrong district).
DISTRICT_BY_INSTITUTION = {
    "government arts college, tiruppur": "Tiruppur",
}

app = Flask(__name__)
app.config["SQLALCHEMY_DATABASE_URI"] = DATABASE_URL
app.config["SQLALCHEMY_TRACK_MODIFICATIONS"] = False
db = SQLAlchemy(app)


# ---------------------------------------------------------------------------
# Enum (the only one)
# ---------------------------------------------------------------------------
class LocationType(enum.Enum):
    URBAN = "Urban"
    RURAL = "Rural"


# institution_type values written to colleges.institution_type (plain text, not an enum)
COLLEGE = "COLLEGE"
STANDALONE = "STANDALONE"
PM_VIDYALAXMI = "PM_VIDYALAXMI"
RD = "R&D"


# ---------------------------------------------------------------------------
# Models
# ---------------------------------------------------------------------------
class State(db.Model):
    __tablename__ = "states"

    id = db.Column(db.Integer, primary_key=True, autoincrement=True)
    name = db.Column(db.String(100), unique=True, nullable=False)

    def to_dict(self):
        return {c.name: getattr(self, c.name) for c in self.__table__.columns}


class District(db.Model):
    __tablename__ = "districts"
    __table_args__ = (db.UniqueConstraint("state_id", "name", name="uq_district_state_name"),)

    id = db.Column(db.Integer, primary_key=True, autoincrement=True)
    name = db.Column(db.String(100), nullable=False)
    state_id = db.Column(db.Integer, db.ForeignKey("states.id", ondelete="CASCADE"), nullable=False, index=True)

    def to_dict(self):
        return {c.name: getattr(self, c.name) for c in self.__table__.columns}


class University(db.Model):
    __tablename__ = "universities"

    id = db.Column(db.Integer, primary_key=True, autoincrement=True)
    aishe_code = db.Column(db.String(20), unique=True, nullable=False, index=True)
    name = db.Column(db.String(500), nullable=False, index=True)
    state_id = db.Column(db.Integer, db.ForeignKey("states.id", ondelete="RESTRICT"), nullable=False, index=True)
    district_id = db.Column(db.Integer, db.ForeignKey("districts.id", ondelete="RESTRICT"), nullable=True, index=True)
    website = db.Column(db.String(500), nullable=True)
    year_of_establishment = db.Column(db.Integer, nullable=True)
    location = db.Column(db.Enum(LocationType, name="location_type",
                                 values_callable=lambda e: [m.value for m in e]), nullable=True)

    def to_dict(self):
        data = {}
        for column in self.__table__.columns:
            value = getattr(self, column.name)
            data[column.name] = value.value if isinstance(value, enum.Enum) else value
        return data


class College(db.Model):
    """Colleges, standalone institutions, PM Vidyalaxmi institutes and R&D institutes."""

    __tablename__ = "colleges"
    __table_args__ = (
        db.UniqueConstraint("institution_type", "aishe_code", name="uq_college_type_aishe_code"),
    )

    id = db.Column(db.Integer, primary_key=True, autoincrement=True)
    institution_type = db.Column(db.String(20), nullable=False, index=True)  # COLLEGE / STANDALONE / PM_VIDYALAXMI / R&D
    aishe_code = db.Column(db.String(20), nullable=False, index=True)
    name = db.Column(db.String(500), nullable=False, index=True)
    state_id = db.Column(db.Integer, db.ForeignKey("states.id", ondelete="RESTRICT"), nullable=False, index=True)
    district_id = db.Column(db.Integer, db.ForeignKey("districts.id", ondelete="RESTRICT"), nullable=True, index=True)
    website = db.Column(db.String(500), nullable=True)
    year_of_establishment = db.Column(db.Integer, nullable=True)
    location = db.Column(db.Enum(LocationType, name="location_type",
                                 values_callable=lambda e: [m.value for m in e]), nullable=True)
    type = db.Column(db.String(255), nullable=True, index=True)  # College Type / Standalone Type
    management_type = db.Column(db.String(255), nullable=True)  # Manegement / Management Type / Administrative Ministry
    university_id = db.Column(db.Integer, db.ForeignKey("universities.id", ondelete="RESTRICT"),
                              nullable=True, index=True)  # COLLEGE rows only

    def to_dict(self):
        data = {}
        for column in self.__table__.columns:
            value = getattr(self, column.name)
            data[column.name] = value.value if isinstance(value, enum.Enum) else value
        return data


# ---------------------------------------------------------------------------
# Excel column mapping, per file:  Excel header -> field
# ---------------------------------------------------------------------------
UNIVERSITY_COLUMNS = {
    "Aishe Code": "aishe_code",
    "Name": "name",
    "State": "state",
    "District": "district",
    "Website": "website",
    "Year Of Establishment": "year_of_establishment",
    "Location": "location",
}
COLLEGE_COLUMNS = {
    "Aishe Code": "aishe_code",
    "Name": "name",
    "State": "state",
    "District": "district",
    "Website": "website",
    "Year Of Establishment": "year_of_establishment",
    "Location": "location",
    "College Type": "type",
    "Manegement": "management_type",  # spelled this way in the AISHE file
    "University Aishe Code": "university_code",
    # "University Name" and "University Type" are ignored
}
STANDALONE_COLUMNS = {
    "Aishe Code": "aishe_code",
    "Name": "name",
    "State": "state",
    "District": "district",
    "Year Of Establishment": "year_of_establishment",
    "Location": "location",
    "Standalone Type": "type",
    "Manegement": "management_type",
}
PM_VIDYALAXMI_COLUMNS = {
    "AISHE Code": "aishe_code",
    "Institute Name": "name",
    "State": "state",
    "District": "district",
    "Website": "website",
    "Year Of Establishment": "year_of_establishment",
    "Location": "location",
    "Management Type": "management_type",
}
RD_COLUMNS = {
    "AISHE Code": "aishe_code",
    "Institute Name": "name",
    "State Name": "state",
    "District Name": "district",
    "Administrative Ministry": "management_type",
}


# ---------------------------------------------------------------------------
# Cleaning
# ---------------------------------------------------------------------------
def clean_text(value):
    """Collapse spaces and line breaks; treat '', '-', 'NA' etc. as NULL."""
    if value is None:
        return None
    value = " ".join(str(value).split())
    if value.lower() in ("", "-", "--", "na", "n/a", "null", "none", "u-"):
        return None
    return value


def clean_title(value):
    """Lower-case, strip, then make the first letter of each word capital.
    '  PRIVATE AIDED (GOVERNMENT AIDED)' -> 'Private Aided (Government Aided)'"""
    value = clean_text(value)
    if value is None:
        return None
    words = []
    for word in value.lower().strip().split():
        for i, ch in enumerate(word):  # first letter, even after a leading '(' or quote
            if ch.isalpha():
                word = word[:i] + ch.upper() + word[i + 1:]
                break
        words.append(word)
    return " ".join(words)


def clean_year(value):
    value = clean_text(value)
    if value is None:
        return None
    try:
        year = int(float(value))
    except ValueError:
        return None
    return year if 1000 <= year <= datetime.now().year else None


def clean_location(value):
    value = clean_text(value)
    if value is None:
        return None
    for member in LocationType:
        if member.value.lower() == value.lower():
            return member
    print(f"  ! Unknown location '{value}' -> stored as NULL")
    return None


def apply_fixes(name, state, district):
    """Apply DATA FIXES. Returns (state, district)."""
    district = DISTRICT_BY_INSTITUTION.get(name.lower(), district)
    state = STATE_NAME_FIXES.get(state.lower(), state)
    if district:
        state = STATE_BY_DISTRICT.get(district.lower(), state)
    return state, district


def excel_path(filename):
    path = os.path.join(os.getcwd(), filename)
    if not os.path.exists(path):
        sys.exit(f"File not found: {path}")
    return path


def read_excel(filename, columns):
    """Return (records, skipped, duplicates). The header row is found automatically
    (the AISHE downloads have a title and a date row above it)."""
    wb = load_workbook(excel_path(filename), read_only=True, data_only=True)
    ws = wb.worksheets[0]
    wanted = {name.strip().lower(): field for name, field in columns.items()}
    rows = ws.iter_rows(values_only=True)

    col_index = None
    for _ in range(HEADER_SEARCH_ROWS):
        header = next(rows, None)
        if header is None:
            break
        found = {}
        for i, h in enumerate(header):
            field = wanted.get(" ".join(str(h or "").split()).lower())
            if field:
                found[field] = i
        if "aishe_code" in found and "name" in found:
            col_index = found
            break
    if col_index is None:
        sys.exit(f"{filename}: no header row with the AISHE code and name columns in the first "
                 f"{HEADER_SEARCH_ROWS} rows")
    missing = set(columns.values()) - col_index.keys()
    if missing:
        sys.exit(f"{filename}: missing columns {sorted(missing)}. Expected: {list(columns)}")

    records, skipped, duplicates = {}, 0, 0
    for row in rows:
        def get(field):
            i = col_index.get(field)
            return row[i] if i is not None and i < len(row) else None

        code = clean_text(get("aishe_code"))
        name = clean_text(get("name"))
        state = clean_text(get("state"))
        if not code or not name or not state:
            if any(v not in (None, "") for v in row):
                skipped += 1
            continue
        code = code.upper()
        district = clean_text(get("district"))
        state, district = apply_fixes(name, state, district)
        if code in records:
            duplicates += 1  # last occurrence wins
        uni_code = clean_text(get("university_code"))
        records[code] = {
            "aishe_code": code,
            "name": name,
            "state": state,
            "district": district,
            "website": clean_text(get("website")),
            "year_of_establishment": clean_year(get("year_of_establishment")),
            "location": clean_location(get("location")),
            "type": clean_title(get("type")),
            "management_type": clean_title(get("management_type")),
            "university_code": uni_code.upper() if uni_code else None,
        }
    wb.close()
    return list(records.values()), skipped, duplicates


# ---------------------------------------------------------------------------
# Load helpers
# ---------------------------------------------------------------------------
def get_or_create_states(names):
    """Return ({state_name_lower: State}, created_count)."""
    cache = {s.name.lower(): s for s in State.query.all()}
    created = 0
    for name in sorted(names):
        if name.lower() not in cache:
            state = State(name=name)
            db.session.add(state)
            cache[name.lower()] = state
            created += 1
    db.session.flush()
    return cache, created


def get_or_create_districts(pairs, state_cache):
    """pairs = {(state_name, district_name)}. Return ({(state_id, district_lower): District}, created)."""
    cache = {(d.state_id, d.name.lower()): d for d in District.query.all()}
    created = 0
    for state_name, district_name in sorted(pairs):
        state = state_cache[state_name.lower()]
        key = (state.id, district_name.lower())
        if key not in cache:
            district = District(name=district_name, state_id=state.id)
            db.session.add(district)
            cache[key] = district
            created += 1
    db.session.flush()
    return cache, created


def resolve_location_ids(rec, state_cache, district_cache):
    state = state_cache[rec["state"].lower()]
    district = district_cache.get((state.id, rec["district"].lower())) if rec["district"] else None
    return state.id, (district.id if district else None)


def delete_unused_states_and_districts(used_district_ids, used_state_ids):
    """Remove districts and states that no loaded row uses (left over from earlier runs)."""
    in_use_districts = (set(used_district_ids)
                        | {d for (d,) in db.session.query(University.district_id).distinct() if d}
                        | {d for (d,) in db.session.query(College.district_id).distinct() if d})
    in_use_states = (set(used_state_ids)
                     | {s for (s,) in db.session.query(University.state_id).distinct()}
                     | {s for (s,) in db.session.query(College.state_id).distinct()}
                     | {s for (s,) in db.session.query(District.state_id)
                        .filter(District.id.in_(in_use_districts)).distinct()})
    removed_d = District.query.filter(District.id.notin_(in_use_districts)).delete(synchronize_session=False)
    removed_s = State.query.filter(State.id.notin_(in_use_states)).delete(synchronize_session=False)
    return removed_d, removed_s


def upsert(model, rows, key_fields):
    """Insert new rows / update changed ones, matched on key_fields. Returns (inserted, updated)."""
    existing = {tuple(getattr(obj, f) for f in key_fields): obj for obj in model.query.all()}
    to_insert, updated = [], 0
    for values in rows:
        obj = existing.get(tuple(values[f] for f in key_fields))
        if obj is None:
            to_insert.append(values)
            continue
        changed = False
        for field, val in values.items():
            if getattr(obj, field) != val:
                setattr(obj, field, val)
                changed = True
        updated += changed
    for i in range(0, len(to_insert), BATCH_SIZE):
        db.session.execute(db.insert(model), to_insert[i:i + BATCH_SIZE])
    db.session.flush()
    return len(to_insert), updated


def prepare_tables():
    """Create missing tables; reset or stop if an older version of the tables exists."""
    inspector = db.inspect(db.engine)
    if RESET_TABLES:
        for table in ("colleges", "universities", "districts", "states"):
            db.session.execute(db.text(f'DROP TABLE IF EXISTS "{table}" CASCADE'))
        for enum_type in ("location_type", "institution_type", "college_type", "management_type"):
            db.session.execute(db.text(f"DROP TYPE IF EXISTS {enum_type}"))
        db.session.commit()
        print("Tables reset (RESET_TABLES = True). Set it back to False after this run.")
    elif inspector.has_table("colleges"):
        cols = {c["name"] for c in inspector.get_columns("colleges")}
        if "management_type" not in cols or "management" in cols:
            sys.exit("The colleges table is from the previous version of this script. "
                     "Set RESET_TABLES = True for one run to recreate the tables.")
    db.create_all()


# ---------------------------------------------------------------------------
# Main load
# ---------------------------------------------------------------------------
def load():
    print("Reading Excel files ...")
    uni_records, uni_skipped, uni_dups = read_excel(UNIVERSITY_EXCEL, UNIVERSITY_COLUMNS)
    sources = [  # institution_type, file, records, skipped, duplicates
        (COLLEGE, COLLEGE_EXCEL, *read_excel(COLLEGE_EXCEL, COLLEGE_COLUMNS)),
        (STANDALONE, STANDALONE_EXCEL, *read_excel(STANDALONE_EXCEL, STANDALONE_COLUMNS)),
        (PM_VIDYALAXMI, PM_VIDYALAXMI_EXCEL, *read_excel(PM_VIDYALAXMI_EXCEL, PM_VIDYALAXMI_COLUMNS)),
        (RD, RD_EXCEL, *read_excel(RD_EXCEL, RD_COLUMNS)),
    ]

    print(f"{UNIVERSITY_EXCEL:<22}: {len(uni_records)} rows ({uni_skipped} skipped, {uni_dups} duplicate codes)")
    for inst_type, filename, records, skipped, dups in sources:
        print(f"{filename:<22}: {len(records)} rows ({skipped} skipped, {dups} duplicate codes) -> {inst_type}")

    # States & districts come from every row of every file
    all_records = uni_records + [r for _, _, records, _, _ in sources for r in records]

    with app.app_context():
        prepare_tables()
        try:
            # 1) States + districts
            state_cache, states_new = get_or_create_states({r["state"] for r in all_records})
            district_cache, districts_new = get_or_create_districts(
                {(r["state"], r["district"]) for r in all_records if r["district"]}, state_cache)

            # 2) Universities
            uni_rows = []
            for rec in uni_records:
                state_id, district_id = resolve_location_ids(rec, state_cache, district_cache)
                uni_rows.append({
                    "aishe_code": rec["aishe_code"],
                    "name": rec["name"],
                    "state_id": state_id,
                    "district_id": district_id,
                    "website": rec["website"],
                    "year_of_establishment": rec["year_of_establishment"],
                    "location": rec["location"],
                })
            uni_ins, uni_upd = upsert(University, uni_rows, ("aishe_code",))

            # 3) Colleges table: all four files
            uni_ids = {code: uid for code, uid in db.session.query(University.aishe_code, University.id)}
            college_rows, no_uni_code, unknown_uni_code = [], 0, set()
            for inst_type, _, records, _, _ in sources:
                for rec in records:
                    university_id = None
                    if inst_type == COLLEGE:
                        if not rec["university_code"]:  # blank / '-' / 'U-'
                            no_uni_code += 1
                            continue
                        university_id = uni_ids.get(rec["university_code"])
                        if university_id is None:  # code not in universities table
                            unknown_uni_code.add(rec["university_code"])
                            continue
                    state_id, district_id = resolve_location_ids(rec, state_cache, district_cache)
                    college_rows.append({
                        "institution_type": inst_type,
                        "aishe_code": rec["aishe_code"],
                        "name": rec["name"],
                        "state_id": state_id,
                        "district_id": district_id,
                        "website": rec["website"],
                        "year_of_establishment": rec["year_of_establishment"],
                        "location": rec["location"],
                        "type": rec["type"],
                        "management_type": rec["management_type"],
                        "university_id": university_id,
                    })
            col_ins, col_upd = upsert(College, college_rows, ("institution_type", "aishe_code"))

            # 4) Clean up states / districts no longer used by any row
            all_rows = uni_rows + college_rows
            districts_removed, states_removed = delete_unused_states_and_districts(
                {r["district_id"] for r in all_rows if r["district_id"]}, {r["state_id"] for r in all_rows})

            db.session.commit()
        except Exception:
            db.session.rollback()
            raise

        print("-" * 70)
        print(f"States       -> new: {states_new:<6} removed (unused): {states_removed:<4} total: {State.query.count()}")
        print(f"Districts    -> new: {districts_new:<6} removed (unused): {districts_removed:<4} total: {District.query.count()}")
        print(f"Universities -> inserted: {uni_ins} | updated: {uni_upd} | total: {University.query.count()}")
        print(f"Colleges     -> inserted: {col_ins} | updated: {col_upd} | total: {College.query.count()}")
        for inst_type in (COLLEGE, STANDALONE, PM_VIDYALAXMI, RD):
            print(f"                {inst_type:<14}: {College.query.filter_by(institution_type=inst_type).count()}")
        if no_uni_code:
            print(f"  ! {no_uni_code} colleges skipped: University Aishe Code blank / '-' / 'U-'")
        if unknown_uni_code:
            print(f"  ! Colleges skipped: University Aishe Code not in universities table: "
                  f"{sorted(unknown_uni_code)}")


if __name__ == "__main__":
    started = datetime.now()
    print(f"Started : {started:%d-%m-%Y %H:%M:%S}")
    failed = None
    try:
        load()
    except Exception as e:
        failed = e
    finished = datetime.now()
    seconds = int((finished - started).total_seconds())
    print("-" * 70)
    print(f"Started : {started:%d-%m-%Y %H:%M:%S}")
    print(f"Finished: {finished:%d-%m-%Y %H:%M:%S}")
    print(f"Duration: {seconds // 3600:02d}:{seconds % 3600 // 60:02d}:{seconds % 60:02d} (hh:mm:ss)")
    if failed is not None:
        sys.exit(f"Failed: {failed}")
