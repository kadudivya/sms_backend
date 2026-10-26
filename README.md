# Student Migration System: backend (phase 1)

Flask + SQLAlchemy + PostgreSQL API for the Student Migration System BRD v1.0.
208 APIs (codes in `config.py`), 50 tables (all in `app/models.py`), in-app scheduled jobs, 42 tests against PostgreSQL.
No Celery, no Redis, no Docker files, no Flask CLI commands.

## Quick start

```bash
cp .env.example .env                 # database settings and secrets
pip install -r requirements-dev.txt
createdb sms_dev                     # an empty PostgreSQL database

# 1. Start the app once: it creates every table and enum type itself (db.create_all).
gunicorn --preload -w 4 -b 0.0.0.0:5000 wsgi:app   # or, for development: python wsgi.py

# 2. Seed what the system needs (run by hand; safe to repeat).
python scripts/seed_data.py --admin ops1@yourco.in "Anita Rao" --admin ops2@yourco.in "Vivek Shah"

# 3. Load universities and colleges from the AISHE files (your loader, run by hand).
#    Set DATABASE_URL at the top of the script, then run it from the folder with the 5 .xlsx files.
cd data/aishe && python ../../scripts/load_universities.py && cd ../..

# 4. Load PIN codes (after step 3: PINs are linked to the AISHE states and districts).
python scripts/load_pincodes.py data/pincodes.csv
```

Order matters: start the app before running `load_universities.py`, so `db.create_all()` builds the full `universities` and `colleges` tables (the loader's columns plus the app's). If the loader runs first on an empty database, its own `create_all` makes the tables with only its columns, and the app will not add the missing ones.

The API is at `http://localhost:5000/api/v1`. Keep `--preload` with several workers: the app (and `db.create_all()`) then starts once before gunicorn forks the workers. Without it, four workers each try to create the tables on an empty database and collide.

Tests: create a `sms_test` database, then run `pytest` (42 tests, about 90 seconds).

## Project layout

```
config.py                  ALL configuration (secrets come from env vars)
app/__init__.py            app factory, request hooks, error handlers
app/enums.py               every enum (stored as PostgreSQL enums)
app/models.py              all 50 tables in one plain file; each extends db.Model and has to_dict()
app/core/                  api decorator + registry, activity log, auth, permissions,
                           storage, security, responses, validation
app/services/              business logic (one module per area)
app/api/                   blueprints: thin routes, marshmallow validation
app/core/records.py        active(), is_active(), activate(), deactivate() and small row helpers
app/core/institutions.py   helpers for the two institution tables (universities / colleges)
app/core/audit.py          fills created_by_id / updated_by_id with the signed-in user
app/core/json.py           JSON for responses (ISO dates, Decimal as numbers)
app/core/scheduler.py      in-app job scheduler (APScheduler + PostgreSQL advisory locks)
app/core/background.py     in-app thread pool that sends notifications
app/services/jobs.py       the scheduled jobs
scripts/seed_data.py       seeds Platform Admins and letter templates (run by hand)
scripts/load_universities.py your AISHE loader: states, districts, universities, colleges (run by hand)
scripts/load_pincodes.py   loads the India PIN code list (run by hand, after the AISHE loader)
tests/                     end-to-end tests through the real APIs
```

## Conventions you asked for

**Enums.** Every fixed value set is a Python enum in `app/enums.py`, stored as a native PostgreSQL enum via `enum_column()`. The database rejects anything else.

**Plain models.** `app/models.py` has no base class, mixins, properties or event listeners. Every table extends `db.Model` directly and declares all of its columns, including the shared ones: `id`, `uuid`, `status` (ACTIVE / INACTIVE), `deleted_at`, `created_at`, `updated_at`, `created_by_id`, `updated_by_id`. Enums are plain `db.Enum(...)` columns, which become native PostgreSQL enum types. Timestamps use `default=lambda: datetime.now(timezone.utc)` directly in the column. Each model has a `to_dict()` that returns every column, with enums as their `.value` (password hashes and token hashes are never included). Relationships (`request.student`, `membership.university`, `college.university` …) are kept. `migration_status_history` and `user_activity` are append-only logs, so they carry only their own columns.

**Status and `deleted_at`.** Change status only with `deactivate(row)` / `activate(row)` from `app/core/records.py`. `deactivate` sets INACTIVE and fills `deleted_at`; `activate` sets ACTIVE and clears it. Query live rows with `active(Model)`. Workflow states live in their own columns (`request_status`, `onboarding_status`, `approval_status` …), never in `status`.

**`created_by_id` / `updated_by_id`** are filled from the signed-in user by one database-session hook in `app/core/audit.py`, which is kept out of the models. In scripts and scheduled jobs there is no signed-in user, so they stay empty.

**All configuration in `config.py`.** Deadlines, limits, retry delays, file sizes, channels, schedules, column maps. Secrets are read from environment variables inside `config.py` and never written in it.

**Unique API codes, defined in `config.py`.** `config.API_CODES` lists every API's code and description, grouped by area: `("CREATE_STUDENT", "Register a student ...")`. A route only names its code: `@api("CREATE_STUDENT", auth=False)`. The app refuses to start if a code appears twice in config, if a route uses a code that isn't in config, if two routes use the same code, or if a code in config has no route. So config and the routes can't drift apart. Nothing is generated: to add an API, add its code to `config.API_CODES`, then decorate the route. The code is returned in every response (`api_code`), in the `X-Api-Code` header, and stored in the activity log. `GET /api/v1/system/api-codes` lists them with their paths.

**Tables are created when the app starts.** `create_app()` imports the models and runs `db.create_all()` in the app context. `create_all` only adds missing tables and enum types; it never changes or drops existing ones, so a later change to an existing column needs an `ALTER TABLE` by hand. With several gunicorn workers, start with `--preload` (see Quick start).

**Background work inside the app.** Notifications are sent after each commit by a small thread pool inside the app (`NOTIFICATION_WORKER_THREADS`). Failed sends are retried by the `retry_notifications` job. Scheduled jobs run inside the app with APScheduler (`SCHEDULER_ENABLED`, `JOB_SCHEDULE_MINUTES`). Each run takes a PostgreSQL advisory lock, so with 4 gunicorn workers each job still runs in only one of them per tick.

**No Flask CLI commands.** Seeding and data loading are plain Python scripts in `scripts/`, run by hand.

**One activity row per API call.** Code records major steps with `activity.step("...")` and field changes with `activity.change(...)`. After the response is built, exactly one `user_activity` row is written on a separate connection, so it survives a rolled-back request. The row holds: request id, API code, user, session, university or college, IP, user agent, the request payload with secrets masked, the ordered list of steps, the field changes, outcome, HTTP status, error code, duration, and `created_by`, `updated_by`, `created_at`, `updated_at`.

**Sessions in one table.** `user_sessions`: one row per login. Its `uuid` is the `sid` claim in the JWT and is checked on every request. A new login ends the previous session (single active session). An ended session is INACTIVE, so `deleted_at` is the moment it ended, and `end_reason` says why.

**Students** have `abc_account_id` (12 digits, unique) on `student_profiles`.

## Universities and colleges (two tables)

`State`, `District`, `University` and `College` in `app/models.py` are the models from `scripts/load_universities.py`, column for column. `University` and `College` also carry the columns the app needs (`uuid`, `status`, `deleted_at`, `created_at` …, `onboarding_status`, `source`, address and PIN, `email_domains`, letter branding, and on universities `release_unfilled_reserved_to_open`). Every one of those has a server default or allows NULL, so the loader's inserts work unchanged. `states` and `districts` are exactly the loader's: `id` and `name` (plus `state_id`).

- **The colleges table holds four kinds of row**, in `colleges.institution_type` (plain text): `COLLEGE` (affiliated, `university_id` set), `STANDALONE`, `PM_VIDYALAXMI` and `R&D`. A college row with no `university_id` has no parent university: the Platform Admin approves its admin, and university staff have no rights over it. All four kinds can onboard, hold staff, send and receive students.
- **The same AISHE code can be in two files** (most PM Vidyalaxmi rows repeat a code from College or Standalone). Colleges are unique on `(institution_type, aishe_code)`. An AISHE lookup that finds several rows takes them in this order: COLLEGE, STANDALONE, R&D, PM_VIDYALAXMI (`LOOKUP_PREFERENCE` in `app/core/institutions.py`).
- **References that are always one kind** point straight at that table: courses, syllabi, grading schemes, academic years and letter templates at `universities`; course offerings, offering requests, interviews, interview hours, a student's current college and college onboarding requests at `colleges`. A migration request has `source_college_id`, `source_university_id` (empty for a college without a university), `destination_college_id` and `destination_university_id`.
- **References that can be either kind** have a `university_id` and a `college_id`, and exactly one is set: staff memberships (enforced by a check constraint), invitations, registration requests, letters, bulk upload jobs, documents (`uploaded_by_…`) and the activity log.
- **Routes that work for either kind** carry it in the URL: `/institutions/university/<id>` or `/institutions/college/<id>`, then `/members`, `/invitations`, `/access-requests`, `/transfer-primary-admin`, `/bulk-uploads`, `/suspend`, `/reactivate`, `/branding`. `GET /institutions?kind=university|college` lists one table (colleges also filter by `institution_type` and `university_id`). Affiliation is `PUT /colleges/<id>/affiliation`; the reserved-seat policy is `PUT /universities/<id>/reservation-policy`. The API codes did not change.
- **`GET /auth/me`** gives each membership `institution_kind` (UNIVERSITY / COLLEGE), `institution_id`, `institution_type` (UNIVERSITY, or the college row's type) and `parent_university_id`.
- **Onboarding still uses three registration types:** `UNIVERSITY`, `COLLEGE` (needs a parent university) and `STANDALONE` (a colleges row with no university). An institution added by hand goes into the matching table, with `source = MANUAL`.

## Loading the AISHE data

Done **outside the app**, by hand, with `scripts/load_universities.py` (your script, unchanged). Set `DATABASE_URL` at its top, put `University.xlsx`, `College.xlsx`, `Standalone.xlsx`, `pm_vidya_lakshmi.xlsx` and `RD.xlsx` in a folder and run the script from that folder. It prints the start time, end time and duration. With the current files it loads 36 states, 765 districts, 1,437 universities and 73,137 colleges in about 20 seconds.

- **Start the app first** (see Quick start), so the tables have the app's columns too.
- **Re-running is safe.** Rows are matched on their key and updated. Onboarding status, admins and everything else the app adds are left alone.
- **Leave `RESET_TABLES = False`.** Setting it to True drops `colleges`, `universities`, `districts` and `states` with `CASCADE`: that also drops every foreign key that points at them from the app's tables (memberships, courses, offerings, migration requests …), and the loader then recreates the four tables with only its own columns. If you ever need it, use it only on a fresh database, then drop and recreate the whole database and start again from step 1: restarting the app does not bring back the dropped columns and foreign keys.
- **States and districts no row uses are deleted** at the end of each run. PIN codes and student addresses that pointed at one keep their row with that link set to empty (`ON DELETE SET NULL`).

`scripts/load_pincodes.py data/pincodes.csv` then loads the PIN code list. Each PIN is linked to the AISHE state and district of the same name (ignoring case and punctuation); one whose state or district isn't found is still saved, unlinked, and counted. Each run writes a `master_import_batches` row, which Platform Admins see through `LIST_IMPORT_BATCHES` / `GET_IMPORT_BATCH`.

## Seed data

`python scripts/seed_data.py --admin EMAIL "FULL NAME" [--admin ...] [--generate-passwords]`

- **Platform Admins.** At least two are required. The script asks for each new admin's password (checked against the password policy). With `--generate-passwords` it prints a random temporary password for each instead.
- **Letter templates.** The built-in NOC, offer, admission and other letter templates are stored as editable platform defaults.

It is safe to run again: existing admins and templates are left alone. It uses `APP_ENV` (or `--env`) to pick the database. Everything else (courses, windows, vacancies, staff) is created through the app by the people responsible for it.

## Interviews

Colleges hold interviews on their own video platform: Zoom, Google Meet, Microsoft Teams or anything else. The system does not host video calls or record them.

- `SCHEDULE_INTERVIEW` takes `mode`. For `ONLINE` it needs `meeting_url`, a full `https://` link, and optionally `meeting_info` (meeting ID, passcode, instructions). For `IN_PERSON` it needs `location`. The database enforces that an online interview has a link.
- `UPDATE_INTERVIEW_MEETING_LINK` (`PUT /interviews/<id>/meeting-link`) changes the link or details of a scheduled online interview. It notifies the student and the panel.
- The student sees the link on the interview (`GET_INTERVIEW` / `LIST_INTERVIEWS`). It is also included in the scheduled, rescheduled, link-changed and reminder notifications.
- A student who moves the interview (once, at least 24 hours ahead) changes only the time. The mode and link stay as the college set them.
- Outcomes (passed / not selected) and no-shows are recorded in the system as before.

## Response format

```json
{"success": true, "api_code": "SUBMIT_MIGRATION_REQUEST", "request_id": "…",
 "message": "Request submitted", "data": {…}, "errors": [], "meta": {"page": 1, "total": 7}}
```

Errors use the same envelope with `success: false` and `errors: [{code, message, details}]`, for example `CONCURRENT_UPDATE`, `INVALID_STATUS_TRANSITION`, `REQUEST_INCOMPLETE`, `PERMISSION_DENIED`.

## Roles and permissions

Roles are derived: `users.user_type` (PLATFORM_ADMIN, STAFF, STUDENT) plus `institution_memberships` (ADMIN or USER of a university, or of a colleges row: college, standalone, PM Vidyalaxmi or R&D institute).

- Admins hold every permission of their level.
- Users hold permission bundles (`ENROLMENT_VERIFIER`, `DOCUMENT_VERIFIER`, `REVIEWER`, `CREDIT_ASSESSOR`, `INTERVIEWER`, `APPROVER`, `VACANCY_MANAGER`, `ACADEMIC_MANAGER`, `UNIVERSITY_APPROVER`, `REPORT_VIEWER`).
- Permissions are read from the database on every request, so revoking one takes effect immediately.
- Every service call also checks the institution scope.
- Two-factor login (email OTP) is mandatory for admins and for anyone holding an approval permission.

## How the first university gets in (BRD 4.1)

1. **Path A:** `CREATE_UNIVERSITY` (Platform Admin), then the invitee calls `ACCEPT_INVITATION`, then `VERIFY_EMAIL`, and becomes active immediately.
2. **Path B:** `SEND_UNIVERSITY_SIGNUP_LINK`, then `ACCEPT_INVITATION` with an authorization letter, then `VERIFY_EMAIL`, then `APPROVE_REGISTRATION`. The link carries a signed single-use token bound to the email and AISHE code, never `?aishe_code=` in plain text.
3. **Path C:** `SUBMIT_INSTITUTION_REGISTRATION` (public form, rate limited), then `APPROVE_REGISTRATION`.

Colleges follow the same paths with the University Admin as approver. Colleges rows with no university (standalone, PM Vidyalaxmi, R&D) are approved by the Platform Admin.

## Migration workflow

Statuses and allowed moves are in `services/migration.py: ALLOWED`. Every move writes `migration_status_history`. Competing decisions send `version`, and a stale version gets `CONCURRENT_UPDATE`.

```
DRAFT → SUBMITTED → NOC_GRANTED → UNDER_REVIEW (→ RETURNED_FOR_CORRECTION / INTERVIEW_SCHEDULED)
  → RECOMMENDED → ENTRY_APPROVED → PROVISIONAL_OFFER | WAITLISTED → OFFER_ACCEPTED
  → RELEASED (TC; plus migration certificate across universities) → ADMITTED_ORIGINALS_PENDING → ADMITTED
REJECTED → APPEALED (once) → back to the stage, or REJECTED
```

Seats are blocked at the offer and filled at admission, using row locks. The waitlist is ordered by merit, then by submission time. Merit-round courses get their offers when the window closes (`merit_rounds` job). Rolling courses get them as soon as entry is approved.

## Scheduled jobs

They run inside the app on the intervals in `config.JOB_SCHEDULE_MINUTES`. No worker or broker is needed:

| Job | What it does |
| --- | --- |
| `retry_notifications` | Every minute: retries failed sends that are due, and picks up rows left unsent by a restart |
| `expire_offers` | Expires offers past their deadline and frees the seats |
| `offer_reminders` | Sends offer-expiry reminders |
| `noc_escalation_check` | Moves NOCs undecided after 7 days to the source university |
| `release_overdue_check` | Release reminders at day 7 and day 12, then escalation |
| `originals_deadline_check` | Reminds students to report with original documents |
| `merit_rounds` | Runs merit rounds for closed windows |
| `interview_reminders` | Reminders 24 hours and 2 hours before interviews |
| `retention_purge` | Deletes files and logs past their retention period |

## Decisions and deviations from the BRD

- **Settings live in `config.py`, as you asked.** The BRD says the Platform Admin changes configurable values "without a code change". Here a change is an env or config edit plus a restart. `GET_SYSTEM_SETTINGS` shows the effective values.
- **No separate audit table.** Field-level old/new values are kept in `user_activity.changes`, plus `migration_status_history`. Both are append-only.
- **`created_by` / `updated_by` / `created_at` / `updated_at`** are on every model AND on each activity row.
- **Hindi:** the translation structure is in place (`notification_templates.TEMPLATES["hi"]`, `users.language`). The Hindi texts still need writing.
- **Not built yet (v2 or provider-dependent):**
  - ABC/DigiLocker API: v1 uses manual ABC statement upload, per the BRD.
  - DSC signing.
  - S3: the backend class exists; `boto3` is commented out in requirements.
  - ClamAV scanning: built in, off unless `VIRUS_SCAN_ENABLED`.
- **Real providers:** SMS (MSG91) and WhatsApp need DLT and Meta template IDs in `MSG91_TEMPLATE_IDS` / `WHATSAPP_TEMPLATE_NAMES`. Until they are filled in, those channels fail and fall back to email.
- **Pending admins** can log in but only reach the APIs marked `allow_pending`: their profile, status, tickets and preferences.
