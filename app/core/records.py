"""Small helpers for working with model rows (the models themselves are plain columns).

Status rule: every business table has status (ACTIVE / INACTIVE) and deleted_at.
Always change status with activate() / deactivate(): INACTIVE fills deleted_at,
ACTIVE clears it.
"""
from datetime import datetime, timezone

from app.enums import OnboardingStatus, RecordStatus, UserType


# ------------------------------------------------------------------ status
def active(model):
    """Query of a model's ACTIVE rows: active(Course).filter_by(...)."""
    return model.query.filter(model.status == RecordStatus.ACTIVE)


def is_active(row):
    return row is not None and row.status == RecordStatus.ACTIVE


def deactivate(row):
    row.status = RecordStatus.INACTIVE
    if row.deleted_at is None:
        row.deleted_at = datetime.now(timezone.utc)
    return row


def activate(row):
    row.status = RecordStatus.ACTIVE
    row.deleted_at = None
    return row


# ------------------------------------------------------------------ users
def is_platform_admin_user(user):
    return user.user_type == UserType.PLATFORM_ADMIN


def is_student_user(user):
    return user.user_type == UserType.STUDENT


def user_public_dict(user):
    """A user for API responses (no password or lockout counters)."""
    return user.to_dict(exclude=("failed_login_count", "lockout_count", "lockout_window_started_at"))


def student_name(profile):
    return " ".join(p for p in (profile.first_name, profile.last_name) if p)


# ------------------------------------------------------------------ institutions
def is_operational(inst):
    """Onboarded and not deactivated."""
    return inst is not None and inst.onboarding_status == OnboardingStatus.ACTIVE and is_active(inst)


# ------------------------------------------------------------------ seats
def category_available(cat):
    return max(cat.seats - cat.blocked - cat.filled, 0)


def vacancy_total_seats(vacancy):
    return sum(c.seats for c in vacancy.categories if is_active(c))


def vacancy_available(vacancy):
    return max(sum(category_available(c) for c in vacancy.categories if is_active(c)), 0)
