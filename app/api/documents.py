import io

from flask import Blueprint, g, request, send_file
from marshmallow import Schema, fields, validate

from app.core.api import api
from app.core.errors import ValidationError
from app.core.responses import created, ok
from app.core.storage import get_storage, resolve_download_token
from app.core.validation import load
from app.enums import DocumentOwnerType, DocumentType, Permission, Role, VerificationStatus
from app.extensions import db
from app.services import documents as svc
from app.services.migration import STUDENT_UPLOADABLE

bp = Blueprint("documents", __name__, url_prefix="")
EVERYONE = [Role.PLATFORM_ADMIN, Role.UNIVERSITY_ADMIN, Role.UNIVERSITY_USER, Role.COLLEGE_ADMIN,
            Role.COLLEGE_USER, Role.STUDENT]


class VerifySchema(Schema):
    decision = fields.String(required=True, validate=validate.OneOf(
        [VerificationStatus.VERIFIED.value, VerificationStatus.REJECTED.value,
         VerificationStatus.CORRECTION_REQUESTED.value]))
    reason = fields.String(validate=validate.Length(max=500))


def _doc_view(d):
    return d.to_dict(exclude=("storage_key", "checksum_sha256"))


@bp.post("/documents/mine")
@api("UPLOAD_PROFILE_DOCUMENT",
     roles=[Role.STUDENT])
def upload_mine():
    doc_type = request.form.get("doc_type")
    if doc_type not in {t.value for t in STUDENT_UPLOADABLE}:
        raise ValidationError("Choose a document type", details={"allowed": sorted(t.value for t in STUDENT_UPLOADABLE)})
    replace = None
    if request.form.get("replace_document_id"):
        replace = svc.get_document(int(request.form["replace_document_id"]))
        if replace.owner_type != DocumentOwnerType.USER or replace.owner_id != g.current_user.id:
            raise ValidationError("You can only replace your own documents")
    doc = svc.store_upload(request.files.get("file"), DocumentOwnerType.USER, g.current_user.id,
                           DocumentType(doc_type), replace_document=replace,
                           description=request.form.get("description"))
    db.session.commit()
    return created(_doc_view(doc), "Document uploaded")


@bp.get("/documents/mine")
@api("LIST_MY_DOCUMENTS", roles=[Role.STUDENT])
def list_mine():
    return ok([_doc_view(d) for d in svc.student_documents(g.current_user.id)])


@bp.get("/documents/<int:doc_id>")
@api("GET_DOCUMENT", roles=EVERYONE)
def get_document(doc_id):
    doc = svc.get_document(doc_id)
    return ok({**_doc_view(doc), "download_url": svc.download_link(g.actor, doc)})


@bp.get("/documents/<int:doc_id>/versions")
@api("LIST_DOCUMENT_VERSIONS", roles=EVERYONE)
def versions(doc_id):
    doc = svc.get_document(doc_id)
    svc.require_access(g.actor, doc)
    from app.models import Document
    rows = Document.query.filter_by(group_uuid=doc.group_uuid).order_by(Document.version_no.desc()).all()
    return ok([_doc_view(d) for d in rows])


@bp.post("/documents/<int:doc_id>/verify")
@api("VERIFY_DOCUMENT", permissions=[Permission.VERIFY_DOCUMENTS])
def verify(doc_id):
    data = load(VerifySchema)
    doc = svc.verify_document(g.actor, svc.get_document(doc_id), data["decision"], data.get("reason"))
    db.session.commit()
    return ok(_doc_view(doc), "Decision recorded")


@bp.get("/files/download")
@api("DOWNLOAD_FILE", auth=False)
def download():
    key, filename, mime = resolve_download_token(request.args.get("token", ""))
    data = get_storage().read(key)
    return send_file(io.BytesIO(data), mimetype=mime, as_attachment=True, download_name=filename)
