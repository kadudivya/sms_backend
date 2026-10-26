"""Database models for the Student Migration System.

Plain SQLAlchemy models: every table extends db.Model directly and declares all of
its own columns. Each model has a to_dict() that returns every column, with enums
returned as their .value.

Business tables share these columns: id, uuid, status (ACTIVE / INACTIVE), deleted_at,
created_at, updated_at, created_by_id, updated_by_id.

states, districts, universities and colleges are the models from scripts/load_universities.py
(the AISHE loader). states and districts are exactly the loader's; universities and colleges add
the app's columns, each with a server default or nullable so the loader can still insert rows. Change status with
app.core.records.activate() / deactivate(), which also fill or clear deleted_at.
History and log tables (migration_status_history, user_activity) only have id and
created_at because their rows are never edited.
"""
import enum
from datetime import datetime, timezone
from uuid import uuid4

from app.enums import (
    AccountState, ActivityOutcome, AppealDecision, ApprovalStatus, BulkUploadType, CancellationStage,
    ConversionSource, CourseLevel, CreditDecision, DataRequestState, DataRequestType, DocumentOwnerType,
    DocumentType, EligibilityResult, EnrolmentStatus, Gender, GradeSystem, ImportDataset, InstitutionSource,
    InstitutionKind, InterviewMode, InterviewOutcome, InterviewStatus, InvitationPurpose, LocationType, InvitationState,
    LetterStatus, LetterType, MembershipRole, MigrationStatus, MigrationType, NocRefusalReason,
    NotificationChannel, NotificationEvent, NotificationState, OfferMode, OnboardingStatus, OtpPurpose,
    RecordStatus, RegistrationPath, ReservationCategory, SeatState, SessionEndReason, TicketCategory,
    TicketState, TokenPurpose, UserType, VerificationStatus, Weekday,
)
from app.extensions import db


# ======================================================================
# Users, sessions and security
# ======================================================================
class User(db.Model):
    __tablename__ = "users"

    id = db.Column(db.BigInteger, primary_key=True)
    uuid = db.Column(db.String(36), unique=True, nullable=False, index=True, default=lambda: str(uuid4()))
    status = db.Column(db.Enum(RecordStatus, name="record_status"), nullable=False, default=RecordStatus.ACTIVE,
                       server_default=RecordStatus.ACTIVE.value, index=True)
    deleted_at = db.Column(db.DateTime(timezone=True))
    created_at = db.Column(db.DateTime(timezone=True), nullable=False, default=lambda: datetime.now(timezone.utc),
                           server_default=db.func.now())
    updated_at = db.Column(db.DateTime(timezone=True), nullable=False, default=lambda: datetime.now(timezone.utc),
                           onupdate=lambda: datetime.now(timezone.utc), server_default=db.func.now())
    created_by_id = db.Column(db.BigInteger, db.ForeignKey("users.id", use_alter=True, ondelete="SET NULL"))
    updated_by_id = db.Column(db.BigInteger, db.ForeignKey("users.id", use_alter=True, ondelete="SET NULL"))

    email = db.Column(db.String(255), nullable=False, unique=True, index=True)
    mobile = db.Column(db.String(15), nullable=True, index=True)
    full_name = db.Column(db.String(200), nullable=False)
    designation = db.Column(db.String(150))
    password_hash = db.Column(db.String(255))
    password_history = db.Column(db.JSON, nullable=False, default=list)
    password_changed_at = db.Column(db.DateTime(timezone=True))
    user_type = db.Column(db.Enum(UserType, name="user_type"), nullable=False, index=True)
    account_state = db.Column(db.Enum(AccountState, name="account_state"), nullable=False,
                              default=AccountState.PENDING_VERIFICATION, index=True)
    email_verified_at = db.Column(db.DateTime(timezone=True))
    two_factor_enabled = db.Column(db.Boolean, nullable=False, default=False)
    failed_login_count = db.Column(db.Integer, nullable=False, default=0)
    locked_until = db.Column(db.DateTime(timezone=True))
    lockout_count = db.Column(db.Integer, nullable=False, default=0)
    lockout_window_started_at = db.Column(db.DateTime(timezone=True))
    last_login_at = db.Column(db.DateTime(timezone=True))
    preferred_channel = db.Column(db.Enum(NotificationChannel, name="notification_channel"), nullable=False,
                                  default=NotificationChannel.EMAIL)
    whatsapp_opt_in = db.Column(db.Boolean, nullable=False, default=False)
    language = db.Column(db.String(5), nullable=False, default="en")

    memberships = db.relationship(
        "InstitutionMembership", back_populates="user",
        foreign_keys="InstitutionMembership.user_id", lazy="selectin")
    student_profile = db.relationship(
        "StudentProfile", back_populates="user", uselist=False,
        foreign_keys="StudentProfile.user_id")

    def to_dict(self, exclude=()):
        """Every column of this row; enums as their value."""
        exclude = set(exclude) | {'password_hash', 'password_history'}  # never returned
        data = {}
        for column in self.__table__.columns:
            if column.name not in exclude:
                value = getattr(self, column.name)
                data[column.name] = value.value if isinstance(value, enum.Enum) else value
        return data


class UserSession(db.Model):
    """One row per login. The row's ``uuid`` is the ``sid`` claim in the JWT.

    ACTIVE = session usable. Ending a session sets INACTIVE, so ``deleted_at``
    holds the moment it ended.
    """
    __tablename__ = "user_sessions"

    id = db.Column(db.BigInteger, primary_key=True)
    uuid = db.Column(db.String(36), unique=True, nullable=False, index=True, default=lambda: str(uuid4()))
    status = db.Column(db.Enum(RecordStatus, name="record_status"), nullable=False, default=RecordStatus.ACTIVE,
                       server_default=RecordStatus.ACTIVE.value, index=True)
    deleted_at = db.Column(db.DateTime(timezone=True))
    created_at = db.Column(db.DateTime(timezone=True), nullable=False, default=lambda: datetime.now(timezone.utc),
                           server_default=db.func.now())
    updated_at = db.Column(db.DateTime(timezone=True), nullable=False, default=lambda: datetime.now(timezone.utc),
                           onupdate=lambda: datetime.now(timezone.utc), server_default=db.func.now())
    created_by_id = db.Column(db.BigInteger, db.ForeignKey("users.id", use_alter=True, ondelete="SET NULL"))
    updated_by_id = db.Column(db.BigInteger, db.ForeignKey("users.id", use_alter=True, ondelete="SET NULL"))

    user_id = db.Column(db.BigInteger, db.ForeignKey("users.id", ondelete="CASCADE"),
                        nullable=False, index=True)
    login_at = db.Column(db.DateTime(timezone=True), nullable=False)
    logout_at = db.Column(db.DateTime(timezone=True))
    last_seen_at = db.Column(db.DateTime(timezone=True))
    ip_address = db.Column(db.String(100))
    user_agent = db.Column(db.String(500))
    two_factor_passed = db.Column(db.Boolean, nullable=False, default=False)
    end_reason = db.Column(db.Enum(SessionEndReason, name="session_end_reason"), nullable=True)

    user = db.relationship("User", foreign_keys=[user_id])

    def to_dict(self, exclude=()):
        """Every column of this row; enums as their value."""
        data = {}
        for column in self.__table__.columns:
            if column.name not in exclude:
                value = getattr(self, column.name)
                data[column.name] = value.value if isinstance(value, enum.Enum) else value
        return data


class OtpCode(db.Model):
    __tablename__ = "otp_codes"

    id = db.Column(db.BigInteger, primary_key=True)
    uuid = db.Column(db.String(36), unique=True, nullable=False, index=True, default=lambda: str(uuid4()))
    status = db.Column(db.Enum(RecordStatus, name="record_status"), nullable=False, default=RecordStatus.ACTIVE,
                       server_default=RecordStatus.ACTIVE.value, index=True)
    deleted_at = db.Column(db.DateTime(timezone=True))
    created_at = db.Column(db.DateTime(timezone=True), nullable=False, default=lambda: datetime.now(timezone.utc),
                           server_default=db.func.now())
    updated_at = db.Column(db.DateTime(timezone=True), nullable=False, default=lambda: datetime.now(timezone.utc),
                           onupdate=lambda: datetime.now(timezone.utc), server_default=db.func.now())
    created_by_id = db.Column(db.BigInteger, db.ForeignKey("users.id", use_alter=True, ondelete="SET NULL"))
    updated_by_id = db.Column(db.BigInteger, db.ForeignKey("users.id", use_alter=True, ondelete="SET NULL"))

    user_id = db.Column(db.BigInteger, db.ForeignKey("users.id", ondelete="CASCADE"),
                        nullable=False, index=True)
    purpose = db.Column(db.Enum(OtpPurpose, name="otp_purpose"), nullable=False)
    target = db.Column(db.String(255), nullable=False)  # email or mobile it was sent to
    code_hash = db.Column(db.String(128), nullable=False)
    expires_at = db.Column(db.DateTime(timezone=True), nullable=False)
    attempts = db.Column(db.Integer, nullable=False, default=0)
    consumed_at = db.Column(db.DateTime(timezone=True))

    def to_dict(self, exclude=()):
        """Every column of this row; enums as their value."""
        exclude = set(exclude) | {'code_hash'}  # never returned
        data = {}
        for column in self.__table__.columns:
            if column.name not in exclude:
                value = getattr(self, column.name)
                data[column.name] = value.value if isinstance(value, enum.Enum) else value
        return data


class OneTimeToken(db.Model):
    __tablename__ = "one_time_tokens"

    id = db.Column(db.BigInteger, primary_key=True)
    uuid = db.Column(db.String(36), unique=True, nullable=False, index=True, default=lambda: str(uuid4()))
    status = db.Column(db.Enum(RecordStatus, name="record_status"), nullable=False, default=RecordStatus.ACTIVE,
                       server_default=RecordStatus.ACTIVE.value, index=True)
    deleted_at = db.Column(db.DateTime(timezone=True))
    created_at = db.Column(db.DateTime(timezone=True), nullable=False, default=lambda: datetime.now(timezone.utc),
                           server_default=db.func.now())
    updated_at = db.Column(db.DateTime(timezone=True), nullable=False, default=lambda: datetime.now(timezone.utc),
                           onupdate=lambda: datetime.now(timezone.utc), server_default=db.func.now())
    created_by_id = db.Column(db.BigInteger, db.ForeignKey("users.id", use_alter=True, ondelete="SET NULL"))
    updated_by_id = db.Column(db.BigInteger, db.ForeignKey("users.id", use_alter=True, ondelete="SET NULL"))

    user_id = db.Column(db.BigInteger, db.ForeignKey("users.id", ondelete="CASCADE"),
                        nullable=False, index=True)
    purpose = db.Column(db.Enum(TokenPurpose, name="token_purpose"), nullable=False)
    token_hash = db.Column(db.String(128), nullable=False, unique=True)
    expires_at = db.Column(db.DateTime(timezone=True), nullable=False)
    used_at = db.Column(db.DateTime(timezone=True))

    def to_dict(self, exclude=()):
        """Every column of this row; enums as their value."""
        exclude = set(exclude) | {'token_hash'}  # never returned
        data = {}
        for column in self.__table__.columns:
            if column.name not in exclude:
                value = getattr(self, column.name)
                data[column.name] = value.value if isinstance(value, enum.Enum) else value
        return data


class IpBlock(db.Model):
    __tablename__ = "ip_blocks"

    id = db.Column(db.BigInteger, primary_key=True)
    uuid = db.Column(db.String(36), unique=True, nullable=False, index=True, default=lambda: str(uuid4()))
    status = db.Column(db.Enum(RecordStatus, name="record_status"), nullable=False, default=RecordStatus.ACTIVE,
                       server_default=RecordStatus.ACTIVE.value, index=True)
    deleted_at = db.Column(db.DateTime(timezone=True))
    created_at = db.Column(db.DateTime(timezone=True), nullable=False, default=lambda: datetime.now(timezone.utc),
                           server_default=db.func.now())
    updated_at = db.Column(db.DateTime(timezone=True), nullable=False, default=lambda: datetime.now(timezone.utc),
                           onupdate=lambda: datetime.now(timezone.utc), server_default=db.func.now())
    created_by_id = db.Column(db.BigInteger, db.ForeignKey("users.id", use_alter=True, ondelete="SET NULL"))
    updated_by_id = db.Column(db.BigInteger, db.ForeignKey("users.id", use_alter=True, ondelete="SET NULL"))

    ip_address = db.Column(db.String(100), nullable=False, index=True)
    blocked_until = db.Column(db.DateTime(timezone=True), nullable=False)
    reason = db.Column(db.String(255))

    def to_dict(self, exclude=()):
        """Every column of this row; enums as their value."""
        data = {}
        for column in self.__table__.columns:
            if column.name not in exclude:
                value = getattr(self, column.name)
                data[column.name] = value.value if isinstance(value, enum.Enum) else value
        return data


# ======================================================================
# Geography, universities, colleges and onboarding
#
# State, District, University and College are the models from
# load_universities.py (the AISHE loader). The loader fills these tables;
# University and College also carry the columns the app needs, and every one
# of those has a server default or is nullable so the loader can insert rows.
# ======================================================================

class State(db.Model):
    __tablename__ = "states"

    id = db.Column(db.Integer, primary_key=True, autoincrement=True)
    name = db.Column(db.String(100), unique=True, nullable=False)

    def to_dict(self, exclude=()):
        return {c.name: getattr(self, c.name) for c in self.__table__.columns if c.name not in exclude}


class District(db.Model):
    __tablename__ = "districts"
    __table_args__ = (db.UniqueConstraint("state_id", "name", name="uq_district_state_name"),)

    id = db.Column(db.Integer, primary_key=True, autoincrement=True)
    name = db.Column(db.String(100), nullable=False)
    state_id = db.Column(db.Integer, db.ForeignKey("states.id", ondelete="CASCADE"), nullable=False, index=True)

    state = db.relationship("State")

    def to_dict(self, exclude=()):
        return {c.name: getattr(self, c.name) for c in self.__table__.columns if c.name not in exclude}


class Pincode(db.Model):
    __tablename__ = "pincodes"

    id = db.Column(db.BigInteger, primary_key=True)
    uuid = db.Column(db.String(36), unique=True, nullable=False, index=True, default=lambda: str(uuid4()))
    status = db.Column(db.Enum(RecordStatus, name="record_status"), nullable=False, default=RecordStatus.ACTIVE,
                       server_default=RecordStatus.ACTIVE.value, index=True)
    deleted_at = db.Column(db.DateTime(timezone=True))
    created_at = db.Column(db.DateTime(timezone=True), nullable=False, default=lambda: datetime.now(timezone.utc),
                           server_default=db.func.now())
    updated_at = db.Column(db.DateTime(timezone=True), nullable=False, default=lambda: datetime.now(timezone.utc),
                           onupdate=lambda: datetime.now(timezone.utc), server_default=db.func.now())
    created_by_id = db.Column(db.BigInteger, db.ForeignKey("users.id", use_alter=True, ondelete="SET NULL"))
    updated_by_id = db.Column(db.BigInteger, db.ForeignKey("users.id", use_alter=True, ondelete="SET NULL"))

    pincode = db.Column(db.String(6), nullable=False, index=True)
    locality = db.Column(db.String(200))
    # SET NULL so the AISHE loader can remove a state / district nobody uses any more
    district_id = db.Column(db.Integer, db.ForeignKey("districts.id", ondelete="SET NULL"), index=True)
    state_id = db.Column(db.Integer, db.ForeignKey("states.id", ondelete="SET NULL"), index=True)

    district = db.relationship("District")
    state = db.relationship("State")

    def to_dict(self, exclude=()):
        """Every column of this row; enums as their value."""
        data = {}
        for column in self.__table__.columns:
            if column.name not in exclude:
                value = getattr(self, column.name)
                data[column.name] = value.value if isinstance(value, enum.Enum) else value
        return data


class University(db.Model):
    __tablename__ = "universities"

    # ---- columns from load_universities.py
    id = db.Column(db.Integer, primary_key=True, autoincrement=True)
    aishe_code = db.Column(db.String(20), unique=True, nullable=False, index=True)
    name = db.Column(db.String(500), nullable=False, index=True)
    state_id = db.Column(db.Integer, db.ForeignKey("states.id", ondelete="RESTRICT"), nullable=False, index=True)
    district_id = db.Column(db.Integer, db.ForeignKey("districts.id", ondelete="RESTRICT"), nullable=True, index=True)
    website = db.Column(db.String(500), nullable=True)
    year_of_establishment = db.Column(db.Integer, nullable=True)
    location = db.Column(db.Enum(LocationType, name="location_type",
                                 values_callable=lambda e: [m.value for m in e]), nullable=True)

    # ---- app columns (server defaults, so the loader's inserts work)
    uuid = db.Column(db.String(36), unique=True, nullable=False, index=True, default=lambda: str(uuid4()),
                     server_default=db.text("gen_random_uuid()::text"))
    status = db.Column(db.Enum(RecordStatus, name="record_status"), nullable=False, default=RecordStatus.ACTIVE,
                       server_default=RecordStatus.ACTIVE.value, index=True)
    deleted_at = db.Column(db.DateTime(timezone=True))
    created_at = db.Column(db.DateTime(timezone=True), nullable=False, default=lambda: datetime.now(timezone.utc),
                           server_default=db.func.now())
    updated_at = db.Column(db.DateTime(timezone=True), nullable=False, default=lambda: datetime.now(timezone.utc),
                           onupdate=lambda: datetime.now(timezone.utc), server_default=db.func.now())
    created_by_id = db.Column(db.BigInteger, db.ForeignKey("users.id", use_alter=True, ondelete="SET NULL"))
    updated_by_id = db.Column(db.BigInteger, db.ForeignKey("users.id", use_alter=True, ondelete="SET NULL"))
    address_line = db.Column(db.String(500))
    city = db.Column(db.String(150))
    pincode = db.Column(db.String(6), index=True)
    pincode_verified = db.Column(db.Boolean, nullable=False, default=True, server_default=db.true())
    email_domains = db.Column(db.JSON, nullable=False, default=list, server_default=db.text("'[]'"))
    phone = db.Column(db.String(30))
    onboarding_status = db.Column(db.Enum(OnboardingStatus, name="onboarding_status"), nullable=False,
                                  default=OnboardingStatus.MASTER, server_default=OnboardingStatus.MASTER.value,
                                  index=True)
    source = db.Column(db.Enum(InstitutionSource, name="institution_source"), nullable=False,
                       default=InstitutionSource.AISHE_IMPORT, server_default=InstitutionSource.AISHE_IMPORT.value)
    onboarded_at = db.Column(db.DateTime(timezone=True))
    suspended_reason = db.Column(db.String(500))
    # 12.1: unfilled reserved seats go to OPEN at window close
    release_unfilled_reserved_to_open = db.Column(db.Boolean, nullable=False, default=False,
                                                  server_default=db.false())
    # letter branding (12.2)
    letter_header = db.Column(db.Text)
    letter_footer = db.Column(db.Text)
    seal_document_id = db.Column(db.BigInteger, db.ForeignKey("documents.id", use_alter=True))

    state = db.relationship("State")
    district = db.relationship("District")

    def to_dict(self, exclude=()):
        """Every column of this row; enums as their value."""
        data = {}
        for column in self.__table__.columns:
            if column.name not in exclude:
                value = getattr(self, column.name)
                data[column.name] = value.value if isinstance(value, enum.Enum) else value
        return data


class College(db.Model):
    """Colleges, standalone institutions, PM Vidyalaxmi institutes and R&D institutes."""

    __tablename__ = "colleges"
    __table_args__ = (
        db.UniqueConstraint("institution_type", "aishe_code", name="uq_college_type_aishe_code"),
    )

    # ---- columns from load_universities.py
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

    # ---- app columns (server defaults, so the loader's inserts work)
    uuid = db.Column(db.String(36), unique=True, nullable=False, index=True, default=lambda: str(uuid4()),
                     server_default=db.text("gen_random_uuid()::text"))
    status = db.Column(db.Enum(RecordStatus, name="record_status"), nullable=False, default=RecordStatus.ACTIVE,
                       server_default=RecordStatus.ACTIVE.value, index=True)
    deleted_at = db.Column(db.DateTime(timezone=True))
    created_at = db.Column(db.DateTime(timezone=True), nullable=False, default=lambda: datetime.now(timezone.utc),
                           server_default=db.func.now())
    updated_at = db.Column(db.DateTime(timezone=True), nullable=False, default=lambda: datetime.now(timezone.utc),
                           onupdate=lambda: datetime.now(timezone.utc), server_default=db.func.now())
    created_by_id = db.Column(db.BigInteger, db.ForeignKey("users.id", use_alter=True, ondelete="SET NULL"))
    updated_by_id = db.Column(db.BigInteger, db.ForeignKey("users.id", use_alter=True, ondelete="SET NULL"))
    address_line = db.Column(db.String(500))
    city = db.Column(db.String(150))
    pincode = db.Column(db.String(6), index=True)
    pincode_verified = db.Column(db.Boolean, nullable=False, default=True, server_default=db.true())
    email_domains = db.Column(db.JSON, nullable=False, default=list, server_default=db.text("'[]'"))
    phone = db.Column(db.String(30))
    onboarding_status = db.Column(db.Enum(OnboardingStatus, name="onboarding_status"), nullable=False,
                                  default=OnboardingStatus.MASTER, server_default=OnboardingStatus.MASTER.value,
                                  index=True)
    source = db.Column(db.Enum(InstitutionSource, name="institution_source"), nullable=False,
                       default=InstitutionSource.AISHE_IMPORT, server_default=InstitutionSource.AISHE_IMPORT.value)
    onboarded_at = db.Column(db.DateTime(timezone=True))
    suspended_reason = db.Column(db.String(500))
    # letter branding (12.2)
    letter_header = db.Column(db.Text)
    letter_footer = db.Column(db.Text)
    seal_document_id = db.Column(db.BigInteger, db.ForeignKey("documents.id", use_alter=True))

    state = db.relationship("State")
    district = db.relationship("District")
    university = db.relationship("University", backref="colleges")

    def to_dict(self, exclude=()):
        """Every column of this row; enums as their value."""
        data = {}
        for column in self.__table__.columns:
            if column.name not in exclude:
                value = getattr(self, column.name)
                data[column.name] = value.value if isinstance(value, enum.Enum) else value
        return data


class InstitutionMembership(db.Model):
    """Links staff users to an institution with a role and permissions."""
    __tablename__ = "institution_memberships"
    __table_args__ = (
        db.UniqueConstraint("user_id", "university_id", name="uq_membership_user_university"),
        db.UniqueConstraint("user_id", "college_id", name="uq_membership_user_college"),
        db.CheckConstraint("(university_id IS NULL) <> (college_id IS NULL)", name="membership_one_institution"),
    )

    id = db.Column(db.BigInteger, primary_key=True)
    uuid = db.Column(db.String(36), unique=True, nullable=False, index=True, default=lambda: str(uuid4()))
    status = db.Column(db.Enum(RecordStatus, name="record_status"), nullable=False, default=RecordStatus.ACTIVE,
                       server_default=RecordStatus.ACTIVE.value, index=True)
    deleted_at = db.Column(db.DateTime(timezone=True))
    created_at = db.Column(db.DateTime(timezone=True), nullable=False, default=lambda: datetime.now(timezone.utc),
                           server_default=db.func.now())
    updated_at = db.Column(db.DateTime(timezone=True), nullable=False, default=lambda: datetime.now(timezone.utc),
                           onupdate=lambda: datetime.now(timezone.utc), server_default=db.func.now())
    created_by_id = db.Column(db.BigInteger, db.ForeignKey("users.id", use_alter=True, ondelete="SET NULL"))
    updated_by_id = db.Column(db.BigInteger, db.ForeignKey("users.id", use_alter=True, ondelete="SET NULL"))

    user_id = db.Column(db.BigInteger, db.ForeignKey("users.id", ondelete="CASCADE"),
                        nullable=False, index=True)
    # exactly one of university_id / college_id is set
    university_id = db.Column(db.Integer, db.ForeignKey("universities.id"), index=True)
    college_id = db.Column(db.Integer, db.ForeignKey("colleges.id"), index=True)
    role = db.Column(db.Enum(MembershipRole, name="membership_role"), nullable=False)
    is_primary = db.Column(db.Boolean, nullable=False, default=False)
    bundles = db.Column(db.JSON, nullable=False, default=list)
    permissions = db.Column(db.JSON, nullable=False, default=list)
    approval_status = db.Column(db.Enum(ApprovalStatus, name="approval_status"), nullable=False,
                                default=ApprovalStatus.PENDING, index=True)
    approved_by_id = db.Column(db.BigInteger, db.ForeignKey("users.id"))
    approved_at = db.Column(db.DateTime(timezone=True))
    rejection_reason = db.Column(db.String(500))
    joined_via = db.Column(db.String(30))  # INVITATION / ACCESS_REQUEST / REGISTRATION / DIRECT

    user = db.relationship("User", back_populates="memberships", foreign_keys=[user_id])
    university = db.relationship("University", lazy="joined")
    college = db.relationship("College", lazy="joined")

    def to_dict(self, exclude=()):
        """Every column of this row; enums as their value."""
        data = {}
        for column in self.__table__.columns:
            if column.name not in exclude:
                value = getattr(self, column.name)
                data[column.name] = value.value if isinstance(value, enum.Enum) else value
        return data


class Invitation(db.Model):
    __tablename__ = "invitations"

    id = db.Column(db.BigInteger, primary_key=True)
    uuid = db.Column(db.String(36), unique=True, nullable=False, index=True, default=lambda: str(uuid4()))
    status = db.Column(db.Enum(RecordStatus, name="record_status"), nullable=False, default=RecordStatus.ACTIVE,
                       server_default=RecordStatus.ACTIVE.value, index=True)
    deleted_at = db.Column(db.DateTime(timezone=True))
    created_at = db.Column(db.DateTime(timezone=True), nullable=False, default=lambda: datetime.now(timezone.utc),
                           server_default=db.func.now())
    updated_at = db.Column(db.DateTime(timezone=True), nullable=False, default=lambda: datetime.now(timezone.utc),
                           onupdate=lambda: datetime.now(timezone.utc), server_default=db.func.now())
    created_by_id = db.Column(db.BigInteger, db.ForeignKey("users.id", use_alter=True, ondelete="SET NULL"))
    updated_by_id = db.Column(db.BigInteger, db.ForeignKey("users.id", use_alter=True, ondelete="SET NULL"))

    purpose = db.Column(db.Enum(InvitationPurpose, name="invitation_purpose"), nullable=False)
    email = db.Column(db.String(255), nullable=False, index=True)
    full_name = db.Column(db.String(200))
    # the institution invited to (at most one of university_id / college_id)
    university_id = db.Column(db.Integer, db.ForeignKey("universities.id"), index=True)
    college_id = db.Column(db.Integer, db.ForeignKey("colleges.id"), index=True)
    parent_university_id = db.Column(db.Integer, db.ForeignKey("universities.id"))  # university inviting a new college
    aishe_code = db.Column(db.String(20))  # bound code for signup links (path B)
    institution_kind = db.Column(db.Enum(InstitutionKind, name="institution_kind"), nullable=True)
    bundles = db.Column(db.JSON, nullable=False, default=list)
    permissions = db.Column(db.JSON, nullable=False, default=list)
    pre_approved = db.Column(db.Boolean, nullable=False, default=False)  # path A
    token_hash = db.Column(db.String(128), nullable=False, unique=True)
    expires_at = db.Column(db.DateTime(timezone=True), nullable=False)
    invitation_state = db.Column(db.Enum(InvitationState, name="invitation_state"), nullable=False,
                                 default=InvitationState.SENT)
    accepted_at = db.Column(db.DateTime(timezone=True))
    accepted_by_id = db.Column(db.BigInteger, db.ForeignKey("users.id"))
    invited_by_id = db.Column(db.BigInteger, db.ForeignKey("users.id"))
    note = db.Column(db.String(500))

    university = db.relationship("University", foreign_keys=[university_id])
    college = db.relationship("College", foreign_keys=[college_id])

    def to_dict(self, exclude=()):
        """Every column of this row; enums as their value."""
        exclude = set(exclude) | {'token_hash'}  # never returned
        data = {}
        for column in self.__table__.columns:
            if column.name not in exclude:
                value = getattr(self, column.name)
                data[column.name] = value.value if isinstance(value, enum.Enum) else value
        return data


class RegistrationRequest(db.Model):
    """An institution admin's registration awaiting approval."""
    __tablename__ = "registration_requests"

    id = db.Column(db.BigInteger, primary_key=True)
    uuid = db.Column(db.String(36), unique=True, nullable=False, index=True, default=lambda: str(uuid4()))
    status = db.Column(db.Enum(RecordStatus, name="record_status"), nullable=False, default=RecordStatus.ACTIVE,
                       server_default=RecordStatus.ACTIVE.value, index=True)
    deleted_at = db.Column(db.DateTime(timezone=True))
    created_at = db.Column(db.DateTime(timezone=True), nullable=False, default=lambda: datetime.now(timezone.utc),
                           server_default=db.func.now())
    updated_at = db.Column(db.DateTime(timezone=True), nullable=False, default=lambda: datetime.now(timezone.utc),
                           onupdate=lambda: datetime.now(timezone.utc), server_default=db.func.now())
    created_by_id = db.Column(db.BigInteger, db.ForeignKey("users.id", use_alter=True, ondelete="SET NULL"))
    updated_by_id = db.Column(db.BigInteger, db.ForeignKey("users.id", use_alter=True, ondelete="SET NULL"))

    institution_kind = db.Column(db.Enum(InstitutionKind, name="institution_kind"), nullable=False)
    path = db.Column(db.Enum(RegistrationPath, name="registration_path"), nullable=False)
    # the institution (at most one set; neither for a manual institution not yet created)
    university_id = db.Column(db.Integer, db.ForeignKey("universities.id"), index=True)
    college_id = db.Column(db.Integer, db.ForeignKey("colleges.id"), index=True)
    requester_id = db.Column(db.BigInteger, db.ForeignKey("users.id"), nullable=False, index=True)
    invitation_id = db.Column(db.BigInteger, db.ForeignKey("invitations.id"))
    approver_university_id = db.Column(db.Integer, db.ForeignKey("universities.id"))  # null = platform admin approves
    designation = db.Column(db.String(150))
    official_email = db.Column(db.String(255))
    authorization_letter_id = db.Column(db.BigInteger, db.ForeignKey("documents.id", use_alter=True))
    manual_details = db.Column(db.JSON)
    is_manual_institution = db.Column(db.Boolean, nullable=False, default=False)
    is_second_claim = db.Column(db.Boolean, nullable=False, default=False)
    approval_status = db.Column(db.Enum(ApprovalStatus, name="approval_status"), nullable=False,
                                default=ApprovalStatus.PENDING, index=True)
    reviewer_id = db.Column(db.BigInteger, db.ForeignKey("users.id"))
    reviewed_at = db.Column(db.DateTime(timezone=True))
    review_note = db.Column(db.String(1000))

    university = db.relationship("University", foreign_keys=[university_id])
    college = db.relationship("College", foreign_keys=[college_id])
    requester = db.relationship("User", foreign_keys=[requester_id])

    def to_dict(self, exclude=()):
        """Every column of this row; enums as their value."""
        data = {}
        for column in self.__table__.columns:
            if column.name not in exclude:
                value = getattr(self, column.name)
                data[column.name] = value.value if isinstance(value, enum.Enum) else value
        return data


class MasterImportBatch(db.Model):
    __tablename__ = "master_import_batches"

    id = db.Column(db.BigInteger, primary_key=True)
    uuid = db.Column(db.String(36), unique=True, nullable=False, index=True, default=lambda: str(uuid4()))
    status = db.Column(db.Enum(RecordStatus, name="record_status"), nullable=False, default=RecordStatus.ACTIVE,
                       server_default=RecordStatus.ACTIVE.value, index=True)
    deleted_at = db.Column(db.DateTime(timezone=True))
    created_at = db.Column(db.DateTime(timezone=True), nullable=False, default=lambda: datetime.now(timezone.utc),
                           server_default=db.func.now())
    updated_at = db.Column(db.DateTime(timezone=True), nullable=False, default=lambda: datetime.now(timezone.utc),
                           onupdate=lambda: datetime.now(timezone.utc), server_default=db.func.now())
    created_by_id = db.Column(db.BigInteger, db.ForeignKey("users.id", use_alter=True, ondelete="SET NULL"))
    updated_by_id = db.Column(db.BigInteger, db.ForeignKey("users.id", use_alter=True, ondelete="SET NULL"))

    dataset = db.Column(db.Enum(ImportDataset, name="import_dataset"), nullable=False)
    filename = db.Column(db.String(300))
    total_rows = db.Column(db.Integer, nullable=False, default=0)
    imported_rows = db.Column(db.Integer, nullable=False, default=0)
    updated_rows = db.Column(db.Integer, nullable=False, default=0)
    duplicate_rows = db.Column(db.Integer, nullable=False, default=0)
    unmatched_rows = db.Column(db.Integer, nullable=False, default=0)
    invalid_rows = db.Column(db.Integer, nullable=False, default=0)
    exceptions = db.Column(db.JSON, nullable=False, default=list)
    finished_at = db.Column(db.DateTime(timezone=True))

    def to_dict(self, exclude=()):
        """Every column of this row; enums as their value."""
        data = {}
        for column in self.__table__.columns:
            if column.name not in exclude:
                value = getattr(self, column.name)
                data[column.name] = value.value if isinstance(value, enum.Enum) else value
        return data


class CollegeOnboardingRequest(db.Model):
    """A student asking for their non-onboarded college to be brought in (4.4)."""
    __tablename__ = "college_onboarding_requests"

    id = db.Column(db.BigInteger, primary_key=True)
    uuid = db.Column(db.String(36), unique=True, nullable=False, index=True, default=lambda: str(uuid4()))
    status = db.Column(db.Enum(RecordStatus, name="record_status"), nullable=False, default=RecordStatus.ACTIVE,
                       server_default=RecordStatus.ACTIVE.value, index=True)
    deleted_at = db.Column(db.DateTime(timezone=True))
    created_at = db.Column(db.DateTime(timezone=True), nullable=False, default=lambda: datetime.now(timezone.utc),
                           server_default=db.func.now())
    updated_at = db.Column(db.DateTime(timezone=True), nullable=False, default=lambda: datetime.now(timezone.utc),
                           onupdate=lambda: datetime.now(timezone.utc), server_default=db.func.now())
    created_by_id = db.Column(db.BigInteger, db.ForeignKey("users.id", use_alter=True, ondelete="SET NULL"))
    updated_by_id = db.Column(db.BigInteger, db.ForeignKey("users.id", use_alter=True, ondelete="SET NULL"))

    student_user_id = db.Column(db.BigInteger, db.ForeignKey("users.id"), nullable=False, index=True)
    college_id = db.Column(db.Integer, db.ForeignKey("colleges.id"), nullable=False, index=True)
    contact_name = db.Column(db.String(200))
    contact_email = db.Column(db.String(255), nullable=False)
    note = db.Column(db.String(1000))
    approval_status = db.Column(db.Enum(ApprovalStatus, name="approval_status"), nullable=False,
                                default=ApprovalStatus.PENDING)
    reviewed_by_id = db.Column(db.BigInteger, db.ForeignKey("users.id"))
    reviewed_at = db.Column(db.DateTime(timezone=True))

    college = db.relationship("College")

    def to_dict(self, exclude=()):
        """Every column of this row; enums as their value."""
        data = {}
        for column in self.__table__.columns:
            if column.name not in exclude:
                value = getattr(self, column.name)
                data[column.name] = value.value if isinstance(value, enum.Enum) else value
        return data


# ======================================================================
# Documents
# ======================================================================

class Document(db.Model):
    """Every uploaded or generated file. Re-uploading creates a new version row
    in the same ``group_uuid``; only one row per group has ``is_current``."""
    __tablename__ = "documents"

    id = db.Column(db.BigInteger, primary_key=True)
    uuid = db.Column(db.String(36), unique=True, nullable=False, index=True, default=lambda: str(uuid4()))
    status = db.Column(db.Enum(RecordStatus, name="record_status"), nullable=False, default=RecordStatus.ACTIVE,
                       server_default=RecordStatus.ACTIVE.value, index=True)
    deleted_at = db.Column(db.DateTime(timezone=True))
    created_at = db.Column(db.DateTime(timezone=True), nullable=False, default=lambda: datetime.now(timezone.utc),
                           server_default=db.func.now())
    updated_at = db.Column(db.DateTime(timezone=True), nullable=False, default=lambda: datetime.now(timezone.utc),
                           onupdate=lambda: datetime.now(timezone.utc), server_default=db.func.now())
    created_by_id = db.Column(db.BigInteger, db.ForeignKey("users.id", use_alter=True, ondelete="SET NULL"))
    updated_by_id = db.Column(db.BigInteger, db.ForeignKey("users.id", use_alter=True, ondelete="SET NULL"))

    group_uuid = db.Column(db.String(36), nullable=False, index=True)
    owner_type = db.Column(db.Enum(DocumentOwnerType, name="document_owner_type"), nullable=False)
    owner_id = db.Column(db.BigInteger, nullable=False, index=True)
    doc_type = db.Column(db.Enum(DocumentType, name="document_type"), nullable=False)
    version_no = db.Column(db.Integer, nullable=False, default=1)
    is_current = db.Column(db.Boolean, nullable=False, default=True, index=True)
    storage_key = db.Column(db.String(500), nullable=False)
    original_filename = db.Column(db.String(300), nullable=False)
    mime_type = db.Column(db.String(150), nullable=False)
    size_bytes = db.Column(db.BigInteger, nullable=False)
    checksum_sha256 = db.Column(db.String(64), nullable=False)
    verification_status = db.Column(db.Enum(VerificationStatus, name="verification_status"), nullable=False,
                                    default=VerificationStatus.PENDING)
    verified_by_id = db.Column(db.BigInteger, db.ForeignKey("users.id"))
    verified_at = db.Column(db.DateTime(timezone=True))
    rejection_reason = db.Column(db.String(500))
    uploaded_by_university_id = db.Column(db.Integer, db.ForeignKey("universities.id"))
    uploaded_by_college_id = db.Column(db.Integer, db.ForeignKey("colleges.id"))
    description = db.Column(db.String(300))

    def to_dict(self, exclude=()):
        """Every column of this row; enums as their value."""
        data = {}
        for column in self.__table__.columns:
            if column.name not in exclude:
                value = getattr(self, column.name)
                data[column.name] = value.value if isinstance(value, enum.Enum) else value
        return data


# ======================================================================
# Courses, syllabi, rules, windows and vacancies
# ======================================================================

class AcademicYear(db.Model):
    __tablename__ = "academic_years"
    __table_args__ = (
        db.UniqueConstraint("university_id", "label", name="uq_academic_years_university_label"),
        db.CheckConstraint("end_date > start_date", name="dates_ordered"),
    )

    id = db.Column(db.BigInteger, primary_key=True)
    uuid = db.Column(db.String(36), unique=True, nullable=False, index=True, default=lambda: str(uuid4()))
    status = db.Column(db.Enum(RecordStatus, name="record_status"), nullable=False, default=RecordStatus.ACTIVE,
                       server_default=RecordStatus.ACTIVE.value, index=True)
    deleted_at = db.Column(db.DateTime(timezone=True))
    created_at = db.Column(db.DateTime(timezone=True), nullable=False, default=lambda: datetime.now(timezone.utc),
                           server_default=db.func.now())
    updated_at = db.Column(db.DateTime(timezone=True), nullable=False, default=lambda: datetime.now(timezone.utc),
                           onupdate=lambda: datetime.now(timezone.utc), server_default=db.func.now())
    created_by_id = db.Column(db.BigInteger, db.ForeignKey("users.id", use_alter=True, ondelete="SET NULL"))
    updated_by_id = db.Column(db.BigInteger, db.ForeignKey("users.id", use_alter=True, ondelete="SET NULL"))

    university_id = db.Column(db.Integer, db.ForeignKey("universities.id"), index=True)  # null = platform calendar
    label = db.Column(db.String(20), nullable=False)  # e.g. 2026-27
    start_date = db.Column(db.Date, nullable=False)
    end_date = db.Column(db.Date, nullable=False)
    is_current = db.Column(db.Boolean, nullable=False, default=False)

    university = db.relationship("University")

    def to_dict(self, exclude=()):
        """Every column of this row; enums as their value."""
        data = {}
        for column in self.__table__.columns:
            if column.name not in exclude:
                value = getattr(self, column.name)
                data[column.name] = value.value if isinstance(value, enum.Enum) else value
        return data


class Course(db.Model):
    __tablename__ = "courses"
    __table_args__ = (db.UniqueConstraint("university_id", "code", name="uq_courses_university_code"),)

    id = db.Column(db.BigInteger, primary_key=True)
    uuid = db.Column(db.String(36), unique=True, nullable=False, index=True, default=lambda: str(uuid4()))
    status = db.Column(db.Enum(RecordStatus, name="record_status"), nullable=False, default=RecordStatus.ACTIVE,
                       server_default=RecordStatus.ACTIVE.value, index=True)
    deleted_at = db.Column(db.DateTime(timezone=True))
    created_at = db.Column(db.DateTime(timezone=True), nullable=False, default=lambda: datetime.now(timezone.utc),
                           server_default=db.func.now())
    updated_at = db.Column(db.DateTime(timezone=True), nullable=False, default=lambda: datetime.now(timezone.utc),
                           onupdate=lambda: datetime.now(timezone.utc), server_default=db.func.now())
    created_by_id = db.Column(db.BigInteger, db.ForeignKey("users.id", use_alter=True, ondelete="SET NULL"))
    updated_by_id = db.Column(db.BigInteger, db.ForeignKey("users.id", use_alter=True, ondelete="SET NULL"))

    university_id = db.Column(db.Integer, db.ForeignKey("universities.id"), nullable=False, index=True)
    code = db.Column(db.String(30), nullable=False)
    name = db.Column(db.String(300), nullable=False)
    normalized_name = db.Column(db.String(300), nullable=False, index=True)
    level = db.Column(db.Enum(CourseLevel, name="course_level"), nullable=False)
    discipline = db.Column(db.String(150))
    duration_years = db.Column(db.Integer, nullable=False)
    total_semesters = db.Column(db.Integer, nullable=False)
    offer_mode = db.Column(db.Enum(OfferMode, name="offer_mode"), nullable=False, default=OfferMode.MERIT_ROUND)
    interview_required = db.Column(db.Boolean, nullable=False, default=False)

    university = db.relationship("University")

    def to_dict(self, exclude=()):
        """Every column of this row; enums as their value."""
        data = {}
        for column in self.__table__.columns:
            if column.name not in exclude:
                value = getattr(self, column.name)
                data[column.name] = value.value if isinstance(value, enum.Enum) else value
        return data


class Syllabus(db.Model):
    __tablename__ = "syllabi"
    __table_args__ = (db.UniqueConstraint("university_id", "name", "version_no",
                                          name="uq_syllabi_university_name_version"),)

    id = db.Column(db.BigInteger, primary_key=True)
    uuid = db.Column(db.String(36), unique=True, nullable=False, index=True, default=lambda: str(uuid4()))
    status = db.Column(db.Enum(RecordStatus, name="record_status"), nullable=False, default=RecordStatus.ACTIVE,
                       server_default=RecordStatus.ACTIVE.value, index=True)
    deleted_at = db.Column(db.DateTime(timezone=True))
    created_at = db.Column(db.DateTime(timezone=True), nullable=False, default=lambda: datetime.now(timezone.utc),
                           server_default=db.func.now())
    updated_at = db.Column(db.DateTime(timezone=True), nullable=False, default=lambda: datetime.now(timezone.utc),
                           onupdate=lambda: datetime.now(timezone.utc), server_default=db.func.now())
    created_by_id = db.Column(db.BigInteger, db.ForeignKey("users.id", use_alter=True, ondelete="SET NULL"))
    updated_by_id = db.Column(db.BigInteger, db.ForeignKey("users.id", use_alter=True, ondelete="SET NULL"))

    university_id = db.Column(db.Integer, db.ForeignKey("universities.id"), nullable=False, index=True)
    name = db.Column(db.String(300), nullable=False)
    regulation_year = db.Column(db.Integer, nullable=False)  # e.g. 2023 pattern
    version_no = db.Column(db.Integer, nullable=False, default=1)
    is_published = db.Column(db.Boolean, nullable=False, default=False)
    published_at = db.Column(db.DateTime(timezone=True))

    subjects = db.relationship("SyllabusSubject", back_populates="syllabus",
                               order_by="(SyllabusSubject.semester, SyllabusSubject.code)")

    university = db.relationship("University")

    def to_dict(self, exclude=()):
        """Every column of this row; enums as their value."""
        data = {}
        for column in self.__table__.columns:
            if column.name not in exclude:
                value = getattr(self, column.name)
                data[column.name] = value.value if isinstance(value, enum.Enum) else value
        return data


class SyllabusSubject(db.Model):
    __tablename__ = "syllabus_subjects"
    __table_args__ = (db.UniqueConstraint("syllabus_id", "code", name="uq_syllabus_subjects_syllabus_code"),)

    id = db.Column(db.BigInteger, primary_key=True)
    uuid = db.Column(db.String(36), unique=True, nullable=False, index=True, default=lambda: str(uuid4()))
    status = db.Column(db.Enum(RecordStatus, name="record_status"), nullable=False, default=RecordStatus.ACTIVE,
                       server_default=RecordStatus.ACTIVE.value, index=True)
    deleted_at = db.Column(db.DateTime(timezone=True))
    created_at = db.Column(db.DateTime(timezone=True), nullable=False, default=lambda: datetime.now(timezone.utc),
                           server_default=db.func.now())
    updated_at = db.Column(db.DateTime(timezone=True), nullable=False, default=lambda: datetime.now(timezone.utc),
                           onupdate=lambda: datetime.now(timezone.utc), server_default=db.func.now())
    created_by_id = db.Column(db.BigInteger, db.ForeignKey("users.id", use_alter=True, ondelete="SET NULL"))
    updated_by_id = db.Column(db.BigInteger, db.ForeignKey("users.id", use_alter=True, ondelete="SET NULL"))

    syllabus_id = db.Column(db.BigInteger, db.ForeignKey("syllabi.id", ondelete="CASCADE"),
                            nullable=False, index=True)
    semester = db.Column(db.Integer, nullable=False)
    code = db.Column(db.String(30), nullable=False)
    name = db.Column(db.String(300), nullable=False)
    credits = db.Column(db.Numeric(5, 2), nullable=False)
    is_elective = db.Column(db.Boolean, nullable=False, default=False)

    syllabus = db.relationship("Syllabus", back_populates="subjects")

    def to_dict(self, exclude=()):
        """Every column of this row; enums as their value."""
        data = {}
        for column in self.__table__.columns:
            if column.name not in exclude:
                value = getattr(self, column.name)
                data[column.name] = value.value if isinstance(value, enum.Enum) else value
        return data


class CourseSyllabus(db.Model):
    """Which syllabus version applies to a course for a batch range (5)."""
    __tablename__ = "course_syllabi"

    id = db.Column(db.BigInteger, primary_key=True)
    uuid = db.Column(db.String(36), unique=True, nullable=False, index=True, default=lambda: str(uuid4()))
    status = db.Column(db.Enum(RecordStatus, name="record_status"), nullable=False, default=RecordStatus.ACTIVE,
                       server_default=RecordStatus.ACTIVE.value, index=True)
    deleted_at = db.Column(db.DateTime(timezone=True))
    created_at = db.Column(db.DateTime(timezone=True), nullable=False, default=lambda: datetime.now(timezone.utc),
                           server_default=db.func.now())
    updated_at = db.Column(db.DateTime(timezone=True), nullable=False, default=lambda: datetime.now(timezone.utc),
                           onupdate=lambda: datetime.now(timezone.utc), server_default=db.func.now())
    created_by_id = db.Column(db.BigInteger, db.ForeignKey("users.id", use_alter=True, ondelete="SET NULL"))
    updated_by_id = db.Column(db.BigInteger, db.ForeignKey("users.id", use_alter=True, ondelete="SET NULL"))

    course_id = db.Column(db.BigInteger, db.ForeignKey("courses.id"), nullable=False, index=True)
    syllabus_id = db.Column(db.BigInteger, db.ForeignKey("syllabi.id"), nullable=False, index=True)
    batch_from = db.Column(db.Integer, nullable=False)
    batch_to = db.Column(db.Integer)  # null = open ended

    course = db.relationship("Course")
    syllabus = db.relationship("Syllabus")

    def to_dict(self, exclude=()):
        """Every column of this row; enums as their value."""
        data = {}
        for column in self.__table__.columns:
            if column.name not in exclude:
                value = getattr(self, column.name)
                data[column.name] = value.value if isinstance(value, enum.Enum) else value
        return data


class CourseEquivalence(db.Model):
    """Source courses a destination course accepts (12.1)."""
    __tablename__ = "course_equivalences"

    id = db.Column(db.BigInteger, primary_key=True)
    uuid = db.Column(db.String(36), unique=True, nullable=False, index=True, default=lambda: str(uuid4()))
    status = db.Column(db.Enum(RecordStatus, name="record_status"), nullable=False, default=RecordStatus.ACTIVE,
                       server_default=RecordStatus.ACTIVE.value, index=True)
    deleted_at = db.Column(db.DateTime(timezone=True))
    created_at = db.Column(db.DateTime(timezone=True), nullable=False, default=lambda: datetime.now(timezone.utc),
                           server_default=db.func.now())
    updated_at = db.Column(db.DateTime(timezone=True), nullable=False, default=lambda: datetime.now(timezone.utc),
                           onupdate=lambda: datetime.now(timezone.utc), server_default=db.func.now())
    created_by_id = db.Column(db.BigInteger, db.ForeignKey("users.id", use_alter=True, ondelete="SET NULL"))
    updated_by_id = db.Column(db.BigInteger, db.ForeignKey("users.id", use_alter=True, ondelete="SET NULL"))

    course_id = db.Column(db.BigInteger, db.ForeignKey("courses.id"), nullable=False, index=True)
    source_university_id = db.Column(db.Integer, db.ForeignKey("universities.id"))  # null = any
    source_course_name = db.Column(db.String(300), nullable=False)
    normalized_source_name = db.Column(db.String(300), nullable=False, index=True)
    source_course_level = db.Column(db.Enum(CourseLevel, name="course_level"), nullable=True)
    note = db.Column(db.String(500))

    def to_dict(self, exclude=()):
        """Every column of this row; enums as their value."""
        data = {}
        for column in self.__table__.columns:
            if column.name not in exclude:
                value = getattr(self, column.name)
                data[column.name] = value.value if isinstance(value, enum.Enum) else value
        return data


class OfferingRequest(db.Model):
    __tablename__ = "offering_requests"

    id = db.Column(db.BigInteger, primary_key=True)
    uuid = db.Column(db.String(36), unique=True, nullable=False, index=True, default=lambda: str(uuid4()))
    status = db.Column(db.Enum(RecordStatus, name="record_status"), nullable=False, default=RecordStatus.ACTIVE,
                       server_default=RecordStatus.ACTIVE.value, index=True)
    deleted_at = db.Column(db.DateTime(timezone=True))
    created_at = db.Column(db.DateTime(timezone=True), nullable=False, default=lambda: datetime.now(timezone.utc),
                           server_default=db.func.now())
    updated_at = db.Column(db.DateTime(timezone=True), nullable=False, default=lambda: datetime.now(timezone.utc),
                           onupdate=lambda: datetime.now(timezone.utc), server_default=db.func.now())
    created_by_id = db.Column(db.BigInteger, db.ForeignKey("users.id", use_alter=True, ondelete="SET NULL"))
    updated_by_id = db.Column(db.BigInteger, db.ForeignKey("users.id", use_alter=True, ondelete="SET NULL"))

    college_id = db.Column(db.Integer, db.ForeignKey("colleges.id"), nullable=False, index=True)
    course_id = db.Column(db.BigInteger, db.ForeignKey("courses.id"), nullable=False, index=True)
    proposed_intake = db.Column(db.Integer, nullable=False)
    justification = db.Column(db.String(1000))
    approval_status = db.Column(db.Enum(ApprovalStatus, name="approval_status"), nullable=False,
                                default=ApprovalStatus.PENDING, index=True)
    decided_by_id = db.Column(db.BigInteger, db.ForeignKey("users.id"))
    decided_at = db.Column(db.DateTime(timezone=True))
    decision_note = db.Column(db.String(1000))

    college = db.relationship("College")
    course = db.relationship("Course")

    def to_dict(self, exclude=()):
        """Every column of this row; enums as their value."""
        data = {}
        for column in self.__table__.columns:
            if column.name not in exclude:
                value = getattr(self, column.name)
                data[column.name] = value.value if isinstance(value, enum.Enum) else value
        return data


class CourseOffering(db.Model):
    __tablename__ = "course_offerings"
    __table_args__ = (db.UniqueConstraint("college_id", "course_id", name="uq_offerings_college_course"),
                      db.CheckConstraint("sanctioned_intake >= 0", name="intake_non_negative"))

    id = db.Column(db.BigInteger, primary_key=True)
    uuid = db.Column(db.String(36), unique=True, nullable=False, index=True, default=lambda: str(uuid4()))
    status = db.Column(db.Enum(RecordStatus, name="record_status"), nullable=False, default=RecordStatus.ACTIVE,
                       server_default=RecordStatus.ACTIVE.value, index=True)
    deleted_at = db.Column(db.DateTime(timezone=True))
    created_at = db.Column(db.DateTime(timezone=True), nullable=False, default=lambda: datetime.now(timezone.utc),
                           server_default=db.func.now())
    updated_at = db.Column(db.DateTime(timezone=True), nullable=False, default=lambda: datetime.now(timezone.utc),
                           onupdate=lambda: datetime.now(timezone.utc), server_default=db.func.now())
    created_by_id = db.Column(db.BigInteger, db.ForeignKey("users.id", use_alter=True, ondelete="SET NULL"))
    updated_by_id = db.Column(db.BigInteger, db.ForeignKey("users.id", use_alter=True, ondelete="SET NULL"))

    college_id = db.Column(db.Integer, db.ForeignKey("colleges.id"), nullable=False, index=True)
    course_id = db.Column(db.BigInteger, db.ForeignKey("courses.id"), nullable=False, index=True)
    sanctioned_intake = db.Column(db.Integer, nullable=False)
    assigned_directly = db.Column(db.Boolean, nullable=False, default=False)
    offering_request_id = db.Column(db.BigInteger, db.ForeignKey("offering_requests.id"))

    college = db.relationship("College", lazy="joined")
    course = db.relationship("Course", lazy="joined")

    def to_dict(self, exclude=()):
        """Every column of this row; enums as their value."""
        data = {}
        for column in self.__table__.columns:
            if column.name not in exclude:
                value = getattr(self, column.name)
                data[column.name] = value.value if isinstance(value, enum.Enum) else value
        return data


class MigrationWindow(db.Model):
    __tablename__ = "migration_windows"
    __table_args__ = (db.CheckConstraint("closes_on >= opens_on", name="window_dates_ordered"),)

    id = db.Column(db.BigInteger, primary_key=True)
    uuid = db.Column(db.String(36), unique=True, nullable=False, index=True, default=lambda: str(uuid4()))
    status = db.Column(db.Enum(RecordStatus, name="record_status"), nullable=False, default=RecordStatus.ACTIVE,
                       server_default=RecordStatus.ACTIVE.value, index=True)
    deleted_at = db.Column(db.DateTime(timezone=True))
    created_at = db.Column(db.DateTime(timezone=True), nullable=False, default=lambda: datetime.now(timezone.utc),
                           server_default=db.func.now())
    updated_at = db.Column(db.DateTime(timezone=True), nullable=False, default=lambda: datetime.now(timezone.utc),
                           onupdate=lambda: datetime.now(timezone.utc), server_default=db.func.now())
    created_by_id = db.Column(db.BigInteger, db.ForeignKey("users.id", use_alter=True, ondelete="SET NULL"))
    updated_by_id = db.Column(db.BigInteger, db.ForeignKey("users.id", use_alter=True, ondelete="SET NULL"))

    course_id = db.Column(db.BigInteger, db.ForeignKey("courses.id"), nullable=False, index=True)
    academic_year_id = db.Column(db.BigInteger, db.ForeignKey("academic_years.id"), nullable=False, index=True)
    opens_on = db.Column(db.Date, nullable=False)
    closes_on = db.Column(db.Date, nullable=False)
    entry_semesters = db.Column(db.JSON, nullable=False, default=list)
    merit_round_completed_at = db.Column(db.DateTime(timezone=True))
    reserved_released_at = db.Column(db.DateTime(timezone=True))

    course = db.relationship("Course")
    academic_year = db.relationship("AcademicYear")

    def to_dict(self, exclude=()):
        """Every column of this row; enums as their value."""
        data = {}
        for column in self.__table__.columns:
            if column.name not in exclude:
                value = getattr(self, column.name)
                data[column.name] = value.value if isinstance(value, enum.Enum) else value
        return data


class EligibilityRule(db.Model):
    __tablename__ = "eligibility_rules"

    id = db.Column(db.BigInteger, primary_key=True)
    uuid = db.Column(db.String(36), unique=True, nullable=False, index=True, default=lambda: str(uuid4()))
    status = db.Column(db.Enum(RecordStatus, name="record_status"), nullable=False, default=RecordStatus.ACTIVE,
                       server_default=RecordStatus.ACTIVE.value, index=True)
    deleted_at = db.Column(db.DateTime(timezone=True))
    created_at = db.Column(db.DateTime(timezone=True), nullable=False, default=lambda: datetime.now(timezone.utc),
                           server_default=db.func.now())
    updated_at = db.Column(db.DateTime(timezone=True), nullable=False, default=lambda: datetime.now(timezone.utc),
                           onupdate=lambda: datetime.now(timezone.utc), server_default=db.func.now())
    created_by_id = db.Column(db.BigInteger, db.ForeignKey("users.id", use_alter=True, ondelete="SET NULL"))
    updated_by_id = db.Column(db.BigInteger, db.ForeignKey("users.id", use_alter=True, ondelete="SET NULL"))

    course_id = db.Column(db.BigInteger, db.ForeignKey("courses.id"), nullable=False, index=True)
    version_no = db.Column(db.Integer, nullable=False, default=1)
    effective_from = db.Column(db.Date, nullable=False)
    min_percentage = db.Column(db.Numeric(5, 2))
    min_completed_semesters = db.Column(db.Integer)
    max_active_backlogs = db.Column(db.Integer)
    same_discipline_required = db.Column(db.Boolean, nullable=False, default=False)
    min_credits_by_entry_semester = db.Column(db.JSON, nullable=False, default=dict)  # {"3": 40}
    is_statutory = db.Column(db.Boolean, nullable=False, default=False)
    notes = db.Column(db.String(1000))

    def to_dict(self, exclude=()):
        """Every column of this row; enums as their value."""
        data = {}
        for column in self.__table__.columns:
            if column.name not in exclude:
                value = getattr(self, column.name)
                data[column.name] = value.value if isinstance(value, enum.Enum) else value
        return data


class GradingScheme(db.Model):
    __tablename__ = "grading_schemes"

    id = db.Column(db.BigInteger, primary_key=True)
    uuid = db.Column(db.String(36), unique=True, nullable=False, index=True, default=lambda: str(uuid4()))
    status = db.Column(db.Enum(RecordStatus, name="record_status"), nullable=False, default=RecordStatus.ACTIVE,
                       server_default=RecordStatus.ACTIVE.value, index=True)
    deleted_at = db.Column(db.DateTime(timezone=True))
    created_at = db.Column(db.DateTime(timezone=True), nullable=False, default=lambda: datetime.now(timezone.utc),
                           server_default=db.func.now())
    updated_at = db.Column(db.DateTime(timezone=True), nullable=False, default=lambda: datetime.now(timezone.utc),
                           onupdate=lambda: datetime.now(timezone.utc), server_default=db.func.now())
    created_by_id = db.Column(db.BigInteger, db.ForeignKey("users.id", use_alter=True, ondelete="SET NULL"))
    updated_by_id = db.Column(db.BigInteger, db.ForeignKey("users.id", use_alter=True, ondelete="SET NULL"))

    university_id = db.Column(db.Integer, db.ForeignKey("universities.id"), nullable=False, index=True)
    grade_system = db.Column(db.Enum(GradeSystem, name="grade_system"), nullable=False)
    formula_expr = db.Column(db.String(300))  # e.g. "cgpa * 9.5", variable: cgpa
    batch_from = db.Column(db.Integer, nullable=False)
    batch_to = db.Column(db.Integer)
    circular_document_id = db.Column(db.BigInteger, db.ForeignKey("documents.id", use_alter=True))
    source_url = db.Column(db.String(500))

    university = db.relationship("University")

    def to_dict(self, exclude=()):
        """Every column of this row; enums as their value."""
        data = {}
        for column in self.__table__.columns:
            if column.name not in exclude:
                value = getattr(self, column.name)
                data[column.name] = value.value if isinstance(value, enum.Enum) else value
        return data


class Vacancy(db.Model):
    __tablename__ = "vacancies"
    __table_args__ = (db.UniqueConstraint("offering_id", "academic_year_id", "entry_semester",
                                          name="uq_vacancies_offering_year_semester"),)

    id = db.Column(db.BigInteger, primary_key=True)
    uuid = db.Column(db.String(36), unique=True, nullable=False, index=True, default=lambda: str(uuid4()))
    status = db.Column(db.Enum(RecordStatus, name="record_status"), nullable=False, default=RecordStatus.ACTIVE,
                       server_default=RecordStatus.ACTIVE.value, index=True)
    deleted_at = db.Column(db.DateTime(timezone=True))
    created_at = db.Column(db.DateTime(timezone=True), nullable=False, default=lambda: datetime.now(timezone.utc),
                           server_default=db.func.now())
    updated_at = db.Column(db.DateTime(timezone=True), nullable=False, default=lambda: datetime.now(timezone.utc),
                           onupdate=lambda: datetime.now(timezone.utc), server_default=db.func.now())
    created_by_id = db.Column(db.BigInteger, db.ForeignKey("users.id", use_alter=True, ondelete="SET NULL"))
    updated_by_id = db.Column(db.BigInteger, db.ForeignKey("users.id", use_alter=True, ondelete="SET NULL"))

    offering_id = db.Column(db.BigInteger, db.ForeignKey("course_offerings.id"), nullable=False, index=True)
    academic_year_id = db.Column(db.BigInteger, db.ForeignKey("academic_years.id"), nullable=False, index=True)
    entry_semester = db.Column(db.Integer, nullable=False)
    discrepancy_flag = db.Column(db.Boolean, nullable=False, default=False)

    offering = db.relationship("CourseOffering")
    academic_year = db.relationship("AcademicYear")
    categories = db.relationship("VacancyCategory", back_populates="vacancy", lazy="selectin")

    def to_dict(self, exclude=()):
        """Every column of this row; enums as their value."""
        data = {}
        for column in self.__table__.columns:
            if column.name not in exclude:
                value = getattr(self, column.name)
                data[column.name] = value.value if isinstance(value, enum.Enum) else value
        return data


class VacancyCategory(db.Model):
    """Seat counts per reservation category (12.1). Unsplit vacancies use OPEN."""
    __tablename__ = "vacancy_categories"
    __table_args__ = (
        db.UniqueConstraint("vacancy_id", "category", name="uq_vacancy_categories_vacancy_category"),
        db.CheckConstraint("seats >= 0 AND blocked >= 0 AND filled >= 0", name="counts_non_negative"),
    )

    id = db.Column(db.BigInteger, primary_key=True)
    uuid = db.Column(db.String(36), unique=True, nullable=False, index=True, default=lambda: str(uuid4()))
    status = db.Column(db.Enum(RecordStatus, name="record_status"), nullable=False, default=RecordStatus.ACTIVE,
                       server_default=RecordStatus.ACTIVE.value, index=True)
    deleted_at = db.Column(db.DateTime(timezone=True))
    created_at = db.Column(db.DateTime(timezone=True), nullable=False, default=lambda: datetime.now(timezone.utc),
                           server_default=db.func.now())
    updated_at = db.Column(db.DateTime(timezone=True), nullable=False, default=lambda: datetime.now(timezone.utc),
                           onupdate=lambda: datetime.now(timezone.utc), server_default=db.func.now())
    created_by_id = db.Column(db.BigInteger, db.ForeignKey("users.id", use_alter=True, ondelete="SET NULL"))
    updated_by_id = db.Column(db.BigInteger, db.ForeignKey("users.id", use_alter=True, ondelete="SET NULL"))

    vacancy_id = db.Column(db.BigInteger, db.ForeignKey("vacancies.id", ondelete="CASCADE"),
                           nullable=False, index=True)
    category = db.Column(db.Enum(ReservationCategory, name="reservation_category"), nullable=False)
    seats = db.Column(db.Integer, nullable=False, default=0)
    blocked = db.Column(db.Integer, nullable=False, default=0)
    filled = db.Column(db.Integer, nullable=False, default=0)

    vacancy = db.relationship("Vacancy", back_populates="categories")

    def to_dict(self, exclude=()):
        """Every column of this row; enums as their value."""
        data = {}
        for column in self.__table__.columns:
            if column.name not in exclude:
                value = getattr(self, column.name)
                data[column.name] = value.value if isinstance(value, enum.Enum) else value
        return data


class InterviewWorkingHours(db.Model):
    __tablename__ = "interview_working_hours"
    __table_args__ = (db.CheckConstraint("end_time > start_time", name="hours_ordered"),)

    id = db.Column(db.BigInteger, primary_key=True)
    uuid = db.Column(db.String(36), unique=True, nullable=False, index=True, default=lambda: str(uuid4()))
    status = db.Column(db.Enum(RecordStatus, name="record_status"), nullable=False, default=RecordStatus.ACTIVE,
                       server_default=RecordStatus.ACTIVE.value, index=True)
    deleted_at = db.Column(db.DateTime(timezone=True))
    created_at = db.Column(db.DateTime(timezone=True), nullable=False, default=lambda: datetime.now(timezone.utc),
                           server_default=db.func.now())
    updated_at = db.Column(db.DateTime(timezone=True), nullable=False, default=lambda: datetime.now(timezone.utc),
                           onupdate=lambda: datetime.now(timezone.utc), server_default=db.func.now())
    created_by_id = db.Column(db.BigInteger, db.ForeignKey("users.id", use_alter=True, ondelete="SET NULL"))
    updated_by_id = db.Column(db.BigInteger, db.ForeignKey("users.id", use_alter=True, ondelete="SET NULL"))

    college_id = db.Column(db.Integer, db.ForeignKey("colleges.id"), nullable=False, index=True)
    weekday = db.Column(db.Enum(Weekday, name="weekday"), nullable=False)
    start_time = db.Column(db.Time, nullable=False)
    end_time = db.Column(db.Time, nullable=False)
    slot_minutes = db.Column(db.Integer, nullable=False, default=30)

    college = db.relationship("College")

    def to_dict(self, exclude=()):
        """Every column of this row; enums as their value."""
        data = {}
        for column in self.__table__.columns:
            if column.name not in exclude:
                value = getattr(self, column.name)
                data[column.name] = value.value if isinstance(value, enum.Enum) else value
        return data


# ======================================================================
# Students and academic records
# ======================================================================

class StudentProfile(db.Model):
    __tablename__ = "student_profiles"
    __table_args__ = (db.UniqueConstraint("current_college_id", "enrolment_number",
                                          name="uq_student_enrolment_per_college"),)

    id = db.Column(db.BigInteger, primary_key=True)
    uuid = db.Column(db.String(36), unique=True, nullable=False, index=True, default=lambda: str(uuid4()))
    status = db.Column(db.Enum(RecordStatus, name="record_status"), nullable=False, default=RecordStatus.ACTIVE,
                       server_default=RecordStatus.ACTIVE.value, index=True)
    deleted_at = db.Column(db.DateTime(timezone=True))
    created_at = db.Column(db.DateTime(timezone=True), nullable=False, default=lambda: datetime.now(timezone.utc),
                           server_default=db.func.now())
    updated_at = db.Column(db.DateTime(timezone=True), nullable=False, default=lambda: datetime.now(timezone.utc),
                           onupdate=lambda: datetime.now(timezone.utc), server_default=db.func.now())
    created_by_id = db.Column(db.BigInteger, db.ForeignKey("users.id", use_alter=True, ondelete="SET NULL"))
    updated_by_id = db.Column(db.BigInteger, db.ForeignKey("users.id", use_alter=True, ondelete="SET NULL"))

    user_id = db.Column(db.BigInteger, db.ForeignKey("users.id", ondelete="CASCADE"),
                        nullable=False, unique=True)
    first_name = db.Column(db.String(100), nullable=False)
    last_name = db.Column(db.String(100))
    date_of_birth = db.Column(db.Date)
    gender = db.Column(db.Enum(Gender, name="gender"), nullable=False, default=Gender.UNDISCLOSED)
    abc_account_id = db.Column(db.String(12), unique=True, index=True)  # ABC / APAAR, 12 digits
    category = db.Column(db.Enum(ReservationCategory, name="reservation_category"), nullable=False,
                         default=ReservationCategory.OPEN)
    category_verified = db.Column(db.Boolean, nullable=False, default=False)
    guardian_name = db.Column(db.String(200))
    guardian_email = db.Column(db.String(255))
    guardian_mobile = db.Column(db.String(15))
    parental_consent_at = db.Column(db.DateTime(timezone=True))
    address_line = db.Column(db.String(500))
    state_id = db.Column(db.Integer, db.ForeignKey("states.id", ondelete="SET NULL"))
    district_id = db.Column(db.Integer, db.ForeignKey("districts.id", ondelete="SET NULL"))
    pincode = db.Column(db.String(6))

    # current enrolment
    current_college_id = db.Column(db.Integer, db.ForeignKey("colleges.id"), index=True)
    enrolment_number = db.Column(db.String(50))
    current_course_name = db.Column(db.String(300))
    current_course_level = db.Column(db.Enum(CourseLevel, name="course_level"), nullable=True)
    current_discipline = db.Column(db.String(150))
    current_semester = db.Column(db.Integer)
    batch_year = db.Column(db.Integer)  # year of admission
    enrolment_status = db.Column(db.Enum(EnrolmentStatus, name="enrolment_status"), nullable=False,
                                 default=EnrolmentStatus.NOT_SUBMITTED, index=True)
    enrolment_proof_document_id = db.Column(db.BigInteger, db.ForeignKey("documents.id", use_alter=True))
    enrolment_verified_by_id = db.Column(db.BigInteger, db.ForeignKey("users.id"))
    enrolment_verified_at = db.Column(db.DateTime(timezone=True))
    enrolment_rejection_reason = db.Column(db.String(500))

    user = db.relationship("User", back_populates="student_profile", foreign_keys=[user_id])
    current_college = db.relationship("College", foreign_keys=[current_college_id])
    academic_records = db.relationship("AcademicRecord", back_populates="student",
                                       order_by="AcademicRecord.semester")

    def to_dict(self, exclude=()):
        """Every column of this row; enums as their value."""
        data = {}
        for column in self.__table__.columns:
            if column.name not in exclude:
                value = getattr(self, column.name)
                data[column.name] = value.value if isinstance(value, enum.Enum) else value
        return data


class AcademicRecord(db.Model):
    """One semester result. A change after verification creates a new version (M08-F05)."""
    __tablename__ = "academic_records"

    id = db.Column(db.BigInteger, primary_key=True)
    uuid = db.Column(db.String(36), unique=True, nullable=False, index=True, default=lambda: str(uuid4()))
    status = db.Column(db.Enum(RecordStatus, name="record_status"), nullable=False, default=RecordStatus.ACTIVE,
                       server_default=RecordStatus.ACTIVE.value, index=True)
    deleted_at = db.Column(db.DateTime(timezone=True))
    created_at = db.Column(db.DateTime(timezone=True), nullable=False, default=lambda: datetime.now(timezone.utc),
                           server_default=db.func.now())
    updated_at = db.Column(db.DateTime(timezone=True), nullable=False, default=lambda: datetime.now(timezone.utc),
                           onupdate=lambda: datetime.now(timezone.utc), server_default=db.func.now())
    created_by_id = db.Column(db.BigInteger, db.ForeignKey("users.id", use_alter=True, ondelete="SET NULL"))
    updated_by_id = db.Column(db.BigInteger, db.ForeignKey("users.id", use_alter=True, ondelete="SET NULL"))

    student_id = db.Column(db.BigInteger, db.ForeignKey("student_profiles.id", ondelete="CASCADE"),
                           nullable=False, index=True)
    semester = db.Column(db.Integer, nullable=False)
    grade_system = db.Column(db.Enum(GradeSystem, name="grade_system"), nullable=False)
    sgpa = db.Column(db.Numeric(4, 2))
    cgpa = db.Column(db.Numeric(4, 2))
    percentage = db.Column(db.Numeric(5, 2))
    credits_earned = db.Column(db.Numeric(6, 2))
    active_backlogs = db.Column(db.Integer, nullable=False, default=0)
    version_no = db.Column(db.Integer, nullable=False, default=1)
    is_current = db.Column(db.Boolean, nullable=False, default=True, index=True)
    verification_status = db.Column(db.Enum(VerificationStatus, name="verification_status"), nullable=False,
                                    default=VerificationStatus.PENDING)
    verified_by_id = db.Column(db.BigInteger, db.ForeignKey("users.id"))
    verified_at = db.Column(db.DateTime(timezone=True))
    marksheet_document_id = db.Column(db.BigInteger, db.ForeignKey("documents.id", use_alter=True))

    student = db.relationship("StudentProfile", back_populates="academic_records")
    subjects = db.relationship("SubjectResult", back_populates="record", lazy="selectin")

    def to_dict(self, exclude=()):
        """Every column of this row; enums as their value."""
        data = {}
        for column in self.__table__.columns:
            if column.name not in exclude:
                value = getattr(self, column.name)
                data[column.name] = value.value if isinstance(value, enum.Enum) else value
        return data


class SubjectResult(db.Model):
    __tablename__ = "subject_results"

    id = db.Column(db.BigInteger, primary_key=True)
    uuid = db.Column(db.String(36), unique=True, nullable=False, index=True, default=lambda: str(uuid4()))
    status = db.Column(db.Enum(RecordStatus, name="record_status"), nullable=False, default=RecordStatus.ACTIVE,
                       server_default=RecordStatus.ACTIVE.value, index=True)
    deleted_at = db.Column(db.DateTime(timezone=True))
    created_at = db.Column(db.DateTime(timezone=True), nullable=False, default=lambda: datetime.now(timezone.utc),
                           server_default=db.func.now())
    updated_at = db.Column(db.DateTime(timezone=True), nullable=False, default=lambda: datetime.now(timezone.utc),
                           onupdate=lambda: datetime.now(timezone.utc), server_default=db.func.now())
    created_by_id = db.Column(db.BigInteger, db.ForeignKey("users.id", use_alter=True, ondelete="SET NULL"))
    updated_by_id = db.Column(db.BigInteger, db.ForeignKey("users.id", use_alter=True, ondelete="SET NULL"))

    academic_record_id = db.Column(db.BigInteger, db.ForeignKey("academic_records.id", ondelete="CASCADE"),
                                   nullable=False, index=True)
    subject_code = db.Column(db.String(30), nullable=False)
    subject_name = db.Column(db.String(300), nullable=False)
    credits = db.Column(db.Numeric(5, 2), nullable=False)
    grade = db.Column(db.String(10))
    marks_obtained = db.Column(db.Numeric(6, 2))
    max_marks = db.Column(db.Numeric(6, 2))
    passed = db.Column(db.Boolean, nullable=False, default=True)

    record = db.relationship("AcademicRecord", back_populates="subjects")

    def to_dict(self, exclude=()):
        """Every column of this row; enums as their value."""
        data = {}
        for column in self.__table__.columns:
            if column.name not in exclude:
                value = getattr(self, column.name)
                data[column.name] = value.value if isinstance(value, enum.Enum) else value
        return data


class ConsentRecord(db.Model):
    """DPDP consent (11.1). Withdrawal sets the row INACTIVE."""
    __tablename__ = "consent_records"

    id = db.Column(db.BigInteger, primary_key=True)
    uuid = db.Column(db.String(36), unique=True, nullable=False, index=True, default=lambda: str(uuid4()))
    status = db.Column(db.Enum(RecordStatus, name="record_status"), nullable=False, default=RecordStatus.ACTIVE,
                       server_default=RecordStatus.ACTIVE.value, index=True)
    deleted_at = db.Column(db.DateTime(timezone=True))
    created_at = db.Column(db.DateTime(timezone=True), nullable=False, default=lambda: datetime.now(timezone.utc),
                           server_default=db.func.now())
    updated_at = db.Column(db.DateTime(timezone=True), nullable=False, default=lambda: datetime.now(timezone.utc),
                           onupdate=lambda: datetime.now(timezone.utc), server_default=db.func.now())
    created_by_id = db.Column(db.BigInteger, db.ForeignKey("users.id", use_alter=True, ondelete="SET NULL"))
    updated_by_id = db.Column(db.BigInteger, db.ForeignKey("users.id", use_alter=True, ondelete="SET NULL"))

    user_id = db.Column(db.BigInteger, db.ForeignKey("users.id", ondelete="CASCADE"),
                        nullable=False, index=True)
    notice_version = db.Column(db.String(30), nullable=False)
    consented_at = db.Column(db.DateTime(timezone=True), nullable=False)
    withdrawn_at = db.Column(db.DateTime(timezone=True))
    whatsapp_opt_in = db.Column(db.Boolean, nullable=False, default=False)
    is_parental = db.Column(db.Boolean, nullable=False, default=False)
    parent_identifier = db.Column(db.String(255))
    ip_address = db.Column(db.String(100))

    def to_dict(self, exclude=()):
        """Every column of this row; enums as their value."""
        data = {}
        for column in self.__table__.columns:
            if column.name not in exclude:
                value = getattr(self, column.name)
                data[column.name] = value.value if isinstance(value, enum.Enum) else value
        return data


# ======================================================================
# Migration requests
# ======================================================================

class MigrationRequest(db.Model):
    """One student, one destination offering, one entry semester, one year (6)."""
    __tablename__ = "migration_requests"

    id = db.Column(db.BigInteger, primary_key=True)
    uuid = db.Column(db.String(36), unique=True, nullable=False, index=True, default=lambda: str(uuid4()))
    status = db.Column(db.Enum(RecordStatus, name="record_status"), nullable=False, default=RecordStatus.ACTIVE,
                       server_default=RecordStatus.ACTIVE.value, index=True)
    deleted_at = db.Column(db.DateTime(timezone=True))
    created_at = db.Column(db.DateTime(timezone=True), nullable=False, default=lambda: datetime.now(timezone.utc),
                           server_default=db.func.now())
    updated_at = db.Column(db.DateTime(timezone=True), nullable=False, default=lambda: datetime.now(timezone.utc),
                           onupdate=lambda: datetime.now(timezone.utc), server_default=db.func.now())
    created_by_id = db.Column(db.BigInteger, db.ForeignKey("users.id", use_alter=True, ondelete="SET NULL"))
    updated_by_id = db.Column(db.BigInteger, db.ForeignKey("users.id", use_alter=True, ondelete="SET NULL"))

    reference_no = db.Column(db.String(40), nullable=False, unique=True, index=True)
    student_id = db.Column(db.BigInteger, db.ForeignKey("student_profiles.id"), nullable=False, index=True)
    source_college_id = db.Column(db.Integer, db.ForeignKey("colleges.id"), nullable=False, index=True)
    source_university_id = db.Column(db.Integer, db.ForeignKey("universities.id"), index=True)  # null for a college without a university
    destination_offering_id = db.Column(db.BigInteger, db.ForeignKey("course_offerings.id"), nullable=False,
                                        index=True)
    destination_college_id = db.Column(db.Integer, db.ForeignKey("colleges.id"), nullable=False, index=True)
    destination_university_id = db.Column(db.Integer, db.ForeignKey("universities.id"), index=True)
    academic_year_id = db.Column(db.BigInteger, db.ForeignKey("academic_years.id"), nullable=False, index=True)
    window_id = db.Column(db.BigInteger, db.ForeignKey("migration_windows.id"), index=True)
    entry_semester = db.Column(db.Integer, nullable=False)
    migration_type = db.Column(db.Enum(MigrationType, name="migration_type"), nullable=False)
    request_status = db.Column(db.Enum(MigrationStatus, name="migration_status"), nullable=False,
                               default=MigrationStatus.DRAFT, index=True)
    version = db.Column(db.Integer, nullable=False, default=1)  # optimistic lock (INT-13)
    category = db.Column(db.Enum(ReservationCategory, name="reservation_category"), nullable=False,
                         default=ReservationCategory.OPEN)

    eligibility_result = db.Column(db.Enum(EligibilityResult, name="eligibility_result"), nullable=False,
                                   default=EligibilityResult.PENDING)
    eligibility_notes = db.Column(db.JSON, nullable=False, default=list)
    eligibility_rule_id = db.Column(db.BigInteger, db.ForeignKey("eligibility_rules.id"))
    merit_score = db.Column(db.Numeric(6, 2))
    waitlist_rank = db.Column(db.Integer)
    interview_required = db.Column(db.Boolean, nullable=False, default=False)
    no_show_count = db.Column(db.Integer, nullable=False, default=0)
    student_reschedules = db.Column(db.Integer, nullable=False, default=0)
    correction_note = db.Column(db.Text)

    submitted_at = db.Column(db.DateTime(timezone=True), index=True)
    noc_due_at = db.Column(db.DateTime(timezone=True))
    noc_escalated_at = db.Column(db.DateTime(timezone=True))
    noc_decided_at = db.Column(db.DateTime(timezone=True))
    noc_refusal_reason = db.Column(db.Enum(NocRefusalReason, name="noc_refusal_reason"), nullable=True)
    recommended_at = db.Column(db.DateTime(timezone=True))
    entry_approved_at = db.Column(db.DateTime(timezone=True), index=True)
    offer_issued_at = db.Column(db.DateTime(timezone=True))
    offer_expires_at = db.Column(db.DateTime(timezone=True), index=True)
    offer_reminder_sent_at = db.Column(db.DateTime(timezone=True))
    accepted_at = db.Column(db.DateTime(timezone=True))
    release_due_at = db.Column(db.DateTime(timezone=True), index=True)
    release_reminders_sent = db.Column(db.JSON, nullable=False, default=list)
    release_escalated_at = db.Column(db.DateTime(timezone=True))
    tc_document_id = db.Column(db.BigInteger, db.ForeignKey("documents.id", use_alter=True))
    tc_uploaded_at = db.Column(db.DateTime(timezone=True))
    migration_certificate_document_id = db.Column(db.BigInteger, db.ForeignKey("documents.id", use_alter=True))
    source_university_released_at = db.Column(db.DateTime(timezone=True))
    released_at = db.Column(db.DateTime(timezone=True))
    admitted_at = db.Column(db.DateTime(timezone=True))
    originals_due_at = db.Column(db.DateTime(timezone=True), index=True)
    originals_verified_at = db.Column(db.DateTime(timezone=True))
    fee_paid = db.Column(db.Boolean, nullable=False, default=False)

    vacancy_category_id = db.Column(db.BigInteger, db.ForeignKey("vacancy_categories.id"))
    seat_state = db.Column(db.Enum(SeatState, name="seat_state"), nullable=True)

    rejection_reason = db.Column(db.Text)
    rejected_at_status = db.Column(db.Enum(MigrationStatus, name="migration_status"), nullable=True)
    rejected_at = db.Column(db.DateTime(timezone=True))
    cancellation_stage = db.Column(db.Enum(CancellationStage, name="cancellation_stage"), nullable=True)
    cancellation_reason = db.Column(db.Text)
    cancellation_requested_at = db.Column(db.DateTime(timezone=True))
    closed_at = db.Column(db.DateTime(timezone=True), index=True)

    __mapper_args__ = {"version_id_col": version}

    student = db.relationship("StudentProfile")
    source_college = db.relationship("College", foreign_keys=[source_college_id])
    source_university = db.relationship("University", foreign_keys=[source_university_id])
    destination_college = db.relationship("College", foreign_keys=[destination_college_id])
    destination_university = db.relationship("University", foreign_keys=[destination_university_id])
    destination_offering = db.relationship("CourseOffering")
    academic_year = db.relationship("AcademicYear")
    window = db.relationship("MigrationWindow")
    vacancy_category = db.relationship("VacancyCategory")

    def to_dict(self, exclude=()):
        """Every column of this row; enums as their value."""
        data = {}
        for column in self.__table__.columns:
            if column.name not in exclude:
                value = getattr(self, column.name)
                data[column.name] = value.value if isinstance(value, enum.Enum) else value
        return data


class MigrationStatusHistory(db.Model):
    __tablename__ = "migration_status_history"

    id = db.Column(db.BigInteger, primary_key=True)
    created_at = db.Column(db.DateTime(timezone=True), nullable=False, default=lambda: datetime.now(timezone.utc),
                           server_default=db.func.now(), index=True)

    request_id = db.Column(db.BigInteger, db.ForeignKey("migration_requests.id", ondelete="CASCADE"),
                           nullable=False, index=True)
    from_status = db.Column(db.Enum(MigrationStatus, name="migration_status"), nullable=True)
    to_status = db.Column(db.Enum(MigrationStatus, name="migration_status"), nullable=False)
    actor_id = db.Column(db.BigInteger, db.ForeignKey("users.id"))
    reason = db.Column(db.Text)

    def to_dict(self, exclude=()):
        """Every column of this row; enums as their value."""
        data = {}
        for column in self.__table__.columns:
            if column.name not in exclude:
                value = getattr(self, column.name)
                data[column.name] = value.value if isinstance(value, enum.Enum) else value
        return data


class GradeConversion(db.Model):
    __tablename__ = "grade_conversions"

    id = db.Column(db.BigInteger, primary_key=True)
    uuid = db.Column(db.String(36), unique=True, nullable=False, index=True, default=lambda: str(uuid4()))
    status = db.Column(db.Enum(RecordStatus, name="record_status"), nullable=False, default=RecordStatus.ACTIVE,
                       server_default=RecordStatus.ACTIVE.value, index=True)
    deleted_at = db.Column(db.DateTime(timezone=True))
    created_at = db.Column(db.DateTime(timezone=True), nullable=False, default=lambda: datetime.now(timezone.utc),
                           server_default=db.func.now())
    updated_at = db.Column(db.DateTime(timezone=True), nullable=False, default=lambda: datetime.now(timezone.utc),
                           onupdate=lambda: datetime.now(timezone.utc), server_default=db.func.now())
    created_by_id = db.Column(db.BigInteger, db.ForeignKey("users.id", use_alter=True, ondelete="SET NULL"))
    updated_by_id = db.Column(db.BigInteger, db.ForeignKey("users.id", use_alter=True, ondelete="SET NULL"))

    request_id = db.Column(db.BigInteger, db.ForeignKey("migration_requests.id", ondelete="CASCADE"),
                           nullable=False, unique=True)
    source_grade_system = db.Column(db.Enum(GradeSystem, name="grade_system"), nullable=False)
    source_value = db.Column(db.Numeric(6, 2), nullable=False)
    converted_percentage = db.Column(db.Numeric(5, 2))
    conversion_source = db.Column(db.Enum(ConversionSource, name="conversion_source"), nullable=False)
    grading_scheme_id = db.Column(db.BigInteger, db.ForeignKey("grading_schemes.id"))
    certificate_document_id = db.Column(db.BigInteger, db.ForeignKey("documents.id", use_alter=True))

    def to_dict(self, exclude=()):
        """Every column of this row; enums as their value."""
        data = {}
        for column in self.__table__.columns:
            if column.name not in exclude:
                value = getattr(self, column.name)
                data[column.name] = value.value if isinstance(value, enum.Enum) else value
        return data


class CreditAssessment(db.Model):
    __tablename__ = "credit_assessments"

    id = db.Column(db.BigInteger, primary_key=True)
    uuid = db.Column(db.String(36), unique=True, nullable=False, index=True, default=lambda: str(uuid4()))
    status = db.Column(db.Enum(RecordStatus, name="record_status"), nullable=False, default=RecordStatus.ACTIVE,
                       server_default=RecordStatus.ACTIVE.value, index=True)
    deleted_at = db.Column(db.DateTime(timezone=True))
    created_at = db.Column(db.DateTime(timezone=True), nullable=False, default=lambda: datetime.now(timezone.utc),
                           server_default=db.func.now())
    updated_at = db.Column(db.DateTime(timezone=True), nullable=False, default=lambda: datetime.now(timezone.utc),
                           onupdate=lambda: datetime.now(timezone.utc), server_default=db.func.now())
    created_by_id = db.Column(db.BigInteger, db.ForeignKey("users.id", use_alter=True, ondelete="SET NULL"))
    updated_by_id = db.Column(db.BigInteger, db.ForeignKey("users.id", use_alter=True, ondelete="SET NULL"))

    request_id = db.Column(db.BigInteger, db.ForeignKey("migration_requests.id", ondelete="CASCADE"),
                           nullable=False, index=True)
    semester = db.Column(db.Integer)
    subject_code = db.Column(db.String(30), nullable=False)
    subject_name = db.Column(db.String(300), nullable=False)
    credits = db.Column(db.Numeric(5, 2), nullable=False)
    grade = db.Column(db.String(10))
    mapped_subject_code = db.Column(db.String(30))  # destination syllabus subject
    decision = db.Column(db.Enum(CreditDecision, name="credit_decision"), nullable=False,
                         default=CreditDecision.PENDING)
    reason = db.Column(db.String(500))
    decided_by_id = db.Column(db.BigInteger, db.ForeignKey("users.id"))
    decided_at = db.Column(db.DateTime(timezone=True))
    decision_history = db.Column(db.JSON, nullable=False, default=list)  # M15-F07

    def to_dict(self, exclude=()):
        """Every column of this row; enums as their value."""
        data = {}
        for column in self.__table__.columns:
            if column.name not in exclude:
                value = getattr(self, column.name)
                data[column.name] = value.value if isinstance(value, enum.Enum) else value
        return data


class EquivalenceDecision(db.Model):
    __tablename__ = "equivalence_decisions"

    id = db.Column(db.BigInteger, primary_key=True)
    uuid = db.Column(db.String(36), unique=True, nullable=False, index=True, default=lambda: str(uuid4()))
    status = db.Column(db.Enum(RecordStatus, name="record_status"), nullable=False, default=RecordStatus.ACTIVE,
                       server_default=RecordStatus.ACTIVE.value, index=True)
    deleted_at = db.Column(db.DateTime(timezone=True))
    created_at = db.Column(db.DateTime(timezone=True), nullable=False, default=lambda: datetime.now(timezone.utc),
                           server_default=db.func.now())
    updated_at = db.Column(db.DateTime(timezone=True), nullable=False, default=lambda: datetime.now(timezone.utc),
                           onupdate=lambda: datetime.now(timezone.utc), server_default=db.func.now())
    created_by_id = db.Column(db.BigInteger, db.ForeignKey("users.id", use_alter=True, ondelete="SET NULL"))
    updated_by_id = db.Column(db.BigInteger, db.ForeignKey("users.id", use_alter=True, ondelete="SET NULL"))

    request_id = db.Column(db.BigInteger, db.ForeignKey("migration_requests.id", ondelete="CASCADE"),
                           nullable=False, unique=True)
    is_eligible = db.Column(db.Boolean, nullable=False)
    credits_accepted = db.Column(db.Numeric(6, 2), nullable=False, default=0)
    additional_papers = db.Column(db.JSON, nullable=False, default=list)
    remarks = db.Column(db.Text)
    decided_by_id = db.Column(db.BigInteger, db.ForeignKey("users.id"))

    def to_dict(self, exclude=()):
        """Every column of this row; enums as their value."""
        data = {}
        for column in self.__table__.columns:
            if column.name not in exclude:
                value = getattr(self, column.name)
                data[column.name] = value.value if isinstance(value, enum.Enum) else value
        return data


class Appeal(db.Model):
    __tablename__ = "appeals"

    id = db.Column(db.BigInteger, primary_key=True)
    uuid = db.Column(db.String(36), unique=True, nullable=False, index=True, default=lambda: str(uuid4()))
    status = db.Column(db.Enum(RecordStatus, name="record_status"), nullable=False, default=RecordStatus.ACTIVE,
                       server_default=RecordStatus.ACTIVE.value, index=True)
    deleted_at = db.Column(db.DateTime(timezone=True))
    created_at = db.Column(db.DateTime(timezone=True), nullable=False, default=lambda: datetime.now(timezone.utc),
                           server_default=db.func.now())
    updated_at = db.Column(db.DateTime(timezone=True), nullable=False, default=lambda: datetime.now(timezone.utc),
                           onupdate=lambda: datetime.now(timezone.utc), server_default=db.func.now())
    created_by_id = db.Column(db.BigInteger, db.ForeignKey("users.id", use_alter=True, ondelete="SET NULL"))
    updated_by_id = db.Column(db.BigInteger, db.ForeignKey("users.id", use_alter=True, ondelete="SET NULL"))

    request_id = db.Column(db.BigInteger, db.ForeignKey("migration_requests.id", ondelete="CASCADE"),
                           nullable=False, unique=True)  # one appeal per request
    reason = db.Column(db.Text, nullable=False)
    evidence_document_id = db.Column(db.BigInteger, db.ForeignKey("documents.id", use_alter=True))
    rejected_at_status = db.Column(db.Enum(MigrationStatus, name="migration_status"), nullable=True)
    decision = db.Column(db.Enum(AppealDecision, name="appeal_decision"), nullable=False,
                         default=AppealDecision.PENDING)
    decided_by_id = db.Column(db.BigInteger, db.ForeignKey("users.id"))
    decided_at = db.Column(db.DateTime(timezone=True))
    decision_note = db.Column(db.Text)
    due_at = db.Column(db.DateTime(timezone=True), nullable=False)

    def to_dict(self, exclude=()):
        """Every column of this row; enums as their value."""
        data = {}
        for column in self.__table__.columns:
            if column.name not in exclude:
                value = getattr(self, column.name)
                data[column.name] = value.value if isinstance(value, enum.Enum) else value
        return data


# ======================================================================
# Interviews
# ======================================================================

class Interview(db.Model):
    __tablename__ = "interviews"
    __table_args__ = (
        db.CheckConstraint("scheduled_end > scheduled_start", name="interview_times_ordered"),
        # Online interviews run on the college's own platform (Zoom, Meet, Teams ...): the link is required.
        db.CheckConstraint("mode <> 'ONLINE' OR meeting_url IS NOT NULL", name="online_interview_has_link"),
    )

    id = db.Column(db.BigInteger, primary_key=True)
    uuid = db.Column(db.String(36), unique=True, nullable=False, index=True, default=lambda: str(uuid4()))
    status = db.Column(db.Enum(RecordStatus, name="record_status"), nullable=False, default=RecordStatus.ACTIVE,
                       server_default=RecordStatus.ACTIVE.value, index=True)
    deleted_at = db.Column(db.DateTime(timezone=True))
    created_at = db.Column(db.DateTime(timezone=True), nullable=False, default=lambda: datetime.now(timezone.utc),
                           server_default=db.func.now())
    updated_at = db.Column(db.DateTime(timezone=True), nullable=False, default=lambda: datetime.now(timezone.utc),
                           onupdate=lambda: datetime.now(timezone.utc), server_default=db.func.now())
    created_by_id = db.Column(db.BigInteger, db.ForeignKey("users.id", use_alter=True, ondelete="SET NULL"))
    updated_by_id = db.Column(db.BigInteger, db.ForeignKey("users.id", use_alter=True, ondelete="SET NULL"))

    request_id = db.Column(db.BigInteger, db.ForeignKey("migration_requests.id", ondelete="CASCADE"),
                           nullable=False, index=True)
    college_id = db.Column(db.Integer, db.ForeignKey("colleges.id"), nullable=False, index=True)
    mode = db.Column(db.Enum(InterviewMode, name="interview_mode"), nullable=False)
    scheduled_start = db.Column(db.DateTime(timezone=True), nullable=False, index=True)
    scheduled_end = db.Column(db.DateTime(timezone=True), nullable=False)
    location = db.Column(db.String(500))
    meeting_url = db.Column(db.String(1000))  # pasted by the college; any https video-call link
    meeting_info = db.Column(db.String(500))  # optional meeting ID / passcode / instructions
    interview_status = db.Column(db.Enum(InterviewStatus, name="interview_status"), nullable=False,
                                 default=InterviewStatus.SCHEDULED, index=True)
    outcome = db.Column(db.Enum(InterviewOutcome, name="interview_outcome"), nullable=False,
                        default=InterviewOutcome.PENDING)
    remarks = db.Column(db.Text)
    outcome_recorded_by_id = db.Column(db.BigInteger, db.ForeignKey("users.id"))
    outcome_recorded_at = db.Column(db.DateTime(timezone=True))
    extra_slot_reason = db.Column(db.String(500))  # outside working hours (M13-F03)
    cancellation_reason = db.Column(db.String(500))
    rescheduled_from_id = db.Column(db.BigInteger, db.ForeignKey("interviews.id"))
    requested_by_student = db.Column(db.Boolean, nullable=False, default=False)
    reminders_sent = db.Column(db.JSON, nullable=False, default=list)

    panel = db.relationship("InterviewPanelist", back_populates="interview", lazy="selectin")

    college = db.relationship("College")

    def to_dict(self, exclude=()):
        """Every column of this row; enums as their value."""
        data = {}
        for column in self.__table__.columns:
            if column.name not in exclude:
                value = getattr(self, column.name)
                data[column.name] = value.value if isinstance(value, enum.Enum) else value
        return data


class InterviewPanelist(db.Model):
    __tablename__ = "interview_panelists"
    __table_args__ = (db.UniqueConstraint("interview_id", "user_id", name="uq_panelist_interview_user"),)

    id = db.Column(db.BigInteger, primary_key=True)
    uuid = db.Column(db.String(36), unique=True, nullable=False, index=True, default=lambda: str(uuid4()))
    status = db.Column(db.Enum(RecordStatus, name="record_status"), nullable=False, default=RecordStatus.ACTIVE,
                       server_default=RecordStatus.ACTIVE.value, index=True)
    deleted_at = db.Column(db.DateTime(timezone=True))
    created_at = db.Column(db.DateTime(timezone=True), nullable=False, default=lambda: datetime.now(timezone.utc),
                           server_default=db.func.now())
    updated_at = db.Column(db.DateTime(timezone=True), nullable=False, default=lambda: datetime.now(timezone.utc),
                           onupdate=lambda: datetime.now(timezone.utc), server_default=db.func.now())
    created_by_id = db.Column(db.BigInteger, db.ForeignKey("users.id", use_alter=True, ondelete="SET NULL"))
    updated_by_id = db.Column(db.BigInteger, db.ForeignKey("users.id", use_alter=True, ondelete="SET NULL"))

    interview_id = db.Column(db.BigInteger, db.ForeignKey("interviews.id", ondelete="CASCADE"),
                             nullable=False, index=True)
    user_id = db.Column(db.BigInteger, db.ForeignKey("users.id"), nullable=False, index=True)

    interview = db.relationship("Interview", back_populates="panel")

    def to_dict(self, exclude=()):
        """Every column of this row; enums as their value."""
        data = {}
        for column in self.__table__.columns:
            if column.name not in exclude:
                value = getattr(self, column.name)
                data[column.name] = value.value if isinstance(value, enum.Enum) else value
        return data


# ======================================================================
# Letters
# ======================================================================

class LetterTemplate(db.Model):
    """Default templates (university_id null) or a university's own (12.2)."""
    __tablename__ = "letter_templates"
    __table_args__ = (db.UniqueConstraint("letter_type", "university_id",
                                          name="uq_letter_templates_type_university"),)

    id = db.Column(db.BigInteger, primary_key=True)
    uuid = db.Column(db.String(36), unique=True, nullable=False, index=True, default=lambda: str(uuid4()))
    status = db.Column(db.Enum(RecordStatus, name="record_status"), nullable=False, default=RecordStatus.ACTIVE,
                       server_default=RecordStatus.ACTIVE.value, index=True)
    deleted_at = db.Column(db.DateTime(timezone=True))
    created_at = db.Column(db.DateTime(timezone=True), nullable=False, default=lambda: datetime.now(timezone.utc),
                           server_default=db.func.now())
    updated_at = db.Column(db.DateTime(timezone=True), nullable=False, default=lambda: datetime.now(timezone.utc),
                           onupdate=lambda: datetime.now(timezone.utc), server_default=db.func.now())
    created_by_id = db.Column(db.BigInteger, db.ForeignKey("users.id", use_alter=True, ondelete="SET NULL"))
    updated_by_id = db.Column(db.BigInteger, db.ForeignKey("users.id", use_alter=True, ondelete="SET NULL"))

    letter_type = db.Column(db.Enum(LetterType, name="letter_type"), nullable=False)
    university_id = db.Column(db.Integer, db.ForeignKey("universities.id"))
    subject = db.Column(db.String(300), nullable=False)
    body = db.Column(db.Text, nullable=False)  # str.format placeholders, see services.letters

    university = db.relationship("University")

    def to_dict(self, exclude=()):
        """Every column of this row; enums as their value."""
        data = {}
        for column in self.__table__.columns:
            if column.name not in exclude:
                value = getattr(self, column.name)
                data[column.name] = value.value if isinstance(value, enum.Enum) else value
        return data


class Letter(db.Model):
    __tablename__ = "letters"
    __table_args__ = (db.CheckConstraint("(university_id IS NULL) <> (college_id IS NULL)", name="letter_one_institution"),)

    id = db.Column(db.BigInteger, primary_key=True)
    uuid = db.Column(db.String(36), unique=True, nullable=False, index=True, default=lambda: str(uuid4()))
    status = db.Column(db.Enum(RecordStatus, name="record_status"), nullable=False, default=RecordStatus.ACTIVE,
                       server_default=RecordStatus.ACTIVE.value, index=True)
    deleted_at = db.Column(db.DateTime(timezone=True))
    created_at = db.Column(db.DateTime(timezone=True), nullable=False, default=lambda: datetime.now(timezone.utc),
                           server_default=db.func.now())
    updated_at = db.Column(db.DateTime(timezone=True), nullable=False, default=lambda: datetime.now(timezone.utc),
                           onupdate=lambda: datetime.now(timezone.utc), server_default=db.func.now())
    created_by_id = db.Column(db.BigInteger, db.ForeignKey("users.id", use_alter=True, ondelete="SET NULL"))
    updated_by_id = db.Column(db.BigInteger, db.ForeignKey("users.id", use_alter=True, ondelete="SET NULL"))

    request_id = db.Column(db.BigInteger, db.ForeignKey("migration_requests.id"), nullable=False, index=True)
    # issuing institution: exactly one of university_id / college_id
    university_id = db.Column(db.Integer, db.ForeignKey("universities.id"))
    college_id = db.Column(db.Integer, db.ForeignKey("colleges.id"))
    letter_type = db.Column(db.Enum(LetterType, name="letter_type"), nullable=False)
    reference_no = db.Column(db.String(60), nullable=False, unique=True)
    qr_token = db.Column(db.String(64), nullable=False, unique=True, index=True)
    storage_key = db.Column(db.String(500), nullable=False)
    letter_status = db.Column(db.Enum(LetterStatus, name="letter_status"), nullable=False, default=LetterStatus.VALID)
    signatory_name = db.Column(db.String(200))
    signatory_designation = db.Column(db.String(150))
    issued_at = db.Column(db.DateTime(timezone=True), nullable=False)
    cancelled_at = db.Column(db.DateTime(timezone=True))

    university = db.relationship("University")
    college = db.relationship("College")

    def to_dict(self, exclude=()):
        """Every column of this row; enums as their value."""
        data = {}
        for column in self.__table__.columns:
            if column.name not in exclude:
                value = getattr(self, column.name)
                data[column.name] = value.value if isinstance(value, enum.Enum) else value
        return data


# ======================================================================
# Notifications, support and activity
# ======================================================================

class UserActivity(db.Model):
    """ONE row per API request with all its major steps (append-only).

    Carries created_by / updated_by / created_at / updated_at as requested.
    """
    __tablename__ = "user_activity"

    id = db.Column(db.BigInteger, primary_key=True)
    request_id = db.Column(db.String(36), nullable=False, unique=True)
    api_code = db.Column(db.String(80), nullable=False, index=True)
    http_method = db.Column(db.String(10), nullable=False)
    path = db.Column(db.String(500), nullable=False)
    user_id = db.Column(db.BigInteger, db.ForeignKey("users.id", ondelete="SET NULL"), index=True)
    session_id = db.Column(db.BigInteger, db.ForeignKey("user_sessions.id", ondelete="SET NULL"), index=True)
    university_id = db.Column(db.Integer, db.ForeignKey("universities.id", ondelete="SET NULL"), index=True)
    college_id = db.Column(db.Integer, db.ForeignKey("colleges.id", ondelete="SET NULL"), index=True)
    ip_address = db.Column(db.String(100), index=True)
    user_agent = db.Column(db.String(500))
    request_payload = db.Column(db.JSON)
    steps = db.Column(db.JSON, nullable=False, default=list)
    changes = db.Column(db.JSON)
    entity_type = db.Column(db.String(80), index=True)
    entity_id = db.Column(db.String(64), index=True)
    outcome = db.Column(db.Enum(ActivityOutcome, name="activity_outcome"), nullable=False, index=True)
    http_status = db.Column(db.Integer, nullable=False)
    error_code = db.Column(db.String(80))
    error_message = db.Column(db.String(1000))
    duration_ms = db.Column(db.Integer, nullable=False)
    created_by = db.Column(db.BigInteger, db.ForeignKey("users.id", ondelete="SET NULL"))
    updated_by = db.Column(db.BigInteger, db.ForeignKey("users.id", ondelete="SET NULL"))
    created_at = db.Column(db.DateTime(timezone=True), nullable=False, index=True)
    updated_at = db.Column(db.DateTime(timezone=True), nullable=False)

    def to_dict(self, exclude=()):
        """Every column of this row; enums as their value."""
        data = {}
        for column in self.__table__.columns:
            if column.name not in exclude:
                value = getattr(self, column.name)
                data[column.name] = value.value if isinstance(value, enum.Enum) else value
        return data


class Notification(db.Model):
    __tablename__ = "notifications"

    id = db.Column(db.BigInteger, primary_key=True)
    uuid = db.Column(db.String(36), unique=True, nullable=False, index=True, default=lambda: str(uuid4()))
    status = db.Column(db.Enum(RecordStatus, name="record_status"), nullable=False, default=RecordStatus.ACTIVE,
                       server_default=RecordStatus.ACTIVE.value, index=True)
    deleted_at = db.Column(db.DateTime(timezone=True))
    created_at = db.Column(db.DateTime(timezone=True), nullable=False, default=lambda: datetime.now(timezone.utc),
                           server_default=db.func.now())
    updated_at = db.Column(db.DateTime(timezone=True), nullable=False, default=lambda: datetime.now(timezone.utc),
                           onupdate=lambda: datetime.now(timezone.utc), server_default=db.func.now())
    created_by_id = db.Column(db.BigInteger, db.ForeignKey("users.id", use_alter=True, ondelete="SET NULL"))
    updated_by_id = db.Column(db.BigInteger, db.ForeignKey("users.id", use_alter=True, ondelete="SET NULL"))

    user_id = db.Column(db.BigInteger, db.ForeignKey("users.id", ondelete="CASCADE"), index=True)
    event = db.Column(db.Enum(NotificationEvent, name="notification_event"), nullable=False, index=True)
    channel = db.Column(db.Enum(NotificationChannel, name="notification_channel"), nullable=False)
    recipient = db.Column(db.String(255), nullable=False)
    subject = db.Column(db.String(300))
    body = db.Column(db.Text, nullable=False)
    link = db.Column(db.String(500))
    payload = db.Column(db.JSON, nullable=False, default=dict)
    delivery_state = db.Column(db.Enum(NotificationState, name="notification_state"), nullable=False,
                               default=NotificationState.QUEUED, index=True)
    attempts = db.Column(db.Integer, nullable=False, default=0)
    next_attempt_at = db.Column(db.DateTime(timezone=True), index=True)  # when the retry job tries again
    last_error = db.Column(db.String(1000))
    sent_at = db.Column(db.DateTime(timezone=True))
    read_at = db.Column(db.DateTime(timezone=True))
    idempotency_key = db.Column(db.String(200), unique=True)
    fallback_of_id = db.Column(db.BigInteger, db.ForeignKey("notifications.id"))

    def to_dict(self, exclude=()):
        """Every column of this row; enums as their value."""
        data = {}
        for column in self.__table__.columns:
            if column.name not in exclude:
                value = getattr(self, column.name)
                data[column.name] = value.value if isinstance(value, enum.Enum) else value
        return data


class SupportTicket(db.Model):
    __tablename__ = "support_tickets"

    id = db.Column(db.BigInteger, primary_key=True)
    uuid = db.Column(db.String(36), unique=True, nullable=False, index=True, default=lambda: str(uuid4()))
    status = db.Column(db.Enum(RecordStatus, name="record_status"), nullable=False, default=RecordStatus.ACTIVE,
                       server_default=RecordStatus.ACTIVE.value, index=True)
    deleted_at = db.Column(db.DateTime(timezone=True))
    created_at = db.Column(db.DateTime(timezone=True), nullable=False, default=lambda: datetime.now(timezone.utc),
                           server_default=db.func.now())
    updated_at = db.Column(db.DateTime(timezone=True), nullable=False, default=lambda: datetime.now(timezone.utc),
                           onupdate=lambda: datetime.now(timezone.utc), server_default=db.func.now())
    created_by_id = db.Column(db.BigInteger, db.ForeignKey("users.id", use_alter=True, ondelete="SET NULL"))
    updated_by_id = db.Column(db.BigInteger, db.ForeignKey("users.id", use_alter=True, ondelete="SET NULL"))

    reference_no = db.Column(db.String(30), nullable=False, unique=True)
    raised_by_id = db.Column(db.BigInteger, db.ForeignKey("users.id"), nullable=False, index=True)
    category = db.Column(db.Enum(TicketCategory, name="ticket_category"), nullable=False,
                         default=TicketCategory.GENERAL)
    subject = db.Column(db.String(300), nullable=False)
    description = db.Column(db.Text, nullable=False)
    ticket_state = db.Column(db.Enum(TicketState, name="ticket_state"), nullable=False,
                             default=TicketState.OPEN, index=True)
    assigned_to_id = db.Column(db.BigInteger, db.ForeignKey("users.id"))
    related_request_id = db.Column(db.BigInteger, db.ForeignKey("migration_requests.id"))
    first_response_at = db.Column(db.DateTime(timezone=True))
    resolved_at = db.Column(db.DateTime(timezone=True))

    replies = db.relationship("SupportTicketReply", back_populates="ticket",
                              order_by="SupportTicketReply.created_at")

    def to_dict(self, exclude=()):
        """Every column of this row; enums as their value."""
        data = {}
        for column in self.__table__.columns:
            if column.name not in exclude:
                value = getattr(self, column.name)
                data[column.name] = value.value if isinstance(value, enum.Enum) else value
        return data


class SupportTicketReply(db.Model):
    __tablename__ = "support_ticket_replies"

    id = db.Column(db.BigInteger, primary_key=True)
    uuid = db.Column(db.String(36), unique=True, nullable=False, index=True, default=lambda: str(uuid4()))
    status = db.Column(db.Enum(RecordStatus, name="record_status"), nullable=False, default=RecordStatus.ACTIVE,
                       server_default=RecordStatus.ACTIVE.value, index=True)
    deleted_at = db.Column(db.DateTime(timezone=True))
    created_at = db.Column(db.DateTime(timezone=True), nullable=False, default=lambda: datetime.now(timezone.utc),
                           server_default=db.func.now())
    updated_at = db.Column(db.DateTime(timezone=True), nullable=False, default=lambda: datetime.now(timezone.utc),
                           onupdate=lambda: datetime.now(timezone.utc), server_default=db.func.now())
    created_by_id = db.Column(db.BigInteger, db.ForeignKey("users.id", use_alter=True, ondelete="SET NULL"))
    updated_by_id = db.Column(db.BigInteger, db.ForeignKey("users.id", use_alter=True, ondelete="SET NULL"))

    ticket_id = db.Column(db.BigInteger, db.ForeignKey("support_tickets.id", ondelete="CASCADE"),
                          nullable=False, index=True)
    author_id = db.Column(db.BigInteger, db.ForeignKey("users.id"), nullable=False)
    message = db.Column(db.Text, nullable=False)
    is_internal = db.Column(db.Boolean, nullable=False, default=False)

    ticket = db.relationship("SupportTicket", back_populates="replies")

    def to_dict(self, exclude=()):
        """Every column of this row; enums as their value."""
        data = {}
        for column in self.__table__.columns:
            if column.name not in exclude:
                value = getattr(self, column.name)
                data[column.name] = value.value if isinstance(value, enum.Enum) else value
        return data


class DataRequest(db.Model):
    """DPDP rights requests (11.1)."""
    __tablename__ = "data_requests"

    id = db.Column(db.BigInteger, primary_key=True)
    uuid = db.Column(db.String(36), unique=True, nullable=False, index=True, default=lambda: str(uuid4()))
    status = db.Column(db.Enum(RecordStatus, name="record_status"), nullable=False, default=RecordStatus.ACTIVE,
                       server_default=RecordStatus.ACTIVE.value, index=True)
    deleted_at = db.Column(db.DateTime(timezone=True))
    created_at = db.Column(db.DateTime(timezone=True), nullable=False, default=lambda: datetime.now(timezone.utc),
                           server_default=db.func.now())
    updated_at = db.Column(db.DateTime(timezone=True), nullable=False, default=lambda: datetime.now(timezone.utc),
                           onupdate=lambda: datetime.now(timezone.utc), server_default=db.func.now())
    created_by_id = db.Column(db.BigInteger, db.ForeignKey("users.id", use_alter=True, ondelete="SET NULL"))
    updated_by_id = db.Column(db.BigInteger, db.ForeignKey("users.id", use_alter=True, ondelete="SET NULL"))

    user_id = db.Column(db.BigInteger, db.ForeignKey("users.id"), nullable=False, index=True)
    request_type = db.Column(db.Enum(DataRequestType, name="data_request_type"), nullable=False)
    details = db.Column(db.Text)
    request_state = db.Column(db.Enum(DataRequestState, name="data_request_state"), nullable=False,
                              default=DataRequestState.OPEN, index=True)
    due_at = db.Column(db.DateTime(timezone=True), nullable=False)
    completed_at = db.Column(db.DateTime(timezone=True))
    handled_by_id = db.Column(db.BigInteger, db.ForeignKey("users.id"))
    response_note = db.Column(db.Text)
    export_document_id = db.Column(db.BigInteger, db.ForeignKey("documents.id", use_alter=True))

    def to_dict(self, exclude=()):
        """Every column of this row; enums as their value."""
        data = {}
        for column in self.__table__.columns:
            if column.name not in exclude:
                value = getattr(self, column.name)
                data[column.name] = value.value if isinstance(value, enum.Enum) else value
        return data


class BulkUploadJob(db.Model):
    __tablename__ = "bulk_upload_jobs"
    __table_args__ = (db.CheckConstraint("(university_id IS NULL) <> (college_id IS NULL)", name="bulk_upload_one_institution"),)

    id = db.Column(db.BigInteger, primary_key=True)
    uuid = db.Column(db.String(36), unique=True, nullable=False, index=True, default=lambda: str(uuid4()))
    status = db.Column(db.Enum(RecordStatus, name="record_status"), nullable=False, default=RecordStatus.ACTIVE,
                       server_default=RecordStatus.ACTIVE.value, index=True)
    deleted_at = db.Column(db.DateTime(timezone=True))
    created_at = db.Column(db.DateTime(timezone=True), nullable=False, default=lambda: datetime.now(timezone.utc),
                           server_default=db.func.now())
    updated_at = db.Column(db.DateTime(timezone=True), nullable=False, default=lambda: datetime.now(timezone.utc),
                           onupdate=lambda: datetime.now(timezone.utc), server_default=db.func.now())
    created_by_id = db.Column(db.BigInteger, db.ForeignKey("users.id", use_alter=True, ondelete="SET NULL"))
    updated_by_id = db.Column(db.BigInteger, db.ForeignKey("users.id", use_alter=True, ondelete="SET NULL"))

    upload_type = db.Column(db.Enum(BulkUploadType, name="bulk_upload_type"), nullable=False)
    university_id = db.Column(db.Integer, db.ForeignKey("universities.id"), index=True)
    college_id = db.Column(db.Integer, db.ForeignKey("colleges.id"), index=True)
    filename = db.Column(db.String(300))
    total_rows = db.Column(db.Integer, nullable=False, default=0)
    success_rows = db.Column(db.Integer, nullable=False, default=0)
    error_rows = db.Column(db.Integer, nullable=False, default=0)
    error_file_key = db.Column(db.String(500))
    errors = db.Column(db.JSON, nullable=False, default=list)

    university = db.relationship("University")
    college = db.relationship("College")

    def to_dict(self, exclude=()):
        """Every column of this row; enums as their value."""
        data = {}
        for column in self.__table__.columns:
            if column.name not in exclude:
                value = getattr(self, column.name)
                data[column.name] = value.value if isinstance(value, enum.Enum) else value
        return data
