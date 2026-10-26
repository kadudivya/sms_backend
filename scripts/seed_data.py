"""Seed the data the system needs before first use. Run it by hand, once per environment
(it is safe to run again: existing rows are left alone).

    python scripts/seed_data.py --admin ops1@yourco.in "Anita Rao" --admin ops2@yourco.in "Vivek Shah"

What it creates
* Platform Admins: at least two are required (BRD: no single point of failure).
  You are asked for each new admin's password, or use --generate-passwords to print
  a random one per admin, which they change after first sign-in.
* The built-in letter templates (NOC, offer, admission ...) as editable platform defaults.

The tables themselves are created by the app when it starts (db.create_all), and this
script also creates them if they do not exist yet. Universities and colleges are loaded
separately with scripts/load_universities.py, PIN codes with scripts/load_pincodes.py.
"""
import argparse
import getpass
import os
import secrets
import string
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def _password(email, generate):
    from app.core.errors import ValidationError
    from app.core.security import validate_password_policy
    if generate:
        alphabet = string.ascii_letters + string.digits
        while True:
            pw = "".join(secrets.choice(alphabet) for _ in range(14)) + secrets.choice("!@#$%&*") + secrets.choice(string.digits)
            try:
                validate_password_policy(pw)
                return pw
            except ValidationError:
                continue
    while True:
        pw = getpass.getpass(f"Password for {email}: ")
        if pw != getpass.getpass("Repeat the password: "):
            print("The passwords do not match. Try again.")
            continue
        try:
            validate_password_policy(pw)
            return pw
        except ValidationError as exc:
            print(exc.message)


def seed_platform_admins(admins, generate=False):
    from app.core.security import hash_password
    from app.core.utils import utcnow
    from app.enums import AccountState, UserType
    from app.extensions import db
    from app.models import User
    created = []
    for email, name in admins:
        email = email.strip().lower()
        if User.query.filter(db.func.lower(User.email) == email).first():
            print(f"exists   {email}")
            continue
        pw = _password(email, generate)
        db.session.add(User(email=email, full_name=name.strip(), password_hash=hash_password(pw),
                            password_history=[], password_changed_at=utcnow(), user_type=UserType.PLATFORM_ADMIN,
                            account_state=AccountState.ACTIVE, email_verified_at=utcnow()))
        db.session.commit()
        created.append(email)
        print(f"created  {email}" + (f"   temporary password: {pw}" if generate else ""))
    return created


def seed_letter_templates():
    from app.extensions import db
    from app.models import LetterTemplate
    from app.services.letters import DEFAULT_TEMPLATES
    added = 0
    for letter_type, (subject, body) in DEFAULT_TEMPLATES.items():
        if not LetterTemplate.query.filter_by(letter_type=letter_type, university_id=None).first():
            db.session.add(LetterTemplate(letter_type=letter_type, university_id=None, subject=subject, body=body))
            added += 1
    db.session.commit()
    print(f"letter templates: {added} added, {len(DEFAULT_TEMPLATES) - added} already present")
    return added


def main(argv=None):
    parser = argparse.ArgumentParser(description="Seed platform admins and letter templates.")
    parser.add_argument("--admin", nargs=2, action="append", default=[], metavar=("EMAIL", "FULL_NAME"),
                        help="A Platform Admin to create (repeat for each one)")
    parser.add_argument("--generate-passwords", action="store_true",
                        help="Create random temporary passwords and print them, instead of asking")
    parser.add_argument("--env", default=os.environ.get("APP_ENV", "development"),
                        help="Config to use: development, testing or production (default: APP_ENV)")
    args = parser.parse_args(argv)

    from app import create_app
    from app.enums import UserType
    from app.models import User
    app = create_app(args.env, start_scheduler=False)
    with app.app_context():
        seed_platform_admins(args.admin, args.generate_passwords)
        seed_letter_templates()
        total = User.query.filter_by(user_type=UserType.PLATFORM_ADMIN).count()
        minimum = app.config["MIN_PLATFORM_ADMINS"]
        if total < minimum:
            print(f"WARNING: {total} Platform Admin(s); at least {minimum} are required. "
                  "Run again with another --admin.")


if __name__ == "__main__":
    main()
