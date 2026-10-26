"""Master data imports (Platform Admin) and Excel bulk uploads (institutions)."""
import io

from flask import Blueprint, g, request, send_file

from app.api.common import arg_int
from app.core.api import api
from app.core.errors import PermissionDenied, ValidationError
from app.core.responses import created, ok, paginated
from app.core.storage import signed_download_url
from app.core.validation import get_or_404
from app.enums import BulkUploadType, Permission, Role
from app.core.institutions import get_institution, ref_filter
from app.models import BulkUploadJob, MasterImportBatch
from app.services import bulk
from app.core.records import active

bp = Blueprint("data_admin", __name__, url_prefix="")
PA = [Role.PLATFORM_ADMIN]


def _file():
    f = request.files.get("file")
    if f is None:
        raise ValidationError("Attach the file in the 'file' field")
    return f


@bp.get("/master-data/import-batches")
@api("LIST_IMPORT_BATCHES", roles=PA)
def list_batches():
    return paginated(MasterImportBatch.query.order_by(MasterImportBatch.created_at.desc()),
                     lambda b: b.to_dict(exclude=("exceptions",)))


@bp.get("/master-data/import-batches/<int:batch_id>")
@api("GET_IMPORT_BATCH", roles=PA)
def get_batch(batch_id):
    return ok(get_or_404(MasterImportBatch, batch_id, label="Import batch").to_dict())


# --------------------------------------------------------------- bulk upload
@bp.get("/bulk-uploads/templates/<upload_type>")
@api("DOWNLOAD_BULK_TEMPLATE", auth=False)
def template(upload_type):
    upload_type = BulkUploadType(upload_type.upper())
    return send_file(io.BytesIO(bulk.template_bytes(upload_type)), as_attachment=True,
                     download_name=f"{upload_type.value.lower()}_template.xlsx",
                     mimetype="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")


@bp.post("/institutions/<any(university,college):kind>/<int:institution_id>/bulk-uploads/<upload_type>")
@api("UPLOAD_BULK_FILE",
     permissions=[Permission.BULK_UPLOAD])
def upload(kind, institution_id, upload_type):
    inst = get_institution(kind, institution_id)
    job = bulk.process(g.actor, upload_type.upper(), inst, _file(), arg_int("syllabus_id") or
                       (int(request.form["syllabus_id"]) if request.form.get("syllabus_id") else None))
    out = job.to_dict(exclude=("error_file_key",))
    if job.error_file_key:
        out["error_file_url"] = signed_download_url(job.error_file_key, f"errors-{job.id}.xlsx",
                                                    "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")
    return created(out, f"{job.success_rows} rows saved, {job.error_rows} rows with errors")


@bp.get("/institutions/<any(university,college):kind>/<int:institution_id>/bulk-uploads")
@api("LIST_BULK_UPLOADS", permissions=[Permission.BULK_UPLOAD])
def list_uploads(kind, institution_id):
    inst = get_institution(kind, institution_id)
    if not g.actor.can(Permission.BULK_UPLOAD, inst):
        raise PermissionDenied("Not your institution")
    q = active(BulkUploadJob).filter(ref_filter(BulkUploadJob, inst)).order_by(BulkUploadJob.created_at.desc())
    return paginated(q, lambda j: j.to_dict(exclude=("errors", "error_file_key")))
