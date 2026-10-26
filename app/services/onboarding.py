"""Institution onboarding (4.1-4.3): direct creation, signup links, registration
requests, approvals, second claims, staff invitations and access requests."""
from flask import current_app
from sqlalchemy import func

from app.core.activity import activity
from app.core.errors import (BusinessRuleViolation, Conflict, NotFound, PermissionDenied,
                             ValidationError)
from app.core.permissions import COLLEGE_USER_GRANTABLE, UNIVERSITY_USER_GRANTABLE, expand
from app.core.security import generate_token, hash_password, hash_token, verify_password
from app.core.utils import email_domain, ensure_aware, utcnow, valid_aishe, valid_pincode
from app.enums import (AccountState, ApprovalStatus, DocumentOwnerType, DocumentType,
                       InstitutionKind, InstitutionSource, InvitationPurpose, InvitationState,
                       MembershipRole, NotificationEvent, OnboardingStatus, OtpPurpose, Permission,
                       RegistrationPath, SessionEndReason, UserType)
from app.extensions import db
from app.core.institutions import (COLLEGE, STANDALONE, find_by_aishe, get_institution, is_affiliated,
                                   is_college, is_university, kind_for_registration_type,
                                   kind_of, points_at, ref, ref_filter, registration_type, target)
from app.models import (College, CollegeOnboardingRequest, District, InstitutionMembership,
                        Invitation, RegistrationRequest, State, University, User)
from app.services import otp as otp_service
from app.services.directory import (has_active_admin, institution_admins, platform_admins,
                                    primary_admin)
from app.services.documents import store_upload
from app.services.notifications import notify, notify_email_address
from app.core.auth import membership_institution
from app.core.records import activate, active, deactivate, is_active, is_operational

GENERIC_EMAIL_DOMAINS = {"gmail.com", "yahoo.com", "yahoo.co.in", "outlook.com", "hotmail.com",
                         "rediffmail.com", "icloud.com", "protonmail.com", "live.com"}

# Registration type (UNIVERSITY / COLLEGE / STANDALONE, see core.institutions) <-> admin invitation purpose
PURPOSE_FOR_TYPE = {
    "UNIVERSITY": InvitationPurpose.UNIVERSITY_ADMIN,
    "COLLEGE": InvitationPurpose.COLLEGE_ADMIN,
    "STANDALONE": InvitationPurpose.STANDALONE_ADMIN,
}
TYPE_FOR_PURPOSE = {v: k for k, v in PURPOSE_FOR_TYPE.items()}


def _reg_type(value):
    value = str(value).upper()
    kind_for_registration_type(value)  # validates
    return value


# ----------------------------------------------------------------- lookup
def lookup_institution(aishe_code, reg_type=None):
    """University or college for an AISHE code; checks it matches the registration type.

    COLLEGE needs a college with a parent university; STANDALONE a college without one.
    """
    code = (aishe_code or "").strip().upper()
    kind = kind_for_registration_type(reg_type) if reg_type else None
    inst = find_by_aishe(code, kind)
    if inst is None:
        return None
    if reg_type:
        actual = registration_type(inst)
        if actual != _reg_type(reg_type):
            raise ValidationError(f"{code} is a {actual.lower()} institution, not a {str(reg_type).lower()} one",
                                  error_code="AISHE_TYPE_MISMATCH")
    return inst


def institution_card(inst):
    parent = inst.university if is_college(inst) else None
    return {
        **inst.to_dict(exclude=("letter_header", "letter_footer")),
        "kind": kind_of(inst).value,
        # UNIVERSITY, or the colleges row's type: COLLEGE / STANDALONE / PM_VIDYALAXMI / R&D
        "institution_type": "UNIVERSITY" if is_university(inst) else inst.institution_type,
        "registration_type": registration_type(inst),
        "state_name": inst.state.name if inst.state else None,
        "district_name": inst.district.name if inst.district else None,
        "parent_name": parent.name if parent else None,
        "parent_aishe_code": parent.aishe_code if parent else None,
        "has_active_admin": has_active_admin(inst),
    }


def _validate_manual_details(details, reg_type):
    required = ["name", "state_id", "district_id", "address_line", "pincode"]
    missing = [f for f in required if not details.get(f)]
    if reg_type == "COLLEGE" and not details.get("parent_university_id"):
        missing.append("parent_university_id")
    if missing:
        raise ValidationError("Institution details are required because the AISHE code was not found",
                              error_code="INSTITUTION_DETAILS_REQUIRED", details={"missing": missing})
    if not valid_pincode(str(details["pincode"])):
        raise ValidationError("PIN code must be 6 digits and not start with 0", error_code="INVALID_PINCODE")
    district = db.session.get(District, int(details["district_id"]))
    if district is None or district.state_id != int(details["state_id"]):
        raise ValidationError("District does not belong to the state", error_code="INVALID_DISTRICT")


def create_manual_institution(aishe_code, reg_type, details, onboarding=OnboardingStatus.PENDING):
    """INT-31: validate a new AISHE code and the details before saving.

    UNIVERSITY adds a row to universities; COLLEGE / STANDALONE add a row to colleges
    with institution_type COLLEGE (with its parent university) or STANDALONE.
    """
    reg_type = _reg_type(reg_type)
    kind = kind_for_registration_type(reg_type)
    model = University if kind == InstitutionKind.UNIVERSITY else College
    code = aishe_code.strip().upper()
    if not valid_aishe(code, reg_type):
        raise ValidationError(f"{code} is not a valid {reg_type.lower()} AISHE code",
                              error_code="INVALID_AISHE_CODE")
    if find_by_aishe(code, kind) is not None:
        raise Conflict("An institution with this AISHE code already exists", error_code="DUPLICATE_AISHE")
    _validate_manual_details(details, reg_type)
    from app.models import Pincode
    pin_known = Pincode.query.filter_by(pincode=str(details["pincode"])).first() is not None
    extra = {}
    if reg_type == "COLLEGE":
        parent = db.session.get(University, int(details["parent_university_id"]))
        if parent is None:
            raise ValidationError("Parent university not found", error_code="INVALID_PARENT")
        extra = {"institution_type": COLLEGE, "university_id": parent.id}
    elif reg_type == "STANDALONE":
        extra = {"institution_type": STANDALONE, "university_id": None}
    dup_name = (model.query.filter(func.lower(model.name) == details["name"].strip().lower(),
                                   model.district_id == int(details["district_id"])).first())
    if dup_name:
        raise Conflict(f"An institution with this name already exists in the district ({dup_name.aishe_code})",
                       error_code="POSSIBLE_DUPLICATE_INSTITUTION")
    inst = model(aishe_code=code, name=details["name"].strip(), state_id=int(details["state_id"]),
                 district_id=int(details["district_id"]), address_line=details.get("address_line"),
                 city=details.get("city"), pincode=str(details["pincode"]),
                 pincode_verified=pin_known, website=details.get("website"),
                 phone=details.get("phone"), source=InstitutionSource.MANUAL,
                 onboarding_status=onboarding, **extra)
    db.session.add(inst)
    db.session.flush()
    activity.step("Created institution from manual details", entity=kind.value.lower(), entity_id=inst.id,
                  aishe_code=code, pincode_verified=pin_known)
    return inst


# ------------------------------------------------------------ invitations
def _invite_link(token):
    return f"{current_app.config['FRONTEND_BASE_URL']}/invite?token={token}"


def create_invitation(purpose, email, invited_by, *, institution=None, parent=None, aishe_code=None,
                      bundles=(), permissions=(), pre_approved=False, full_name=None, note=None):
    """``institution`` is a University / College row; ``parent`` the university inviting a new college."""
    email = email.strip().lower()
    existing = active(Invitation).filter(Invitation.email == email,
                                          Invitation.invitation_state == InvitationState.SENT,
                                          Invitation.purpose == InvitationPurpose(purpose))
    if institution is not None:
        existing = existing.filter(ref_filter(Invitation, institution))
    for old in existing.all():
        old.invitation_state = InvitationState.REVOKED
        deactivate(old)
    token = generate_token()
    days = current_app.config["INVITATION_EXPIRES"].days
    purpose = InvitationPurpose(purpose)
    if institution is not None:
        kind = kind_of(institution)
    elif purpose in (InvitationPurpose.UNIVERSITY_ADMIN, InvitationPurpose.UNIVERSITY_USER):
        kind = InstitutionKind.UNIVERSITY
    elif purpose == InvitationPurpose.PLATFORM_ADMIN:
        kind = None
    else:
        kind = InstitutionKind.COLLEGE
    inv = Invitation(purpose=purpose, email=email, full_name=full_name, **ref(institution),
                     parent_university_id=parent.id if parent else None,
                     aishe_code=(aishe_code or (institution.aishe_code if institution else None)),
                     institution_kind=kind,
                     bundles=[str(b) for b in bundles or []], permissions=[str(p) for p in permissions or []],
                     pre_approved=pre_approved, token_hash=hash_token(token),
                     expires_at=utcnow() + current_app.config["INVITATION_EXPIRES"],
                     invited_by_id=invited_by.id if invited_by else None, note=note)
    db.session.add(inv)
    db.session.flush()
    name = institution.name if institution else (aishe_code or "the Student Migration System")
    link = _invite_link(token)
    notify_email_address(email, NotificationEvent.INVITATION_SENT,
                         {"institution": name, "role": inv.purpose.value.replace("_", " ").title(),
                          "days": days, "link": link}, link=link)
    activity.step("Invitation created and emailed", entity="invitation", entity_id=inv.id,
                  purpose=inv.purpose, email=email, pre_approved=pre_approved)
    return inv, token


def _debug_token(token):
    return {"debug_token": token} if current_app.config["EXPOSE_DEBUG_OTP"] else {}


def get_invitation_by_token(token):
    inv = Invitation.query.filter_by(token_hash=hash_token(token or "")).first()
    if inv is None or not is_active(inv):
        raise NotFound("Invitation not found", error_code="INVITATION_INVALID")
    if inv.invitation_state != InvitationState.SENT:
        raise BusinessRuleViolation(f"This invitation is {inv.invitation_state.value.lower()}",
                                    error_code="INVITATION_USED")
    if ensure_aware(inv.expires_at) < utcnow():
        inv.invitation_state = InvitationState.EXPIRED
        deactivate(inv)
        db.session.commit()
        raise BusinessRuleViolation("This invitation has expired", error_code="INVITATION_EXPIRED")
    return inv


def describe_invitation(inv):
    inst = target(inv)
    if inst is None and inv.aishe_code:
        inst = find_by_aishe(inv.aishe_code, inv.institution_kind)
    return {
        "purpose": inv.purpose.value,
        "email": inv.email,
        "full_name": inv.full_name,
        "aishe_code": inv.aishe_code,
        "institution_kind": inv.institution_kind.value if inv.institution_kind else None,
        "registration_type": TYPE_FOR_PURPOSE.get(inv.purpose),
        "pre_approved": inv.pre_approved,
        "expires_at": inv.expires_at.isoformat(),
        "institution": institution_card(inst) if inst else None,
        "requires_institution_details": inst is None and inv.purpose in TYPE_FOR_PURPOSE,
        "requires_authorization_letter": (not inv.pre_approved) and inv.purpose in TYPE_FOR_PURPOSE,
        "existing_account": User.query.filter(func.lower(User.email) == inv.email).first() is not None,
    }


# ------------------------------------------------------- direct creation
def _ensure_target_institution(reg_type, aishe_code, details, parent=None):
    inst = lookup_institution(aishe_code, reg_type)
    if inst is None:
        if not details:
            raise ValidationError("AISHE code not found; institution details are required",
                                  error_code="INSTITUTION_DETAILS_REQUIRED")
        if parent is not None:
            details = {**details, "parent_university_id": parent.id}
        inst = create_manual_institution(aishe_code, reg_type, details)
    elif parent is not None and inst.university_id != parent.id:
        raise PermissionDenied("This college is affiliated to another university", error_code="WRONG_UNIVERSITY")
    return inst


def create_institution_and_invite_admin(actor, reg_type, aishe_code, admin_email, admin_name,
                                        details=None, parent=None):
    """Path A: approver creates/picks the institution and invites a pre-approved admin.

    ``reg_type`` is UNIVERSITY, COLLEGE (needs ``parent``, a University) or STANDALONE.
    """
    reg_type = _reg_type(reg_type)
    if reg_type in ("UNIVERSITY", "STANDALONE") and not actor.is_platform_admin:
        raise PermissionDenied("Only the Platform Admin can create universities and standalone institutions")
    if reg_type == "COLLEGE":
        if parent is None:
            raise ValidationError("Parent university is required")
        actor.require(Permission.CREATE_COLLEGE, parent)
        if not is_operational(parent):
            raise BusinessRuleViolation("The university is not active", error_code="UNIVERSITY_NOT_ACTIVE")
    inst = _ensure_target_institution(reg_type, aishe_code, details, parent)
    if inst.onboarding_status in (OnboardingStatus.MASTER, OnboardingStatus.REJECTED):
        inst.onboarding_status = OnboardingStatus.PENDING
    inv, token = create_invitation(PURPOSE_FOR_TYPE[reg_type], admin_email, actor.user,
                                   institution=inst, pre_approved=True, full_name=admin_name)
    db.session.commit()
    return inst, inv, _debug_token(token)


def send_signup_link(actor, reg_type, aishe_code, email, parent=None, note=None):
    """Path B: a single-use signup link bound to the email and AISHE code."""
    reg_type = _reg_type(reg_type)
    code = aishe_code.strip().upper()
    if not valid_aishe(code, reg_type):
        raise ValidationError("Invalid AISHE code for this institution type", error_code="INVALID_AISHE_CODE")
    if reg_type == "COLLEGE":
        if parent is None:
            raise ValidationError("Parent university is required")
        actor.require(Permission.CREATE_COLLEGE, parent)
    elif not actor.is_platform_admin:
        raise PermissionDenied("Only the Platform Admin can send this link")
    inst = lookup_institution(code, reg_type)
    if inst is not None and parent is not None and inst.university_id != parent.id:
        raise PermissionDenied("This college is affiliated to another university", error_code="WRONG_UNIVERSITY")
    inv, token = create_invitation(PURPOSE_FOR_TYPE[reg_type], email, actor.user, institution=inst,
                                   parent=parent, aishe_code=code, note=note)
    db.session.commit()
    return inv, _debug_token(token)


def invite_platform_admin(actor, email, full_name):
    if not actor.is_platform_admin:
        raise PermissionDenied("Only a Platform Admin can add Platform Admins")
    inv, token = create_invitation(InvitationPurpose.PLATFORM_ADMIN, email, actor.user, pre_approved=True,
                                   full_name=full_name)
    db.session.commit()
    return inv, _debug_token(token)


# ------------------------------------------------------- accept invitation
def _new_user(email, full_name, password, mobile, designation, user_type):
    from app.core.security import validate_password_policy
    if User.query.filter(func.lower(User.email) == email.lower()).first():
        raise Conflict("An account with this email already exists", error_code="EMAIL_TAKEN")
    validate_password_policy(password)
    user = User(email=email.lower(), full_name=full_name, password_hash=hash_password(password),
                password_changed_at=utcnow(), mobile=mobile, designation=designation, user_type=user_type,
                account_state=AccountState.PENDING_VERIFICATION)
    db.session.add(user)
    db.session.flush()
    activity.step("Created user account", entity="user", entity_id=user.id, user_type=user_type)
    return user


def _approver_university_for(inst):
    """None = Platform Admin. Affiliated colleges go to their university when it has an admin."""
    if is_affiliated(inst):
        parent = inst.university
        if parent and is_operational(parent) and has_active_admin(parent):
            return parent.id
    return None


def _notify_approvers(reg, inst, requester):
    ctx = {"requester": requester.full_name, "institution": inst.name}
    approver = db.session.get(University, reg.approver_university_id) if reg.approver_university_id else None
    approvers = institution_admins(approver) if approver else platform_admins()
    for u in approvers:
        notify(u, NotificationEvent.REGISTRATION_PENDING_APPROVAL, ctx)
    if reg.is_second_claim:
        prim = primary_admin(inst)
        if prim:
            notify(prim.user, NotificationEvent.SECOND_ADMIN_CLAIM, ctx)


def _open_registration(inst, user, path, invitation, designation, official_email, letter_file, manual):
    second_claim = has_active_admin(inst)
    reg = RegistrationRequest(institution_kind=kind_of(inst), path=path, **ref(inst),
                              requester_id=user.id, invitation_id=invitation.id if invitation else None,
                              approver_university_id=_approver_university_for(inst),
                              designation=designation, official_email=official_email,
                              is_manual_institution=manual, is_second_claim=second_claim,
                              manual_details=None)
    db.session.add(reg)
    db.session.flush()
    if letter_file is None:
        raise ValidationError("An authorization letter signed by the Registrar or Principal is required",
                              error_code="AUTHORIZATION_LETTER_REQUIRED")
    doc = store_upload(letter_file, DocumentOwnerType.REGISTRATION_REQUEST, reg.id,
                       DocumentType.AUTHORIZATION_LETTER)
    reg.authorization_letter_id = doc.id
    activity.step("Registration request opened", entity="registration_request", entity_id=reg.id,
                  second_claim=second_claim, approver_university_id=reg.approver_university_id)
    _notify_approvers(reg, inst, user)
    return reg


def accept_invitation(token, data, letter_file=None):
    inv = get_invitation_by_token(token)
    activity.step("Invitation validated", entity="invitation", entity_id=inv.id, purpose=inv.purpose)
    existing = User.query.filter(func.lower(User.email) == inv.email).first()
    if existing is not None:
        if not verify_password(existing.password_hash, data.get("password", "")):
            raise PermissionDenied("An account with this email exists. Enter its password to accept.",
                                   error_code="EXISTING_ACCOUNT_PASSWORD_REQUIRED")
        if inv.purpose == InvitationPurpose.PLATFORM_ADMIN or existing.user_type != UserType.STAFF:
            raise BusinessRuleViolation("This account type cannot accept the invitation",
                                        error_code="INVITATION_ROLE_MISMATCH")
        user = existing
    else:
        user_type = UserType.PLATFORM_ADMIN if inv.purpose == InvitationPurpose.PLATFORM_ADMIN else UserType.STAFF
        user = _new_user(inv.email, data.get("full_name") or inv.full_name, data["password"],
                         data.get("mobile"), data.get("designation"), user_type)

    result = {"user_id": user.id}
    if inv.purpose == InvitationPurpose.PLATFORM_ADMIN:
        pass
    elif inv.purpose in TYPE_FOR_PURPOSE:
        result.update(_accept_admin_invitation(inv, user, data, letter_file))
    else:
        result.update(_accept_staff_invitation(inv, user))

    inv.invitation_state = InvitationState.ACCEPTED
    inv.accepted_at = utcnow()
    inv.accepted_by_id = user.id
    deactivate(inv)
    if user.email_verified_at is None:
        code = otp_service.issue_otp(user, OtpPurpose.EMAIL_VERIFICATION)
        result.update(otp_service.debug_payload(code))
        result["next_step"] = "VERIFY_EMAIL"
    db.session.commit()
    return result


def _membership_of(user_id, inst):
    return InstitutionMembership.query.filter(InstitutionMembership.user_id == user_id,
                                              ref_filter(InstitutionMembership, inst)).first()


def _accept_admin_invitation(inv, user, data, letter_file):
    reg_type = TYPE_FOR_PURPOSE[inv.purpose]
    inst = target(inv)
    manual = False
    if inst is None:
        inst = lookup_institution(inv.aishe_code, reg_type)
    if inst is None:
        details = dict(data.get("institution") or {})
        if inv.parent_university_id:
            details["parent_university_id"] = inv.parent_university_id
        inst = create_manual_institution(inv.aishe_code, reg_type, details)
        manual = True
    if registration_type(inst) != reg_type:
        raise BusinessRuleViolation("Invitation and institution type do not match",
                                    error_code="INVITATION_ROLE_MISMATCH")
    if _membership_of(user.id, inst):
        raise Conflict("You are already linked to this institution", error_code="ALREADY_MEMBER")
    is_primary = primary_admin(inst) is None
    membership = InstitutionMembership(user_id=user.id, **ref(inst), role=MembershipRole.ADMIN,
                                       is_primary=is_primary, joined_via="INVITATION",
                                       approval_status=ApprovalStatus.PENDING)
    db.session.add(membership)
    db.session.flush()
    out = {"institution_kind": kind_of(inst).value, "institution_id": inst.id, "membership_id": membership.id}
    if inv.pre_approved:
        _approve_membership(membership, inv.invited_by_id)
        _activate_institution(inst, user)
        activity.step("Pre-approved admin activated", entity="institution_membership", entity_id=membership.id)
    else:
        if inst.onboarding_status in (OnboardingStatus.MASTER, OnboardingStatus.REJECTED):
            inst.onboarding_status = OnboardingStatus.PENDING
        reg = _open_registration(inst, user, RegistrationPath.INVITE_LINK, inv, data.get("designation"),
                                 data.get("official_email") or user.email, letter_file, manual)
        out["registration_request_id"] = reg.id
    return out


def _accept_staff_invitation(inv, user):
    inst = target(inv)
    if inst is None or not is_operational(inst):
        raise BusinessRuleViolation("The institution is not active", error_code="INSTITUTION_NOT_ACTIVE")
    role = MembershipRole.ADMIN if inv.note == "SECONDARY_ADMIN" else MembershipRole.USER
    membership = _membership_of(user.id, inst)
    if membership is None:
        membership = InstitutionMembership(user_id=user.id, **ref(inst), role=role,
                                           bundles=inv.bundles, permissions=inv.permissions,
                                           joined_via="INVITATION")
        db.session.add(membership)
    else:
        membership.role, membership.bundles, membership.permissions = role, inv.bundles, inv.permissions
        activate(membership)
    db.session.flush()
    _approve_membership(membership, inv.invited_by_id)
    return {"institution_kind": kind_of(inst).value, "institution_id": inst.id, "membership_id": membership.id}


def _approve_membership(membership, approver_id):
    membership.approval_status = ApprovalStatus.APPROVED
    membership.approved_by_id = approver_id
    membership.approved_at = utcnow()
    activate(membership)
    user = membership.user or db.session.get(User, membership.user_id)
    if user.email_verified_at and user.account_state in (AccountState.PENDING_APPROVAL, AccountState.REJECTED):
        user.account_state = AccountState.ACTIVE


def _activate_institution(inst, admin_user):
    if inst.onboarding_status != OnboardingStatus.ACTIVE:
        inst.onboarding_status = OnboardingStatus.ACTIVE
        inst.onboarded_at = utcnow()
    domain = email_domain(admin_user.email)
    if domain and domain not in GENERIC_EMAIL_DOMAINS and domain not in (inst.email_domains or []):
        inst.email_domains = list(inst.email_domains or []) + [domain]


# ------------------------------------------------- public registration (C)
def submit_registration_request(data, letter_file):
    reg_type = _reg_type(data["institution_type"])
    code = data["aishe_code"].strip().upper()
    inst = lookup_institution(code, reg_type)
    manual = False
    if inst is None:
        details = dict(data.get("institution") or {})
        if reg_type == "COLLEGE":
            parent = lookup_institution(details.get("parent_university_aishe_code") or "", "UNIVERSITY")
            if parent is None:
                raise ValidationError("The parent university's AISHE code was not found",
                                      error_code="INVALID_PARENT")
            details["parent_university_id"] = parent.id
        inst = create_manual_institution(code, reg_type, details)
        manual = True
    elif inst.onboarding_status == OnboardingStatus.SUSPENDED:
        raise BusinessRuleViolation("This institution is suspended", error_code="INSTITUTION_SUSPENDED")
    user = _new_user(data["email"], data["full_name"], data["password"], data.get("mobile"),
                     data.get("designation"), UserType.STAFF)
    membership = InstitutionMembership(user_id=user.id, **ref(inst), role=MembershipRole.ADMIN,
                                       is_primary=primary_admin(inst) is None, joined_via="REGISTRATION",
                                       approval_status=ApprovalStatus.PENDING)
    db.session.add(membership)
    if inst.onboarding_status in (OnboardingStatus.MASTER, OnboardingStatus.REJECTED):
        inst.onboarding_status = OnboardingStatus.PENDING
    reg = _open_registration(inst, user, RegistrationPath.REQUEST, None, data.get("designation"),
                             data.get("official_email") or user.email, letter_file, manual)
    code_sent = otp_service.issue_otp(user, OtpPurpose.EMAIL_VERIFICATION)
    db.session.commit()
    return {"registration_request_id": reg.id, "user_id": user.id,
            "institution_kind": kind_of(inst).value, "institution_id": inst.id,
            "next_step": "VERIFY_EMAIL", **otp_service.debug_payload(code_sent)}


# ------------------------------------------------------- review decisions
def _require_reviewer(actor, reg):
    if actor.is_platform_admin:
        return
    approver = db.session.get(University, reg.approver_university_id) if reg.approver_university_id else None
    if approver is not None and actor.can(Permission.APPROVE_COLLEGE_REGISTRATION, approver):
        return
    raise PermissionDenied("You cannot review this registration")


def visible_registrations(actor):
    q = active(RegistrationRequest)
    if actor.is_platform_admin:
        return q
    ids = [m.university_id for m in actor.memberships
           if m.university_id and actor.can(Permission.APPROVE_COLLEGE_REGISTRATION, m.university)]
    return q.filter(RegistrationRequest.approver_university_id.in_(ids or [-1]))


def approve_registration(actor, reg, note=None):
    _require_reviewer(actor, reg)
    if reg.approval_status != ApprovalStatus.PENDING:
        raise BusinessRuleViolation("This request was already decided", error_code="ALREADY_DECIDED")
    inst = target(reg)
    if is_affiliated(inst) and not is_operational(inst.university):
        raise BusinessRuleViolation("The parent university is not active", error_code="UNIVERSITY_NOT_ACTIVE")
    membership = _membership_of(reg.requester_id, inst)
    if reg.is_second_claim or primary_admin(inst) is not None:
        membership.is_primary = False
    else:
        membership.is_primary = True
    _approve_membership(membership, actor.user.id)
    _activate_institution(inst, reg.requester)
    reg.approval_status = ApprovalStatus.APPROVED
    reg.reviewer_id = actor.user.id
    reg.reviewed_at = utcnow()
    reg.review_note = note
    activity.step("Registration approved", entity="registration_request", entity_id=reg.id,
                  institution_kind=kind_of(inst), institution_id=inst.id, primary=membership.is_primary)
    activity.set_institution(inst)
    notify(reg.requester, NotificationEvent.REGISTRATION_DECIDED,
           {"institution": inst.name, "decision": "approved", "note": note or ""})
    db.session.commit()
    return reg


def reject_registration(actor, reg, reason):
    _require_reviewer(actor, reg)
    if not reason:
        raise ValidationError("A reason is required to reject")
    if reg.approval_status != ApprovalStatus.PENDING:
        raise BusinessRuleViolation("This request was already decided", error_code="ALREADY_DECIDED")
    inst = target(reg)
    membership = _membership_of(reg.requester_id, inst)
    if membership:
        membership.approval_status = ApprovalStatus.REJECTED
        membership.rejection_reason = reason
        membership.is_primary = False
        deactivate(membership)
    user = reg.requester
    if not any(m.approval_status == ApprovalStatus.APPROVED and is_active(m) for m in user.memberships):
        user.account_state = AccountState.REJECTED
    if not has_active_admin(inst) and inst.onboarding_status == OnboardingStatus.PENDING:
        inst.onboarding_status = (OnboardingStatus.REJECTED if inst.source == InstitutionSource.MANUAL
                                  else OnboardingStatus.MASTER)
    reg.approval_status = ApprovalStatus.REJECTED
    reg.reviewer_id = actor.user.id
    reg.reviewed_at = utcnow()
    reg.review_note = reason
    activity.step("Registration rejected", entity="registration_request", entity_id=reg.id, reason=reason)
    notify(user, NotificationEvent.REGISTRATION_DECIDED,
           {"institution": inst.name, "decision": "rejected", "note": reason})
    db.session.commit()
    return reg


# --------------------------------------------------------- admin handover
def transfer_primary_admin(actor, institution, to_membership):
    current = primary_admin(institution)
    if not actor.is_platform_admin and (current is None or current.user_id != actor.user.id):
        raise PermissionDenied("Only the primary admin can hand over the role")
    if (not points_at(to_membership, institution) or to_membership.role != MembershipRole.ADMIN
            or to_membership.approval_status != ApprovalStatus.APPROVED or not is_active(to_membership)):
        raise ValidationError("The new primary must be an approved admin of this institution")
    if current:
        current.is_primary = False
    to_membership.is_primary = True
    activity.step("Primary admin transferred", entity=kind_of(institution).value.lower(), entity_id=institution.id,
                  from_user=current.user_id if current else None, to_user=to_membership.user_id)
    db.session.commit()


# ------------------------------------------------------------- staff users
def _grantable_for(inst):
    return UNIVERSITY_USER_GRANTABLE if is_university(inst) else COLLEGE_USER_GRANTABLE


def _validate_grants(inst, bundles, permissions):
    granted = expand(bundles, permissions)
    illegal = granted - set(_grantable_for(inst))
    if illegal:
        raise ValidationError("These permissions cannot be granted here: " +
                              ", ".join(sorted(p.value for p in illegal)), error_code="PERMISSION_NOT_GRANTABLE")


def invite_staff(actor, inst, email, full_name, bundles=(), permissions=(), secondary_admin=False):
    actor.require(Permission.MANAGE_INSTITUTION_USERS, inst)
    if not is_operational(inst):
        raise BusinessRuleViolation("The institution is not active", error_code="INSTITUTION_NOT_ACTIVE")
    if secondary_admin and not (actor.is_platform_admin or actor.admin_of(inst)):
        raise PermissionDenied("Only an admin can add another admin")
    if not secondary_admin:
        _validate_grants(inst, bundles, permissions)
    purpose = InvitationPurpose.UNIVERSITY_USER if is_university(inst) else InvitationPurpose.COLLEGE_USER
    inv, token = create_invitation(purpose, email, actor.user, institution=inst, bundles=bundles,
                                   permissions=permissions, pre_approved=True, full_name=full_name,
                                   note="SECONDARY_ADMIN" if secondary_admin else None)
    activity.set_institution(inst)
    db.session.commit()
    return inv, _debug_token(token)


def request_staff_access(data):
    inst = get_institution(data.get("institution_kind") or InstitutionKind.COLLEGE, data["institution_id"],
                           required=False)
    if inst is None or not is_operational(inst):
        raise BusinessRuleViolation("This institution is not on the platform yet", error_code="INSTITUTION_NOT_ACTIVE")
    user = _new_user(data["email"], data["full_name"], data["password"], data.get("mobile"),
                     data.get("designation"), UserType.STAFF)
    membership = InstitutionMembership(user_id=user.id, **ref(inst), role=MembershipRole.USER,
                                       joined_via="ACCESS_REQUEST", approval_status=ApprovalStatus.PENDING)
    db.session.add(membership)
    db.session.flush()
    for admin in institution_admins(inst):
        notify(admin, NotificationEvent.ACCESS_REQUEST_PENDING,
               {"requester": user.full_name, "institution": inst.name})
    code = otp_service.issue_otp(user, OtpPurpose.EMAIL_VERIFICATION)
    activity.step("Access request recorded", entity="institution_membership", entity_id=membership.id)
    activity.set_institution(inst)
    db.session.commit()
    return {"user_id": user.id, "membership_id": membership.id, "next_step": "VERIFY_EMAIL",
            **otp_service.debug_payload(code)}


def decide_access_request(actor, membership, approve, bundles=(), permissions=(), reason=None):
    inst = membership_institution(membership)
    actor.require(Permission.MANAGE_INSTITUTION_USERS, inst)
    if actor.user.id == membership.user_id:
        raise PermissionDenied("You cannot decide your own access request")
    if membership.approval_status != ApprovalStatus.PENDING or membership.role != MembershipRole.USER:
        raise BusinessRuleViolation("This access request was already decided", error_code="ALREADY_DECIDED")
    if approve:
        _validate_grants(inst, bundles, permissions)
        membership.bundles = [str(b) for b in bundles]
        membership.permissions = [str(p) for p in permissions]
        _approve_membership(membership, actor.user.id)
        decision = "approved"
    else:
        if not reason:
            raise ValidationError("A reason is required to reject")
        membership.approval_status = ApprovalStatus.REJECTED
        membership.rejection_reason = reason
        deactivate(membership)
        if not any(m.approval_status == ApprovalStatus.APPROVED and is_active(m)
                   for m in membership.user.memberships):
            membership.user.account_state = AccountState.REJECTED
        decision = "rejected"
    activity.step(f"Access request {decision}", entity="institution_membership", entity_id=membership.id)
    activity.set_institution(inst)
    notify(membership.user, NotificationEvent.ACCESS_REQUEST_DECIDED,
           {"institution": inst.name, "decision": decision, "note": reason or ""})
    db.session.commit()
    return membership


def update_member_permissions(actor, membership, bundles, permissions):
    inst = membership_institution(membership)
    actor.require(Permission.MANAGE_INSTITUTION_USERS, inst)
    if membership.role != MembershipRole.USER:
        raise BusinessRuleViolation("Admins hold all permissions; change users only")
    _validate_grants(inst, bundles, permissions)
    activity.change("institution_membership", membership.id, "bundles", membership.bundles, list(map(str, bundles)))
    activity.change("institution_membership", membership.id, "permissions", membership.permissions,
                    list(map(str, permissions)))
    membership.bundles = [str(b) for b in bundles]
    membership.permissions = [str(p) for p in permissions]
    activity.set_institution(inst)
    db.session.commit()
    return membership


def set_member_active(actor, membership, active):
    from app.core.auth import end_all_sessions
    inst = membership_institution(membership)
    actor.require(Permission.MANAGE_INSTITUTION_USERS, inst)
    if membership.user_id == actor.user.id:
        raise PermissionDenied("You cannot change your own access")
    if not active and membership.is_primary:
        raise BusinessRuleViolation("Transfer the primary admin role before deactivating this admin",
                                    error_code="PRIMARY_ADMIN")
    if membership.role == MembershipRole.ADMIN and not (actor.is_platform_admin or actor.admin_of(inst)):
        raise PermissionDenied("Only an admin can change another admin")
    if active:
        activate(membership)
    else:
        deactivate(membership)
        others = [m for m in membership.user.memberships if m.id != membership.id and is_active(m)]
        if not others:
            end_all_sessions(membership.user, SessionEndReason.DEACTIVATED)
    activity.step("Member " + ("activated" if active else "deactivated"),
                  entity="institution_membership", entity_id=membership.id)
    activity.set_institution(inst)
    db.session.commit()
    return membership


# ---------------------------------------------- student-led onboarding (4.4)
def request_college_onboarding(student_user, college_id, contact_email, contact_name=None, note=None):
    inst = db.session.get(College, college_id)
    if inst is None:
        raise NotFound("College not found")
    if is_operational(inst) and has_active_admin(inst):
        raise BusinessRuleViolation("This college is already on the platform", error_code="ALREADY_ONBOARDED")
    row = CollegeOnboardingRequest(student_user_id=student_user.id, college_id=inst.id,
                                   contact_email=contact_email.lower(), contact_name=contact_name, note=note)
    db.session.add(row)
    db.session.flush()
    for pa in platform_admins():
        notify(pa, NotificationEvent.COLLEGE_ONBOARDING_REQUESTED,
               {"student": student_user.full_name, "institution": inst.name})
    activity.step("Onboarding request recorded", entity="college_onboarding_request", entity_id=row.id)
    db.session.commit()
    return row


def decide_college_onboarding(actor, row, approve, note=None):
    if not actor.is_platform_admin:
        raise PermissionDenied("Platform Admin only")
    if row.approval_status != ApprovalStatus.PENDING:
        raise BusinessRuleViolation("Already decided", error_code="ALREADY_DECIDED")
    row.reviewed_by_id = actor.user.id
    row.reviewed_at = utcnow()
    debug = {}
    if approve:
        inst = row.college
        inv, token = create_invitation(PURPOSE_FOR_TYPE[registration_type(inst)], row.contact_email, actor.user,
                                       institution=inst, aishe_code=inst.aishe_code,
                                       parent=inst.university, full_name=row.contact_name, note=note)
        row.approval_status = ApprovalStatus.APPROVED
        debug = _debug_token(token)
        activity.step("Signup link sent to the college", entity="invitation", entity_id=inv.id)
    else:
        row.approval_status = ApprovalStatus.REJECTED
    db.session.commit()
    return row, debug


def ensure_min_platform_admins(excluding_user_id):
    remaining = [u for u in platform_admins() if u.id != excluding_user_id]
    if len(remaining) < current_app.config["MIN_PLATFORM_ADMINS"]:
        raise BusinessRuleViolation(
            f"At least {current_app.config['MIN_PLATFORM_ADMINS']} Platform Admins must remain active",
            error_code="MIN_PLATFORM_ADMINS")


def states_and_districts():
    return [{"id": s.id, "name": s.name} for s in State.query.order_by(State.name)]
