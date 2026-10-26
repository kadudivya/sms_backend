"""Who should hear about something: platform admins, institution admins, holders of a permission.

Every function takes a University or College row.
"""
from app.core.auth import Actor
from app.core.institutions import parent_university, ref_filter
from app.enums import ApprovalStatus, MembershipRole, RecordStatus, UserType
from app.models import InstitutionMembership, User
from app.core.records import active


def platform_admins():
    return active(User).filter(User.user_type == UserType.PLATFORM_ADMIN).all()


def _approved_members(inst):
    return (active(InstitutionMembership)
            .filter(ref_filter(InstitutionMembership, inst),
                    InstitutionMembership.approval_status == ApprovalStatus.APPROVED).all())


def institution_admins(inst):
    return [m.user for m in _approved_members(inst)
            if m.role == MembershipRole.ADMIN and m.user.status == RecordStatus.ACTIVE]


def has_active_admin(inst):
    return bool(institution_admins(inst))


def primary_admin(inst):
    for m in _approved_members(inst):
        if m.role == MembershipRole.ADMIN and m.is_primary:
            return m
    return None


def holders_of(inst, permission):
    """Users of the institution itself who hold ``permission`` there."""
    out = []
    for m in _approved_members(inst):
        if m.user.status != RecordStatus.ACTIVE:
            continue
        if permission in Actor(m.user).membership_permissions(m):
            out.append(m.user)
    return out


def responsible_users(inst, permission):
    """Holders at the institution; else its university's holders; else platform admins."""
    users = holders_of(inst, permission)
    if users:
        return users
    parent = parent_university(inst)
    if parent is not None:
        users = holders_of(parent, permission)
        if users:
            return users
    return platform_admins()
