from flask import Blueprint, g, request
from marshmallow import Schema, fields, validate

from app.core.api import api
from app.core.errors import ValidationError
from app.core.permissions import BUNDLES, COLLEGE_USER_GRANTABLE, UNIVERSITY_USER_GRANTABLE
from app.core.responses import created, ok, paginated
from app.core.validation import get_or_404, load
from app.enums import (ApprovalStatus, InvitationState, MembershipRole, Permission, PermissionBundle,
                       RecordStatus)
from app.extensions import db, limiter
from app.core.institutions import get_institution, ref_filter, target as institution_target
from app.models import InstitutionMembership, Invitation
from app.services import onboarding as svc
from app.core.records import active, deactivate

bp = Blueprint("staff", __name__, url_prefix="")
# <kind> is "university" or "college": the table the id belongs to
INSTITUTION_URL = "/institutions/<any(university,college):kind>/<int:institution_id>"


class InviteStaffSchema(Schema):
    email = fields.Email(required=True)
    full_name = fields.String(required=True, validate=validate.Length(min=2, max=200))
    bundles = fields.List(fields.String(validate=validate.OneOf(PermissionBundle.values())), load_default=list)
    permissions = fields.List(fields.String(validate=validate.OneOf(Permission.values())), load_default=list)
    secondary_admin = fields.Boolean(load_default=False)


class AccessRequestSchema(Schema):
    institution_kind = fields.String(load_default="COLLEGE", validate=validate.OneOf(["UNIVERSITY", "COLLEGE"]))
    institution_id = fields.Integer(required=True)
    full_name = fields.String(required=True, validate=validate.Length(min=2, max=200))
    email = fields.Email(required=True)
    password = fields.String(required=True)
    confirm_password = fields.String(required=True)
    mobile = fields.String(validate=validate.Regexp(r"^(\+91)?[6-9]\d{9}$"))
    designation = fields.String(required=True, validate=validate.Length(max=150))


class AccessDecisionSchema(Schema):
    approve = fields.Boolean(required=True)
    bundles = fields.List(fields.String(validate=validate.OneOf(PermissionBundle.values())), load_default=list)
    permissions = fields.List(fields.String(validate=validate.OneOf(Permission.values())), load_default=list)
    reason = fields.String(validate=validate.Length(max=500))


class PermissionUpdateSchema(Schema):
    bundles = fields.List(fields.String(validate=validate.OneOf(PermissionBundle.values())), required=True)
    permissions = fields.List(fields.String(validate=validate.OneOf(Permission.values())), load_default=list)


class TransferSchema(Schema):
    to_membership_id = fields.Integer(required=True)


def _member_view(m):
    return {
        "membership_id": m.id, "user_id": m.user_id, "full_name": m.user.full_name, "email": m.user.email,
        "mobile": m.user.mobile, "designation": m.user.designation, "role": m.role.value,
        "is_primary": m.is_primary, "bundles": m.bundles, "permissions": m.permissions,
        "approval_status": m.approval_status.value, "status": m.status.value,
        "joined_via": m.joined_via, "created_at": m.created_at.isoformat(),
        "account_state": m.user.account_state.value,
    }


@bp.get("/permission-bundles")
@api("LIST_PERMISSION_BUNDLES", allow_pending=True)
def list_bundles():
    return ok({
        "bundles": {b.value: sorted(p.value for p in perms) for b, perms in BUNDLES.items()},
        "university_user_grantable": sorted(p.value for p in UNIVERSITY_USER_GRANTABLE),
        "college_user_grantable": sorted(p.value for p in COLLEGE_USER_GRANTABLE),
    })


@bp.post(INSTITUTION_URL + "/invitations")
@api("INVITE_STAFF_USER",
     permissions=[Permission.MANAGE_INSTITUTION_USERS])
def invite_staff(kind, institution_id):
    inst = get_institution(kind, institution_id)
    data = load(InviteStaffSchema)
    inv, debug = svc.invite_staff(g.actor, inst, data["email"], data["full_name"], data["bundles"],
                                  data["permissions"], data["secondary_admin"])
    return created({"invitation_id": inv.id, "expires_at": inv.expires_at.isoformat(), **debug},
                   "Invitation sent")


@bp.get(INSTITUTION_URL + "/invitations")
@api("LIST_INVITATIONS", permissions=[Permission.MANAGE_INSTITUTION_USERS])
def list_invitations(kind, institution_id):
    inst = get_institution(kind, institution_id)
    g.actor.require(Permission.MANAGE_INSTITUTION_USERS, inst)
    q = Invitation.query.filter(ref_filter(Invitation, inst)).order_by(Invitation.created_at.desc())
    return paginated(q, lambda i: i.to_dict())


@bp.post("/invitations/<int:invitation_id>/revoke")
@api("REVOKE_INVITATION", permissions=[Permission.MANAGE_INSTITUTION_USERS])
def revoke_invitation(invitation_id):
    inv = get_or_404(Invitation, invitation_id, label="Invitation")
    inv_inst = institution_target(inv)
    if inv_inst is not None:
        g.actor.require(Permission.MANAGE_INSTITUTION_USERS, inv_inst)
    elif not g.actor.is_platform_admin:
        raise ValidationError("Only the Platform Admin can revoke this invitation")
    if inv.invitation_state != InvitationState.SENT:
        raise ValidationError("Only unused invitations can be revoked")
    inv.invitation_state = InvitationState.REVOKED
    deactivate(inv)
    db.session.commit()
    return ok(inv.to_dict(), "Invitation revoked")


@bp.post("/access-requests")
@limiter.limit("10 per hour")
@api("REQUEST_STAFF_ACCESS", auth=False)
def request_access():
    data = load(AccessRequestSchema)
    if data["password"] != data["confirm_password"]:
        raise ValidationError("Password and confirmation do not match")
    return created(svc.request_staff_access(data), "Access requested. Verify your email; the admin will review it.")


@bp.get(INSTITUTION_URL + "/access-requests")
@api("LIST_ACCESS_REQUESTS", permissions=[Permission.MANAGE_INSTITUTION_USERS])
def list_access_requests(kind, institution_id):
    inst = get_institution(kind, institution_id)
    g.actor.require(Permission.MANAGE_INSTITUTION_USERS, inst)
    q = (active(InstitutionMembership)
         .filter(ref_filter(InstitutionMembership, inst),
                 InstitutionMembership.role == MembershipRole.USER,
                 InstitutionMembership.approval_status == ApprovalStatus.PENDING)
         .order_by(InstitutionMembership.created_at))
    return paginated(q, _member_view)


@bp.post("/memberships/<int:membership_id>/access-decision")
@api("DECIDE_ACCESS_REQUEST",
     permissions=[Permission.MANAGE_INSTITUTION_USERS])
def decide_access(membership_id):
    m = get_or_404(InstitutionMembership, membership_id, label="Access request")
    data = load(AccessDecisionSchema)
    svc.decide_access_request(g.actor, m, data["approve"], data["bundles"], data["permissions"], data.get("reason"))
    return ok(_member_view(m), "Decision recorded")


@bp.get(INSTITUTION_URL + "/members")
@api("LIST_INSTITUTION_USERS",
     permissions=[Permission.MANAGE_INSTITUTION_USERS])
def list_members(kind, institution_id):
    inst = get_institution(kind, institution_id)
    g.actor.require(Permission.MANAGE_INSTITUTION_USERS, inst)
    q = InstitutionMembership.query.filter(ref_filter(InstitutionMembership, inst))
    if request.args.get("status"):
        q = q.filter(InstitutionMembership.status == RecordStatus(request.args["status"]))
    if request.args.get("role"):
        q = q.filter(InstitutionMembership.role == MembershipRole(request.args["role"]))
    return paginated(q.order_by(InstitutionMembership.created_at), _member_view)


@bp.put("/memberships/<int:membership_id>/permissions")
@api("UPDATE_USER_PERMISSIONS", permissions=[Permission.MANAGE_INSTITUTION_USERS])
def update_permissions(membership_id):
    m = get_or_404(InstitutionMembership, membership_id, label="Membership")
    data = load(PermissionUpdateSchema)
    svc.update_member_permissions(g.actor, m, data["bundles"], data["permissions"])
    return ok(_member_view(m), "Permissions updated")


@bp.post("/memberships/<int:membership_id>/deactivate")
@api("DEACTIVATE_INSTITUTION_USER",
     permissions=[Permission.MANAGE_INSTITUTION_USERS])
def deactivate_member(membership_id):
    m = get_or_404(InstitutionMembership, membership_id, label="Membership")
    svc.set_member_active(g.actor, m, False)
    return ok(_member_view(m), "Member deactivated")


@bp.post("/memberships/<int:membership_id>/activate")
@api("ACTIVATE_INSTITUTION_USER", permissions=[Permission.MANAGE_INSTITUTION_USERS])
def activate_member(membership_id):
    m = get_or_404(InstitutionMembership, membership_id, label="Membership")
    svc.set_member_active(g.actor, m, True)
    return ok(_member_view(m), "Member activated")


@bp.post(INSTITUTION_URL + "/transfer-primary-admin")
@api("TRANSFER_PRIMARY_ADMIN",
     permissions=[Permission.MANAGE_INSTITUTION_USERS])
def transfer_primary(kind, institution_id):
    inst = get_institution(kind, institution_id)
    data = load(TransferSchema)
    target = get_or_404(InstitutionMembership, data["to_membership_id"], label="Membership")
    svc.transfer_primary_admin(g.actor, inst, target)
    return ok(_member_view(target), "Primary admin transferred")
