import re
import secrets
import string
from datetime import datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo

from flask import current_app

IST = ZoneInfo("Asia/Kolkata")


def utcnow():
    return datetime.now(timezone.utc)


def ensure_aware(dt):
    if dt is None:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def deadline_after_days(days, start=None):
    """Calendar-day deadline ending 11:59 pm IST on the last day (12.1)."""
    start = ensure_aware(start or utcnow()).astimezone(IST)
    last_day = (start + timedelta(days=days)).date()
    return datetime.combine(last_day, time(23, 59, 59), tzinfo=IST).astimezone(timezone.utc)


def today_ist():
    return utcnow().astimezone(IST).date()


def normalize_name(value):
    value = (value or "").lower()
    value = re.sub(r"[^a-z0-9]+", " ", value)
    return re.sub(r"\s+", " ", value).strip()


def reference_number(prefix=None):
    prefix = prefix or current_app.config["REFERENCE_PREFIX"]
    alphabet = string.ascii_uppercase + string.digits
    suffix = "".join(secrets.choice(alphabet) for _ in range(8))
    return f"{prefix}-{utcnow().astimezone(IST).year}-{suffix}"


def email_domain(email):
    return (email or "").rsplit("@", 1)[-1].lower()


AISHE_PATTERNS = {
    "UNIVERSITY": re.compile(r"^U-\d{1,6}$"),
    "COLLEGE": re.compile(r"^C-\d{1,6}$"),
    "STANDALONE": re.compile(r"^S-\d{1,6}$"),
    "R&D": re.compile(r"^R-\d{1,6}$"),
}


def valid_aishe(code, institution_type):
    pattern = AISHE_PATTERNS[str(institution_type)]
    return bool(pattern.match((code or "").strip().upper()))


def valid_pincode(pin):
    return bool(re.fullmatch(r"[1-9]\d{5}", pin or ""))


def valid_abc_id(value):
    return bool(re.fullmatch(r"\d{12}", value or ""))


def valid_mobile(value):
    return bool(re.fullmatch(r"(\+91)?[6-9]\d{9}", value or ""))


def age_on(dob, on=None):
    on = on or today_ist()
    return on.year - dob.year - ((on.month, on.day) < (dob.month, dob.day))
