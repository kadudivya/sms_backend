"""Institution profile, status (suspend / reactivate), affiliation and branding."""
from app.core.activity import activity
from app.core.errors import BusinessRuleViolation, PermissionDenied, ValidationError
from app.core.utils import valid_pincode
from app.core.institutions import is_affiliated, is_standalone, is_university, kind_of
from app.enums import (DocumentOwnerType, DocumentType, MigrationStatus, OnboardingStatus, Permission)
from app.extensions import db
from app.models import District, MigrationRequest, Pincode
from app.core.records import active

PROFILE_FIELDS = ("address_line", "city", "pincode", "website", "phone", "district_id", "state_id")

# Steps a suspended institution would normally act on: they move to its parent (9.2 M03-F06).
OPEN_STATUSES = {MigrationStatus.SUBMITTED, MigrationStatus.NOC_GRANTED, MigrationStatus.UNDER_REVIEW,
                 MigrationStatus.RETURNED_FOR_CORRECTION, MigrationStatus.INTERVIEW_SCHEDULED,
                 MigrationStatus.RECOMMENDED, MigrationStatus.ENTRY_APPROVED, MigrationStatus.WAITLISTED,
                 MigrationStatus.PROVISIONAL_OFFER, MigrationStatus.OFFER_ACCEPTED, MigrationStatus.RELEASED,
                 MigrationStatus.ADMITTED_ORIGINALS_PENDING}


def pincode_lookup(pin):
    if not valid_pincode(pin):
        raise ValidationError("PIN code must be 6 digits and not start with 0", error_code="INVALID_PINCODE")
    rows = active(Pincode).filter_by(pincode=pin).all()
    if not rows:
        return {"pincode": pin, "found": False, "localities": []}
    first = rows[0]
    return {
        "pincode": pin, "found": True,
        "state_id": first.state_id, "state_name": first.state.name if first.state else None,
        "district_id": first.district_id, "district_name": first.district.name if first.district else None,
        # M04-F08: locality list, falling back to the district name
        "localities": sorted({r.locality for r in rows if r.locality}) or
                      ([first.district.name] if first.district else []),
    }


def update_profile(actor, inst, data):
    if not (actor.is_platform_admin or actor.admin_of(inst)):
        raise PermissionDenied("Only the institution's admin can edit its profile")
    if "pincode" in data:
        if not valid_pincode(data["pincode"]):
            raise ValidationError("PIN code must be 6 digits and not start with 0", error_code="INVALID_PINCODE")
        inst.pincode_verified = active(Pincode).filter_by(pincode=data["pincode"]).first() is not None
    if "district_id" in data:
        district = db.session.get(District, data["district_id"])
        state_id = data.get("state_id", inst.state_id)
        if district is None or district.state_id != state_id:
            raise ValidationError("District does not belong to the state", error_code="INVALID_DISTRICT")
    for key in PROFILE_FIELDS:
        if key in data:
            activity.change(kind_of(inst).value.lower(), inst.id, key, getattr(inst, key), data[key])
            setattr(inst, key, data[key])
    activity.set_institution(inst)
    db.session.commit()
    return inst


def set_suspended(actor, inst, suspend, reason=None):
    if is_university(inst):
        if not actor.is_platform_admin:
            raise PermissionDenied("Only the Platform Admin can suspend or reactivate a university")
    elif is_standalone(inst):
        if not actor.is_platform_admin:
            raise PermissionDenied("Only the Platform Admin can suspend or reactivate this institution")
    else:
        actor.require(Permission.MANAGE_COLLEGE_STATUS, inst)
    if suspend:
        if not reason:
            raise ValidationError("A reason is required to suspend")
        if inst.onboarding_status == OnboardingStatus.SUSPENDED:
            raise BusinessRuleViolation("Already suspended")
        inst.onboarding_status = OnboardingStatus.SUSPENDED
        inst.suspended_reason = reason
        if is_university(inst):
            touches = db.or_(MigrationRequest.source_university_id == inst.id,
                             MigrationRequest.destination_university_id == inst.id)
        else:
            touches = db.or_(MigrationRequest.source_college_id == inst.id,
                             MigrationRequest.destination_college_id == inst.id)
        in_progress = (MigrationRequest.query.filter(
            touches,
            MigrationRequest.request_status.in_(list(OPEN_STATUSES))).count())
        activity.step("Institution suspended; in-progress requests continue via the parent",
                      entity=kind_of(inst).value.lower(), entity_id=inst.id, in_progress_requests=in_progress)
    else:
        if inst.onboarding_status != OnboardingStatus.SUSPENDED:
            raise BusinessRuleViolation("Institution is not suspended")
        if is_affiliated(inst) and inst.university.onboarding_status == OnboardingStatus.SUSPENDED:
            raise BusinessRuleViolation("Reactivate the university first")
        inst.onboarding_status = OnboardingStatus.ACTIVE
        inst.suspended_reason = None
        activity.step("Institution reactivated", entity=kind_of(inst).value.lower(), entity_id=inst.id)
    activity.set_institution(inst)
    db.session.commit()
    return inst


def update_affiliation(actor, college, new_parent):
    """Move an affiliated college (colleges.institution_type COLLEGE) to another university."""
    if not is_affiliated(college):
        raise ValidationError("Only affiliated colleges have a parent university")
    if not is_university(new_parent):
        raise ValidationError("The new parent must be a university")
    if not actor.is_platform_admin:
        actor.require(Permission.MANAGE_COLLEGE_STATUS, college)  # current university
        actor.require(Permission.MANAGE_COLLEGE_STATUS, new_parent)
    activity.change("college", college.id, "university_id", college.university_id, new_parent.id)
    college.university_id = new_parent.id
    db.session.commit()
    return college


def update_branding(actor, inst, header=None, footer=None, seal_file=None):
    actor.require(Permission.MANAGE_LETTER_BRANDING, inst)
    from app.services.documents import store_upload
    if header is not None:
        inst.letter_header = header
    if footer is not None:
        inst.letter_footer = footer
    if seal_file is not None:
        owner = DocumentOwnerType.UNIVERSITY if is_university(inst) else DocumentOwnerType.COLLEGE
        doc = store_upload(seal_file, owner, inst.id, DocumentType.INSTITUTION_SEAL, institution=inst)
        if doc.mime_type not in ("image/png", "image/jpeg"):
            raise ValidationError("The seal must be a PNG or JPEG image")
        inst.seal_document_id = doc.id
    activity.step("Letter branding updated", entity=kind_of(inst).value.lower(), entity_id=inst.id)
    db.session.commit()
    return inst


def update_reservation_policy(actor, university, release_to_open):
    if not is_university(university):
        raise ValidationError("Reservation release policy is set by universities")
    actor.require(Permission.MANAGE_ACADEMIC_RULES, university)
    activity.change("university", university.id, "release_unfilled_reserved_to_open",
                    university.release_unfilled_reserved_to_open, release_to_open)
    university.release_unfilled_reserved_to_open = release_to_open
    db.session.commit()
    return university
