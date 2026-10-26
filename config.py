"""
Student Migration System - all application configuration.

Every tunable value in the system lives in this file. Secrets (passwords, keys,
provider tokens) are never written here: they are read from environment
variables (or a local .env file) so they never reach version control.

Select the active configuration with the APP_ENV environment variable:
    development (default) | testing | production
"""
import os
from datetime import timedelta

from dotenv import load_dotenv

BASE_DIR = os.path.abspath(os.path.dirname(__file__))
load_dotenv(os.path.join(BASE_DIR, ".env"))


def _env(name, default=None, cast=str):
    value = os.environ.get(name)
    if value is None or value == "":
        return default
    if cast is bool:
        return value.strip().lower() in ("1", "true", "yes", "on")
    return cast(value)


# =============================================================================
# API CODES
# Every API has ONE unique code. A route uses it as @api("CREATE_STUDENT", ...).
# The code is returned in every response (api_code, X-Api-Code header) and stored
# on the request's row in user_activity. The app refuses to start if:
#   * a code below appears twice,
#   * a route uses a code that is not listed here, or
#   * a code listed here has no route.
# To add an API: add its code and description here, then decorate the route.
# =============================================================================
def _unique_codes(pairs):
    seen = {}
    for code, description in pairs:
        if code in seen:
            raise ValueError(f"API code {code} is defined twice in config.API_CODES")
        if code != code.upper() or " " in code:
            raise ValueError(f"API code {code} must be UPPER_SNAKE_CASE")
        seen[code] = description
    return seen


API_CODES = _unique_codes([
    # ---- Courses, syllabi, rules, windows, offerings
    ("CREATE_COURSE", "Create a course under a university"),
    ("LIST_COURSES", "Courses of a university"),
    ("GET_COURSE", "One course with its syllabus assignments and current rule"),
    ("UPDATE_COURSE", "Update a course"),
    ("DEACTIVATE_COURSE", "Deactivate a course (no new requests; history kept)"),
    ("ACTIVATE_COURSE", "Reactivate a course"),
    ("CREATE_SYLLABUS", "Create a draft syllabus with subjects and credits"),
    ("LIST_SYLLABI", "Syllabi of a university"),
    ("GET_SYLLABUS", "A syllabus with its subjects"),
    ("UPDATE_SYLLABUS", "Edit a draft syllabus (published ones are read-only)"),
    ("PUBLISH_SYLLABUS", "Publish a syllabus (it becomes read-only)"),
    ("CREATE_SYLLABUS_VERSION", "Copy a syllabus into a new draft version"),
    ("ASSIGN_SYLLABUS_TO_COURSE", "Assign a published syllabus to a course for a batch range"),
    ("CLOSE_SYLLABUS_ASSIGNMENT", "Set the last batch of a syllabus assignment"),
    ("CREATE_COURSE_EQUIVALENCE", "Add an accepted source course"),
    ("LIST_COURSE_EQUIVALENCES", "Accepted source courses for a course"),
    ("DEACTIVATE_COURSE_EQUIVALENCE", "Remove an accepted source course"),
    ("CREATE_ELIGIBILITY_RULE", "New eligibility rule version with an effective date"),
    ("LIST_ELIGIBILITY_RULES", "All eligibility rule versions of a course"),
    ("CREATE_GRADING_SCHEME", "Grade system and official conversion formula, with the circular"),
    ("LIST_GRADING_SCHEMES", "Grading schemes of a university"),
    ("CREATE_ACADEMIC_YEAR", "Platform calendar year (PA) or a university's year (?university_id=)"),
    ("LIST_ACADEMIC_YEARS", "Academic years (platform calendar and a university's own)"),
    ("SET_CURRENT_ACADEMIC_YEAR", "Mark an academic year as current"),
    ("CREATE_MIGRATION_WINDOW", "Open and close dates and entry semesters for a course and year"),
    ("UPDATE_MIGRATION_WINDOW", "Change a window's dates or entry semesters"),
    ("LIST_MIGRATION_WINDOWS", "Migration windows (filter by course, year, university)"),
    ("ASSIGN_COURSE_TO_COLLEGE", "University assigns a course to an affiliated college"),
    ("REQUEST_COURSE_OFFERING", "College asks to offer a course"),
    ("LIST_OFFERING_REQUESTS", "Course offering requests (university: incoming, college: own)"),
    ("DECIDE_OFFERING_REQUEST", "Approve (with intake) or reject a course offering request"),
    ("LIST_COURSE_OFFERINGS", "Course offerings (filter by college, course, university)"),
    ("UPDATE_COURSE_OFFERING", "Change sanctioned intake or deactivate an offering"),
    # ---- Platform administration
    ("HEALTH_CHECK", "Liveness and database check"),
    ("LIST_API_CODES", "Catalogue of every API with its unique code"),
    ("GET_SYSTEM_SETTINGS", "Effective non-secret settings from config.py"),
    ("LIST_USERS", "All users (Platform Admin)"),
    ("GET_USER", "One user with roles and memberships"),
    ("DEACTIVATE_USER", "Deactivate any account (sessions end at once)"),
    ("ACTIVATE_USER", "Reactivate an account"),
    ("FORCE_LOGOUT_USER", "End all of a user's sessions"),
    ("LOCK_USER", "Lock an account until password reset"),
    ("UNLOCK_USER", "Unlock an account"),
    ("LIST_USER_ACTIVITY", "Audit log: one row per API call with its steps (filters: api_code, user, outcome, dates)"),
    ("GET_USER_ACTIVITY", "One activity row with payload, steps and changes"),
    ("LIST_INTEGRATION_FAILURES", "Failed notifications"),
    ("RETRY_NOTIFICATION", "Retry a failed notification"),
    ("LIST_IP_BLOCKS", "Blocked IP addresses"),
    ("REMOVE_IP_BLOCK", "Lift an IP block early"),
    # ---- Sign-in, sessions and account
    ("LOGIN", "Log in with email and password; returns a token or asks for the 2FA code"),
    ("VERIFY_LOGIN_OTP", "Complete login with the two-factor code"),
    ("RESEND_LOGIN_OTP", "Send a new two-factor code"),
    ("LOGOUT", "End the current session"),
    ("GET_CURRENT_USER", "Current user, roles, memberships and permissions"),
    ("CHANGE_PASSWORD", "Change password (current, new, confirm)"),
    ("FORGOT_PASSWORD", "Email a single-use password reset link"),
    ("RESET_PASSWORD", "Set a new password using the reset link"),
    ("VERIFY_EMAIL", "Verify email ownership with the emailed code"),
    ("RESEND_EMAIL_OTP", "Send a new email verification code"),
    ("UPDATE_MY_PREFERENCES", "Notification channel, WhatsApp opt-in, language, optional 2FA"),
    ("LIST_MY_SESSIONS", "My recent sessions"),
    # ---- Dashboards and reports
    ("GET_STUDENT_DASHBOARD", "My pending tasks, requests, interviews and documents"),
    ("GET_COLLEGE_DASHBOARD", "College pending work and summaries"),
    ("GET_UNIVERSITY_DASHBOARD", "University pending work and summaries"),
    ("GET_PLATFORM_DASHBOARD", "Platform-wide pending work, alerts and onboarding priorities"),
    ("REPORT_APPLICATIONS", "Applications by status, course, college and period (?format=xlsx exports)"),
    ("REPORT_VACANCIES", "Vacancies: sanctioned, uploaded, blocked, filled, available (?format=xlsx exports)"),
    ("REPORT_MIGRATION_FLOW", "Students in and out by institution (?format=xlsx exports)"),
    ("REPORT_TURNAROUND", "Days spent at each approval step; overdue releases (?format=xlsx exports)"),
    ("REPORT_INTERVIEWS", "Interviews: scheduled, completed, cancelled, no-show (?format=xlsx exports)"),
    ("REPORT_CREDIT_TRANSFER", "Credits accepted, rejected and pending (?format=xlsx exports)"),
    ("REPORT_DOCUMENT_VERIFICATION", "Documents pending, verified and rejected (?format=xlsx exports)"),
    ("REPORT_ONBOARDING", "Institutions by onboarding status; students waiting (Platform Admin) (?format=xlsx exports)"),
    # ---- Master data and bulk uploads
    ("LIST_IMPORT_BATCHES", "Master data import history"),
    ("GET_IMPORT_BATCH", "One import with its exception rows"),
    ("DOWNLOAD_BULK_TEMPLATE", "Excel template for a bulk upload type"),
    ("UPLOAD_BULK_FILE", "Process an Excel bulk upload; returns counts and an error file"),
    ("LIST_BULK_UPLOADS", "Bulk upload history for an institution"),
    # ---- Documents and files
    ("UPLOAD_PROFILE_DOCUMENT", "Upload a reusable profile document (ID, marksheets, ABC statement ...)"),
    ("LIST_MY_DOCUMENTS", "My current documents"),
    ("GET_DOCUMENT", "Document details with a short-lived download link"),
    ("LIST_DOCUMENT_VERSIONS", "All versions of a document"),
    ("VERIFY_DOCUMENT", "Verify, reject or ask for a corrected document"),
    ("DOWNLOAD_FILE", "Download a file with a signed, short-lived link"),
    # ---- Institutions and geography
    ("LIST_STATES", "States"),
    ("LIST_DISTRICTS", "Districts of a state"),
    ("LOOKUP_PINCODE", "Locality, district and state for a PIN code"),
    ("LIST_INSTITUTIONS", "Search institution master data (type, state, district, status, affiliation)"),
    ("GET_INSTITUTION", "Institution profile"),
    ("UPDATE_INSTITUTION_PROFILE", "Update address and contact details"),
    ("SUSPEND_INSTITUTION", "Suspend a university (PA) or a college (its university)"),
    ("REACTIVATE_INSTITUTION", "Reactivate a suspended institution"),
    ("UPDATE_COLLEGE_AFFILIATION", "Correct a college's parent university"),
    ("UPDATE_LETTER_BRANDING", "Letter header, footer and seal image"),
    ("UPDATE_RESERVATION_POLICY", "Whether unfilled reserved seats go to the open pool at window close"),
    # ---- Interviews
    ("SCHEDULE_INTERVIEW", "Schedule an interview within working hours (or a one-off slot)"),
    ("LIST_INTERVIEWS", "Interviews in my scope (filters: status, request_id, from, to)"),
    ("GET_INTERVIEW", "One interview"),
    ("RESCHEDULE_INTERVIEW", "College moves an interview"),
    ("REQUEST_INTERVIEW_RESCHEDULE", "Student moves the interview once, at least 24 hours ahead"),
    ("CANCEL_INTERVIEW", "Cancel an interview with a reason"),
    ("RECORD_INTERVIEW_OUTCOME", "Record PASSED or FAILED with remarks"),
    ("MARK_INTERVIEW_NO_SHOW", "Record that the student did not attend"),
    ("UPDATE_INTERVIEW_MEETING_LINK", "Change the meeting link or details of a scheduled online interview"),
    # ---- Letters
    ("LIST_REQUEST_LETTERS", "Letters issued on a request"),
    ("GET_LETTER_LINK", "Short-lived download link for a letter PDF"),
    ("VERIFY_LETTER", "Public QR check: is this letter genuine and valid?"),
    ("LIST_LETTER_TEMPLATES", "Default and university letter templates"),
    ("UPSERT_LETTER_TEMPLATE", "Platform default (no university) or a university's own template"),
    # ---- Migration requests
    ("CREATE_MIGRATION_REQUEST", "Start a draft migration request"),
    ("LIST_MY_MIGRATION_REQUESTS", "My migration requests"),
    ("UPDATE_MIGRATION_REQUEST", "Change a draft's entry semester"),
    ("UPLOAD_REQUEST_DOCUMENT", "Upload a document to a draft or a request returned for correction"),
    ("GET_DOCUMENT_CHECKLIST", "Required documents and their status"),
    ("SET_GRADE_CONVERSION", "Record grade conversion (automatic, or certificate + stated percentage)"),
    ("SUBMIT_MIGRATION_REQUEST", "Submit a complete draft (sends the NOC request)"),
    ("RESUBMIT_MIGRATION_REQUEST", "Send corrections back to the destination college"),
    ("WITHDRAW_MIGRATION_REQUEST", "Withdraw a request before accepting an offer"),
    ("ACCEPT_OFFER", "Accept a provisional offer (other active requests are withdrawn)"),
    ("DECLINE_OFFER", "Decline a provisional offer"),
    ("REQUEST_CANCELLATION", "Ask to cancel after accepting an offer (before release)"),
    ("FILE_APPEAL", "Appeal a rejection once, within the appeal window"),
    ("GET_MIGRATION_REQUEST", "Full request: checklist, grades, credits, interviews, letters, history"),
    ("LIST_INSTITUTION_MIGRATION_REQUESTS", "Requests in or out of my institutions (filters: direction, status, ...)"),
    ("DECIDE_NOC", "Current college grants the NOC or refuses it for a listed reason"),
    ("UPLOAD_TRANSFER_CERTIFICATE", "Current college uploads the TC to release the student"),
    ("RELEASE_FROM_UNIVERSITY", "Source university approves leaving and uploads the migration certificate"),
    ("START_REVIEW", "Destination college starts reviewing"),
    ("RETURN_FOR_CORRECTION", "Send the request back to the student with what to fix"),
    ("ASSESS_CREDITS", "Accept or reject credits subject by subject"),
    ("RUN_ELIGIBILITY_CHECK", "Re-run the automatic eligibility check"),
    ("RECOMMEND_MIGRATION_REQUEST", "Recommend to the destination university"),
    ("REJECT_MIGRATION_REQUEST", "Reject (college during review, university after recommendation)"),
    ("APPROVE_CANCELLATION", "Cancel between offer acceptance and release"),
    ("CONFIRM_ADMISSION", "Confirm admission after release and record fee paid"),
    ("VERIFY_ORIGINALS", "Record that original documents were verified at joining"),
    ("CANCEL_FOR_NON_REPORTING", "Cancel an admission when the student did not report with originals"),
    ("APPROVE_ENTRY", "Destination university approves entry with the equivalence decision"),
    ("OVERRIDE_ELIGIBILITY", "Override a non-statutory eligibility result with a reason"),
    ("CANCEL_ADMISSION", "Destination university cancels a confirmed admission"),
    ("DECIDE_APPEAL", "Uphold or refuse an appeal"),
    ("LIST_APPEALS", "Appeals waiting for my university"),
    ("RUN_MERIT_ROUND", "Run the merit round for a closed window now (normally automatic)"),
    # ---- Notifications
    ("LIST_MY_NOTIFICATIONS", "My in-app notifications (?unread=true)"),
    ("MARK_NOTIFICATION_READ", "Mark one notification as read"),
    ("MARK_ALL_NOTIFICATIONS_READ", "Mark all my notifications as read"),
    ("LIST_NOTIFICATION_HISTORY", "Delivery history: channel, state, attempts (admins; own institution users)"),
    # ---- Onboarding and registration
    ("LOOKUP_INSTITUTION_BY_AISHE", "Find an institution by AISHE code (registration forms)"),
    ("GET_INVITATION", "Details of an invitation or signup link"),
    ("ACCEPT_INVITATION", "Create the account (and institution) from an invitation or signup link"),
    ("SUBMIT_INSTITUTION_REGISTRATION", "Request admin access for an institution (path C)"),
    ("CREATE_UNIVERSITY", "Create or pick a university and invite its pre-approved admin (path A)"),
    ("CREATE_STANDALONE_INSTITUTION", "Create or pick a standalone institution and invite its admin"),
    ("SEND_UNIVERSITY_SIGNUP_LINK", "Email a university signup link bound to email and AISHE code (path B)"),
    ("SEND_STANDALONE_SIGNUP_LINK", "Email a standalone institution signup link"),
    ("INVITE_PLATFORM_ADMIN", "Invite another Platform Admin"),
    ("CREATE_COLLEGE", "Create or pick an affiliated college and invite its pre-approved admin"),
    ("SEND_COLLEGE_SIGNUP_LINK", "Email a college signup link (affiliated colleges only)"),
    ("LIST_REGISTRATION_REQUESTS", "Registration requests I can review"),
    ("GET_REGISTRATION_REQUEST", "One registration request with the authorization letter link"),
    ("APPROVE_REGISTRATION", "Approve an institution admin registration"),
    ("REJECT_REGISTRATION", "Reject an institution admin registration with a reason"),
    ("LIST_COLLEGE_ONBOARDING_REQUESTS", "Students' requests to onboard their college"),
    ("DECIDE_COLLEGE_ONBOARDING_REQUEST", "Send the college a signup link, or decline"),
    # ---- Search
    ("SEARCH_OFFERINGS", "Search courses, colleges and universities with seat availability"),
    ("GET_OFFERING_DETAILS", "One offering with windows, rules and availability"),
    # ---- Staff, invitations and permissions
    ("LIST_PERMISSION_BUNDLES", "Permission bundles and what each grants"),
    ("INVITE_STAFF_USER", "Invite a University/College User (or a secondary admin)"),
    ("LIST_INVITATIONS", "Invitations sent for an institution"),
    ("REVOKE_INVITATION", "Revoke an unused invitation"),
    ("REQUEST_STAFF_ACCESS", "Register as staff and request access to an institution"),
    ("LIST_ACCESS_REQUESTS", "Pending staff access requests"),
    ("DECIDE_ACCESS_REQUEST", "Approve (with bundles) or reject a staff access request"),
    ("LIST_INSTITUTION_USERS", "Admins and users of an institution"),
    ("UPDATE_USER_PERMISSIONS", "Change a user's permission bundles"),
    ("DEACTIVATE_INSTITUTION_USER", "Deactivate a member (ends their sessions)"),
    ("ACTIVATE_INSTITUTION_USER", "Reactivate a member"),
    ("TRANSFER_PRIMARY_ADMIN", "Hand the primary admin role to another admin"),
    # ---- Students and enrolment
    ("CREATE_STUDENT", "Register a student: account, profile and consent (no approval needed)"),
    ("GET_STUDENT_PROFILE", "My student profile"),
    ("UPDATE_STUDENT_PROFILE", "Update my profile, ABC account ID, category and guardian"),
    ("SUBMIT_ENROLMENT_DETAILS", "Claim my current college with enrolment number and proof"),
    ("REQUEST_PARENTAL_CONSENT", "Email a consent code to my parent or guardian (under 18)"),
    ("CONFIRM_PARENTAL_CONSENT", "Record verified parental consent"),
    ("REQUEST_COLLEGE_ONBOARDING", "Ask the platform to bring my college on board"),
    ("LIST_ACADEMIC_RECORDS", "My semester results"),
    ("CREATE_ACADEMIC_RECORD", "Add or replace a semester result (with subjects and marksheet)"),
    ("UPDATE_ACADEMIC_RECORD", "Correct a semester result (new version if already verified)"),
    ("DEACTIVATE_ACADEMIC_RECORD", "Remove a semester result while no request is in progress"),
    ("LIST_ENROLMENT_VERIFICATIONS", "Students waiting for enrolment verification"),
    ("DECIDE_ENROLMENT", "Confirm or reject that a student is enrolled at the college"),
    ("GET_STUDENT_FOR_STAFF", "A student's profile and records (institutions involved only)"),
    # ---- Support and data requests
    ("CREATE_SUPPORT_TICKET", "Raise a support ticket or grievance"),
    ("LIST_MY_SUPPORT_TICKETS", "My support tickets"),
    ("GET_SUPPORT_TICKET", "A ticket with its replies"),
    ("REPLY_SUPPORT_TICKET", "Reply on a ticket"),
    ("LIST_SUPPORT_QUEUE", "Support queue (Platform Admin)"),
    ("UPDATE_SUPPORT_TICKET", "Change a ticket's state or assignee"),
    ("EXPORT_MY_DATA", "Download everything the platform holds about me (JSON)"),
    ("CREATE_DATA_REQUEST", "Request data download, correction or erasure"),
    ("LIST_MY_DATA_REQUESTS", "My data protection requests"),
    ("LIST_DATA_REQUESTS", "All data protection requests (Platform Admin)"),
    ("COMPLETE_DATA_REQUEST", "Fulfil or reject a data protection request"),
    ("WITHDRAW_CONSENT", "Withdraw consent: open requests are withdrawn and the account closes"),
    # ---- Vacancies and interview hours
    ("UPSERT_VACANCY", "Upload or correct vacancies for an offering, year and entry semester, by category"),
    ("LIST_VACANCIES", "Vacancies (filter by college, course, offering, year)"),
    ("SET_INTERVIEW_WORKING_HOURS", "Replace the college's interview days and hours"),
    ("LIST_INTERVIEW_WORKING_HOURS", "The college's interview days and hours"),
])


class BaseConfig:
    API_CODES = API_CODES  # defined above; one unique code per API
    # ------------------------------------------------------------------ app
    APP_NAME = "Student Migration System"
    APP_ENV = "base"
    SECRET_KEY = _env("SECRET_KEY", "change-me-in-env")
    DEBUG = False
    TESTING = False
    JSON_SORT_KEYS = False
    API_PREFIX = "/api/v1"
    FRONTEND_BASE_URL = _env("FRONTEND_BASE_URL", "http://localhost:5173")
    PUBLIC_BASE_URL = _env("PUBLIC_BASE_URL", "http://localhost:5000")
    CORS_ORIGINS = _env("CORS_ORIGINS", "http://localhost:5173").split(",")
    DISPLAY_TIMEZONE = "Asia/Kolkata"  # stored in UTC, shown in IST
    MAX_CONTENT_LENGTH = 60 * 1024 * 1024  # hard request cap (bytes)

    # ------------------------------------------------------------- database
    DB_USER = "postgres"
    DB_PASSWORD = "root"
    DB_HOST = "localhost"
    DB_PORT = 5432
    DB_NAME = "sms"
    SQLALCHEMY_TRACK_MODIFICATIONS = False
    SQLALCHEMY_ENGINE_OPTIONS = {
        "pool_size": _env("DB_POOL_SIZE", 10, int),
        "max_overflow": _env("DB_MAX_OVERFLOW", 20, int),
        "pool_pre_ping": True,
        "pool_recycle": 1800,
    }

    @classmethod
    def database_uri(cls):
        explicit = _env("DATABASE_URL")
        if explicit:
            return explicit
        auth = cls.DB_USER + (f":{cls.DB_PASSWORD}" if cls.DB_PASSWORD else "")
        host = cls.DB_HOST
        if host.startswith("/"):  # unix socket directory
            return f"postgresql+psycopg2://{auth}@/{cls.DB_NAME}?host={host}&port={cls.DB_PORT}"
        return f"postgresql+psycopg2://{auth}@{host}:{cls.DB_PORT}/{cls.DB_NAME}"

    # ----------------------------------------------------------- auth / JWT
    JWT_SECRET_KEY = _env("JWT_SECRET_KEY", "change-me-jwt")
    JWT_ALGORITHM = "HS256"
    JWT_ACCESS_TOKEN_EXPIRES = timedelta(hours=_env("JWT_ACCESS_HOURS", 8, int))
    JWT_PRE_AUTH_TOKEN_EXPIRES = timedelta(minutes=10)  # between password and OTP step
    SINGLE_ACTIVE_SESSION = True  # new login ends the previous session (M01-F05)
    SESSION_IDLE_TIMEOUT = timedelta(hours=8)

    MAX_FAILED_LOGIN_ATTEMPTS = 3  # M01-F09
    LOGIN_LOCKOUT_DURATION = timedelta(minutes=15)
    MAX_LOCKOUTS_PER_DAY = 3  # then locked until password reset

    PASSWORD_MIN_LENGTH = 8
    PASSWORD_REQUIRE_UPPER = True
    PASSWORD_REQUIRE_LOWER = True
    PASSWORD_REQUIRE_DIGIT = True
    PASSWORD_REQUIRE_SYMBOL = True
    PASSWORD_HISTORY_COUNT = 3

    PASSWORD_RESET_TOKEN_EXPIRES = timedelta(minutes=30)
    OTP_LENGTH = 6
    OTP_EXPIRES = timedelta(minutes=10)
    OTP_MAX_ATTEMPTS = 5
    TWO_FACTOR_REQUIRED_FOR_ADMINS = True  # 12.3
    EXPOSE_DEBUG_OTP = False  # dev/test only: echo OTPs and tokens in API responses

    PERMISSION_CACHE_SECONDS = 60  # INT-11

    # ------------------------------------------------------- invitations
    INVITATION_EXPIRES = timedelta(days=7)

    # ------------------------------------------------------- rate limits
    RATELIMIT_STORAGE_URI = _env("RATELIMIT_STORAGE_URI", "memory://")
    RATELIMIT_DEFAULT = "300 per minute"
    RATELIMIT_LOGIN = "20 per minute"
    RATELIMIT_REGISTRATION = "10 per hour"
    SUSPICIOUS_FAILED_LOGINS_PER_IP = 10  # M25-F13
    SUSPICIOUS_WINDOW = timedelta(minutes=10)
    SUSPICIOUS_IP_BLOCK = timedelta(hours=1)

    # -------------------------------------------------- migration workflow
    MAX_ACTIVE_REQUESTS_PER_STUDENT = 3
    NOC_DECISION_DAYS = 7
    OFFER_ACCEPTANCE_DAYS = 7
    RELEASE_DEADLINE_DAYS = 15
    RELEASE_REMINDER_DAYS = (7, 12)
    APPEAL_WINDOW_DAYS = 7
    APPEAL_DECISION_DAYS = 15
    ORIGINALS_REPORTING_DAYS = 15
    OFFER_EXPIRY_REMINDER_HOURS = 48
    INTERVIEW_REMINDER_HOURS = (24, 2)
    INTERVIEW_RESCHEDULE_MIN_HOURS = 24
    MAX_STUDENT_RESCHEDULES = 1
    MAX_NO_SHOWS = 2
    REFERENCE_PREFIX = "SMS"

    # ----------------------------------------------------------- documents
    STORAGE_BACKEND = _env("STORAGE_BACKEND", "local")  # local | s3
    STORAGE_LOCAL_ROOT = _env("STORAGE_LOCAL_ROOT", os.path.join(BASE_DIR, "storage"))
    S3_BUCKET = _env("S3_BUCKET")
    S3_REGION = _env("S3_REGION", "ap-south-1")
    SIGNED_URL_EXPIRES = timedelta(minutes=15)
    MAX_FILE_SIZE_BYTES = 5 * 1024 * 1024
    MAX_REQUEST_DOCUMENTS_BYTES = 50 * 1024 * 1024
    ALLOWED_FILE_EXTENSIONS = {"pdf", "jpg", "jpeg", "png", "docx"}
    ALLOWED_MIME_TYPES = {
        "application/pdf",
        "image/jpeg",
        "image/png",
        "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    }
    IMAGE_MAX_DIMENSION = 2000  # px, images are downscaled above this
    VIRUS_SCAN_ENABLED = _env("VIRUS_SCAN_ENABLED", False, bool)
    CLAMAV_HOST = _env("CLAMAV_HOST", "localhost")
    CLAMAV_PORT = _env("CLAMAV_PORT", 3310, int)

    # ---------------------------------------------------------- retention
    RETENTION_NOT_ADMITTED_DAYS = 365
    RETENTION_ADMITTED_DAYS = 365 * 5
    ACTIVITY_LOG_RETENTION_DAYS = 365  # DPDP Rule 6 minimum

    # ------------------------------------------------------- notifications
    NOTIFICATION_CHANNELS_ENABLED = {"EMAIL": True, "SMS": True, "WHATSAPP": True, "IN_APP": True}
    NOTIFICATION_RETRY_DELAYS_SECONDS = (60, 300, 1800)
    NOTIFICATION_FALLBACK_CHANNEL = "EMAIL"
    NOTIFICATIONS_SEND_ASYNC = _env("NOTIFICATIONS_SEND_ASYNC", True, bool)

    EMAIL_BACKEND = _env("EMAIL_BACKEND", "console")  # console | smtp
    SMTP_HOST = _env("SMTP_HOST", "email-smtp.ap-south-1.amazonaws.com")
    SMTP_PORT = _env("SMTP_PORT", 587, int)
    SMTP_USERNAME = _env("SMTP_USERNAME")
    SMTP_PASSWORD = _env("SMTP_PASSWORD")
    SMTP_USE_TLS = True
    MAIL_FROM = _env("MAIL_FROM", "no-reply@example.in")

    SMS_BACKEND = _env("SMS_BACKEND", "console")  # console | msg91
    MSG91_AUTH_KEY = _env("MSG91_AUTH_KEY")
    MSG91_SENDER_ID = _env("MSG91_SENDER_ID", "SMSMIG")
    MSG91_FLOW_URL = "https://control.msg91.com/api/v5/flow/"
    MSG91_TEMPLATE_IDS = {}  # event -> DLT-registered template id

    WHATSAPP_BACKEND = _env("WHATSAPP_BACKEND", "console")  # console | cloud_api
    WHATSAPP_API_URL = _env("WHATSAPP_API_URL", "https://graph.facebook.com/v20.0")
    WHATSAPP_PHONE_NUMBER_ID = _env("WHATSAPP_PHONE_NUMBER_ID")
    WHATSAPP_ACCESS_TOKEN = _env("WHATSAPP_ACCESS_TOKEN")
    WHATSAPP_TEMPLATE_NAMES = {}  # event -> Meta-approved template name

    # --------------------------------------------------------- integrations
    EXTERNAL_API_TIMEOUT_SECONDS = 10
    EXTERNAL_API_MAX_RETRIES = 3

    INTERVIEW_URL_SCHEMES = ("https",)  # meeting links must be https (any platform)

    ABC_INTEGRATION_ENABLED = False  # v1 is manual upload (M26-F04)

    # ------------------------------------------------------- background work
    # Notifications are sent in background threads inside the app process.
    NOTIFICATION_WORKER_THREADS = _env("NOTIFICATION_WORKER_THREADS", 4, int)
    # Scheduled jobs run inside the app (APScheduler). One process runs each tick
    # (PostgreSQL advisory lock), however many app processes there are.
    SCHEDULER_ENABLED = _env("SCHEDULER_ENABLED", True, bool)
    JOB_SCHEDULE_MINUTES = {
        "retry_notifications": 1,
        "expire_offers": 60,
        "release_overdue_check": 60,
        "noc_escalation_check": 60,
        "merit_rounds": 30,
        "interview_reminders": 15,
        "offer_reminders": 60,
        "originals_deadline_check": 60,
        "retention_purge": 1440,
    }

    # ------------------------------------------------------------- imports
    # Column names in the India PIN code list (scripts/load_pincodes.py). Matching ignores
    # case, spaces and punctuation. (AISHE files are read by scripts/load_universities.py.)
    PINCODE_COLUMN_MAP = {
        "pincode": ["pincode", "Pincode", "PIN"],
        "locality": ["officename", "Office Name", "locality"],
        "district": ["district", "District", "Districtname"],
        "state": ["statename", "State", "StateName"],
    }
    BULK_UPLOAD_MAX_ROWS = 5000

    # ----------------------------------------------------------- pagination
    DEFAULT_PAGE_SIZE = 20
    MAX_PAGE_SIZE = 100

    # -------------------------------------------------------------- logging
    LOG_LEVEL = _env("LOG_LEVEL", "INFO")
    SENSITIVE_FIELDS = {
        "password", "new_password", "current_password", "confirm_password",
        "otp", "token", "access_token", "refresh_token", "secret",
        "aadhaar", "aadhaar_number", "file", "authorization",
    }

    # ------------------------------------------------------- letters / misc
    LETTER_VERIFY_PATH = "/verify/letter/"
    SUPPORT_FIRST_RESPONSE_HOURS = 24
    DATA_REQUEST_RESPONSE_DAYS = 30
    MIN_PLATFORM_ADMINS = 2
    CONSENT_NOTICE_VERSION = "2026-10-v1"
    AGE_OF_MAJORITY = 18


class DevelopmentConfig(BaseConfig):
    APP_ENV = "development"
    DEBUG = True
    EXPOSE_DEBUG_OTP = True
    DB_NAME = "sms"


class TestingConfig(BaseConfig):
    APP_ENV = "testing"
    TESTING = True
    EXPOSE_DEBUG_OTP = True
    DB_NAME = _env("TEST_DB_NAME", "sms_test")
    NOTIFICATIONS_SEND_ASYNC = False
    SCHEDULER_ENABLED = False
    RATELIMIT_ENABLED = False
    TWO_FACTOR_REQUIRED_FOR_ADMINS = True
    STORAGE_LOCAL_ROOT = os.path.join(BASE_DIR, ".test_storage")
    EMAIL_BACKEND = "console"
    SMS_BACKEND = "console"
    WHATSAPP_BACKEND = "console"


class ProductionConfig(BaseConfig):
    APP_ENV = "production"
    DEBUG = False
    EMAIL_BACKEND = _env("EMAIL_BACKEND", "smtp")
    SMS_BACKEND = _env("SMS_BACKEND", "msg91")
    WHATSAPP_BACKEND = _env("WHATSAPP_BACKEND", "cloud_api")
    VIRUS_SCAN_ENABLED = _env("VIRUS_SCAN_ENABLED", True, bool)
    # memory:// limits per process; point this at Redis (and pip install redis) to share limits.
    RATELIMIT_STORAGE_URI = _env("RATELIMIT_STORAGE_URI", "memory://")


CONFIG_BY_NAME = {
    "development": DevelopmentConfig,
    "testing": TestingConfig,
    "production": ProductionConfig,
}


def get_config(name=None):
    name = name or _env("APP_ENV", "development")
    return CONFIG_BY_NAME[name]
