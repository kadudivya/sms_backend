"""Working with the two institution tables: universities and colleges.

The models stay plain (columns only); these helpers answer the questions the
services ask about "an institution" whichever table it is in:

* kind_of(inst)            -> InstitutionKind.UNIVERSITY / COLLEGE
* get_institution(kind, id) -> University or College row
* ref(inst)                -> {"university_id": .., "college_id": ..} for tables that point at either
* target(row)              -> the University or College a row points at (memberships, letters, ...)
* parent_university_id(inst) -> the college's university (None for a university or an unaffiliated college)

Colleges table rows (colleges.institution_type, plain text):
COLLEGE (affiliated, has university_id), STANDALONE, PM_VIDYALAXMI and R&D.
A college with no university_id is approved and managed by the Platform Admin.
"""
from sqlalchemy import case, func

from app.core.errors import NotFound, ValidationError
from app.enums import InstitutionKind
from app.extensions import db

# colleges.institution_type values written by load_universities.py
COLLEGE = "COLLEGE"
STANDALONE = "STANDALONE"
PM_VIDYALAXMI = "PM_VIDYALAXMI"
RD = "R&D"
COLLEGE_INSTITUTION_TYPES = (COLLEGE, STANDALONE, PM_VIDYALAXMI, RD)

# The same AISHE code can appear in more than one colleges file (PM Vidyalaxmi repeats
# codes from the other files). A code lookup prefers the row in this order.
LOOKUP_PREFERENCE = (COLLEGE, STANDALONE, RD, PM_VIDYALAXMI)

# What an institution is registered as (API values for onboarding).
#   UNIVERSITY -> universities table
#   COLLEGE    -> colleges table, institution_type COLLEGE, needs a parent university
#   STANDALONE -> colleges table, institution_type STANDALONE, no parent university
REGISTRATION_TYPES = ("UNIVERSITY", "COLLEGE", "STANDALONE")

# URL segment -> kind, for routes like /institutions/<kind>/<id>
URL_KINDS = {"university": InstitutionKind.UNIVERSITY, "college": InstitutionKind.COLLEGE}
KIND_URL = {v: k for k, v in URL_KINDS.items()}


def _models():
    from app.models import College, University
    return University, College


def model_for(kind):
    University, College = _models()
    return University if InstitutionKind(kind) == InstitutionKind.UNIVERSITY else College


def kind_of(inst):
    University, _ = _models()
    return InstitutionKind.UNIVERSITY if isinstance(inst, University) else InstitutionKind.COLLEGE


def is_university(inst):
    return inst is not None and kind_of(inst) == InstitutionKind.UNIVERSITY


def is_college(inst):
    return inst is not None and kind_of(inst) == InstitutionKind.COLLEGE


def is_affiliated(inst):
    """A college that has a parent university."""
    return is_college(inst) and inst.university_id is not None


def is_standalone(inst):
    """A college row with no parent university (STANDALONE, PM_VIDYALAXMI, R&D, ...)."""
    return is_college(inst) and inst.university_id is None


def parent_university_id(inst):
    return inst.university_id if is_college(inst) else None


def parent_university(inst):
    return inst.university if is_affiliated(inst) else None


def university_of(inst):
    """The university itself, or a college's parent university (None if it has none)."""
    if inst is None:
        return None
    return inst if is_university(inst) else inst.university


def registration_type(inst):
    """UNIVERSITY / COLLEGE / STANDALONE as used by onboarding."""
    if is_university(inst):
        return "UNIVERSITY"
    return "COLLEGE" if inst.university_id else "STANDALONE"


def kind_for_registration_type(reg_type):
    reg_type = str(reg_type).upper()
    if reg_type not in REGISTRATION_TYPES:
        raise ValidationError(f"Institution type must be one of {', '.join(REGISTRATION_TYPES)}")
    return InstitutionKind.UNIVERSITY if reg_type == "UNIVERSITY" else InstitutionKind.COLLEGE


def key(inst):
    """Hashable identity across both tables: ("UNIVERSITY", 4)."""
    return (kind_of(inst).value, inst.id)


def get_institution(kind, institution_id, required=True):
    if isinstance(kind, str) and kind.lower() in URL_KINDS:
        kind = URL_KINDS[kind.lower()]
    inst = db.session.get(model_for(kind), int(institution_id)) if institution_id is not None else None
    if inst is None and required:
        raise NotFound(f"{InstitutionKind(kind).value.title()} not found")
    return inst


# ------------------------------------------------- either-kind references
def ref(inst, prefix=""):
    """Column values for a table that points at a university or a college."""
    if inst is None:
        return {f"{prefix}university_id": None, f"{prefix}college_id": None}
    uni = is_university(inst)
    return {f"{prefix}university_id": inst.id if uni else None,
            f"{prefix}college_id": None if uni else inst.id}


def ref_kind_id(row, prefix=""):
    """(kind, id) of what a row points at, or (None, None)."""
    uid = getattr(row, f"{prefix}university_id")
    if uid:
        return InstitutionKind.UNIVERSITY, uid
    cid = getattr(row, f"{prefix}college_id")
    if cid:
        return InstitutionKind.COLLEGE, cid
    return None, None


def target(row, prefix=""):
    """The University or College a row points at."""
    kind, iid = ref_kind_id(row, prefix)
    return get_institution(kind, iid, required=False) if kind else None


def points_at(row, inst, prefix=""):
    if inst is None:
        return False
    kind, iid = ref_kind_id(row, prefix)
    return kind == kind_of(inst) and iid == inst.id


def ref_filter(model, inst, prefix=""):
    """SQL condition: model's university_id / college_id equals inst."""
    col = f"{prefix}university_id" if is_university(inst) else f"{prefix}college_id"
    return getattr(model, col) == inst.id


def ref_filter_many(model, university_ids=(), college_ids=(), prefix=""):
    """SQL condition: points at any of the given universities or colleges."""
    return db.or_(getattr(model, f"{prefix}university_id").in_(list(university_ids) or [-1]),
                  getattr(model, f"{prefix}college_id").in_(list(college_ids) or [-1]))


# --------------------------------------------------------------- lookups
def find_by_aishe(code, kind=None):
    """University or college with this AISHE code (case-insensitive).

    With no kind, U- codes are looked up in universities, everything else in colleges.
    Duplicate college codes resolve by LOOKUP_PREFERENCE.
    """
    University, College = _models()
    code = (code or "").strip().upper()
    if not code:
        return None
    if kind is None:
        kind = InstitutionKind.UNIVERSITY if code.startswith("U-") else InstitutionKind.COLLEGE
    if InstitutionKind(kind) == InstitutionKind.UNIVERSITY:
        return University.query.filter(func.upper(University.aishe_code) == code).first()
    order = case({t: i for i, t in enumerate(LOOKUP_PREFERENCE)}, value=College.institution_type,
                 else_=len(LOOKUP_PREFERENCE))
    return (College.query.filter(func.upper(College.aishe_code) == code)
            .order_by(order, College.id).first())


def display(inst):
    """Small summary used inside other responses."""
    if inst is None:
        return None
    return {"kind": kind_of(inst).value, "id": inst.id, "aishe_code": inst.aishe_code, "name": inst.name,
            "institution_type": getattr(inst, "institution_type", "UNIVERSITY")}


def scope_for(actor, permission=None, include_child_colleges=True):
    """(university_ids, college_ids) the actor works at, optionally only where they hold ``permission``.

    A university membership also brings in that university's colleges.
    """
    _, College = _models()
    universities, colleges = set(), set()
    for m in actor.memberships:
        if permission is not None and permission not in actor.membership_permissions(m):
            continue
        if m.university_id:
            universities.add(m.university_id)
        else:
            colleges.add(m.college_id)
    if include_child_colleges and universities:
        colleges.update(cid for (cid,) in db.session.query(College.id)
                        .filter(College.university_id.in_(list(universities))))
    return universities, colleges
