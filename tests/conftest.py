import io
import os

import pytest

os.environ.setdefault("APP_ENV", "testing")

from app import create_app  # noqa: E402
from app.extensions import db as _db  # noqa: E402

PASSWORD = "Str0ng!Passw0rd"


@pytest.fixture(scope="session")
def app():
    app = create_app("testing")
    with app.app_context():
        _db.drop_all()
        _db.create_all()
        yield app
        _db.session.remove()


@pytest.fixture(autouse=True)
def clean_db(app):
    yield
    _db.session.rollback()
    tables = ", ".join(f'"{t.name}"' for t in reversed(_db.metadata.sorted_tables))
    _db.session.execute(_db.text(f"TRUNCATE {tables} RESTART IDENTITY CASCADE"))
    _db.session.commit()
    _db.session.remove()


@pytest.fixture()
def client(app):
    return app.test_client()


class Api:
    """Thin helper around the test client that understands the envelope."""

    def __init__(self, client, prefix="/api/v1"):
        self.client = client
        self.prefix = prefix
        self.token = None

    def _headers(self, token=None):
        tok = token or self.token
        return {"Authorization": f"Bearer {tok}"} if tok else {}

    def call(self, method, path, json=None, data=None, token=None, expect=None, **kw):
        fn = getattr(self.client, method)
        kwargs = {"headers": self._headers(token)}
        if json is not None:
            kwargs["json"] = json
        if data is not None:
            kwargs["data"] = data
            kwargs["content_type"] = "multipart/form-data"
        resp = fn(self.prefix + path, **kwargs, **kw)
        body = resp.get_json(silent=True)
        if expect is not None:
            assert resp.status_code == expect, (resp.status_code, body)
        return resp, body

    def get(self, path, **kw):
        return self.call("get", path, **kw)

    def post(self, path, **kw):
        return self.call("post", path, **kw)

    def put(self, path, **kw):
        return self.call("put", path, **kw)

    def patch(self, path, **kw):
        return self.call("patch", path, **kw)


@pytest.fixture()
def api(client):
    return Api(client)


def pdf_file(name="doc.pdf"):
    return (io.BytesIO(b"%PDF-1.4\n% test file\n1 0 obj<<>>endobj\ntrailer<<>>\n%%EOF"), name)


def png_file(name="seal.png"):
    from PIL import Image
    buf = io.BytesIO()
    Image.new("RGB", (40, 40), (200, 20, 20)).save(buf, format="PNG")
    buf.seek(0)
    return (buf, name)


def make_platform_admin(email="pa@platform.in", name="Platform Admin"):
    from app.core.security import hash_password
    from app.core.utils import utcnow
    from app.enums import AccountState, UserType
    from app.models import User
    user = User(email=email, full_name=name, password_hash=hash_password(PASSWORD), user_type=UserType.PLATFORM_ADMIN,
                account_state=AccountState.ACTIVE, email_verified_at=utcnow())
    _db.session.add(user)
    _db.session.commit()
    return user


def login(api, email, password=PASSWORD):
    """Full login including the 2FA step when required. Returns the access token."""
    _, body = api.post("/auth/login", json={"email": email, "password": password}, expect=200)
    data = body["data"]
    if data.get("two_factor_required"):
        _, body = api.post("/auth/login/verify-otp", json={"otp": data["debug_otp"]},
                           token=data["pre_auth_token"], expect=200)
        data = body["data"]
    return data["access_token"]


def seed_geo():
    from app.models import District, Pincode, State
    mh = State(name="Maharashtra")
    _db.session.add(mh)
    _db.session.flush()
    pune = District(state_id=mh.id, name="Pune")
    mumbai = District(state_id=mh.id, name="Mumbai")
    _db.session.add_all([pune, mumbai])
    _db.session.flush()
    _db.session.add(Pincode(pincode="411007", locality="Ganeshkhind", district_id=pune.id, state_id=mh.id))
    _db.session.commit()
    return mh, pune, mumbai


def seed_master_institution(code, name, itype, parent=None, district=None):
    """A row as load_universities.py would leave it. itype: UNIVERSITY, or a colleges.institution_type
    (COLLEGE / STANDALONE / PM_VIDYALAXMI / R&D)."""
    from app.models import College, University
    common = dict(aishe_code=code, name=name, state_id=district.state_id, district_id=district.id, pincode="411007")
    if itype == "UNIVERSITY":
        inst = University(**common)
    else:
        inst = College(institution_type=itype, university_id=parent.id if parent else None, **common)
    _db.session.add(inst)
    _db.session.commit()
    return inst
