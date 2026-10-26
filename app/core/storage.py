"""File storage behind an interface (local disk now, S3 later) and signed links.

Files are never public. A caller that passed the authorisation check gets a
short-lived signed URL to the DOWNLOAD_FILE API.
"""
import io
import os
import socket
import struct
from hashlib import sha256

from flask import current_app
from itsdangerous import BadSignature, SignatureExpired, URLSafeTimedSerializer

from app.core.errors import AppError, NotFound, ValidationError


class StorageBackend:
    def save(self, key, data: bytes):
        raise NotImplementedError

    def read(self, key) -> bytes:
        raise NotImplementedError

    def delete(self, key):
        raise NotImplementedError

    def exists(self, key) -> bool:
        raise NotImplementedError


class LocalStorage(StorageBackend):
    def __init__(self, root):
        self.root = root

    def _path(self, key):
        path = os.path.normpath(os.path.join(self.root, key))
        if not path.startswith(os.path.normpath(self.root)):
            raise ValidationError("Invalid storage key")
        return path

    def save(self, key, data):
        path = self._path(key)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "wb") as fh:
            fh.write(data)
        return key

    def read(self, key):
        path = self._path(key)
        if not os.path.exists(path):
            raise NotFound("File not found")
        with open(path, "rb") as fh:
            return fh.read()

    def delete(self, key):
        path = self._path(key)
        if os.path.exists(path):
            os.remove(path)

    def exists(self, key):
        return os.path.exists(self._path(key))


class S3Storage(StorageBackend):  # pragma: no cover - needs AWS
    def __init__(self, bucket, region):
        import boto3
        self.bucket = bucket
        self.client = boto3.client("s3", region_name=region)

    def save(self, key, data):
        self.client.put_object(Bucket=self.bucket, Key=key, Body=data, ServerSideEncryption="AES256")
        return key

    def read(self, key):
        try:
            return self.client.get_object(Bucket=self.bucket, Key=key)["Body"].read()
        except self.client.exceptions.NoSuchKey as exc:
            raise NotFound("File not found") from exc

    def delete(self, key):
        self.client.delete_object(Bucket=self.bucket, Key=key)

    def exists(self, key):
        try:
            self.client.head_object(Bucket=self.bucket, Key=key)
            return True
        except Exception:
            return False


def get_storage():
    cfg = current_app.config
    if cfg["STORAGE_BACKEND"] == "s3":
        return S3Storage(cfg["S3_BUCKET"], cfg["S3_REGION"])
    return LocalStorage(cfg["STORAGE_LOCAL_ROOT"])


# --------------------------------------------------------------- signing
def _serializer():
    return URLSafeTimedSerializer(current_app.config["SECRET_KEY"], salt="file-download")


def signed_download_url(storage_key, filename, mime_type):
    token = _serializer().dumps({"k": storage_key, "f": filename, "m": mime_type})
    base = current_app.config["PUBLIC_BASE_URL"].rstrip("/")
    return f"{base}{current_app.config['API_PREFIX']}/files/download?token={token}"


def resolve_download_token(token):
    max_age = int(current_app.config["SIGNED_URL_EXPIRES"].total_seconds())
    try:
        data = _serializer().loads(token, max_age=max_age)
    except SignatureExpired as exc:
        raise AppError("Download link has expired", error_code="LINK_EXPIRED", status_code=410) from exc
    except BadSignature as exc:
        raise AppError("Download link is invalid", error_code="LINK_INVALID", status_code=400) from exc
    return data["k"], data["f"], data["m"]


# ---------------------------------------------------------- upload checks
_MAGIC = {
    "application/pdf": [b"%PDF"],
    "image/png": [b"\x89PNG\r\n\x1a\n"],
    "image/jpeg": [b"\xff\xd8\xff"],
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document": [b"PK\x03\x04"],
}
_EXT_TO_MIME = {
    "pdf": "application/pdf",
    "png": "image/png",
    "jpg": "image/jpeg",
    "jpeg": "image/jpeg",
    "docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
}


def _clamav_scan(data):  # pragma: no cover - needs clamd
    cfg = current_app.config
    with socket.create_connection((cfg["CLAMAV_HOST"], cfg["CLAMAV_PORT"]), timeout=30) as sock:
        sock.sendall(b"zINSTREAM\0")
        for i in range(0, len(data), 8192):
            chunk = data[i:i + 8192]
            sock.sendall(struct.pack("!L", len(chunk)) + chunk)
        sock.sendall(struct.pack("!L", 0))
        reply = sock.recv(4096).decode(errors="ignore")
    if "FOUND" in reply:
        raise ValidationError("The file failed the security scan", error_code="FILE_INFECTED")


def _downscale_image(data, mime):
    from PIL import Image
    limit = current_app.config["IMAGE_MAX_DIMENSION"]
    img = Image.open(io.BytesIO(data))
    if max(img.size) <= limit:
        return data
    img.thumbnail((limit, limit))
    out = io.BytesIO()
    fmt = "PNG" if mime == "image/png" else "JPEG"
    if fmt == "JPEG" and img.mode not in ("RGB", "L"):
        img = img.convert("RGB")
    img.save(out, format=fmt, optimize=True, quality=85)
    return out.getvalue()


def validate_upload(file_storage):
    """Check type, size, magic bytes and (optionally) malware. Returns
    (data, mime_type, sha256_hex)."""
    cfg = current_app.config
    if file_storage is None or not file_storage.filename:
        raise ValidationError("A file is required", details={"field": "file"})
    ext = file_storage.filename.rsplit(".", 1)[-1].lower() if "." in file_storage.filename else ""
    if ext not in cfg["ALLOWED_FILE_EXTENSIONS"]:
        raise ValidationError(
            "Unsupported file type. Allowed: " + ", ".join(sorted(cfg["ALLOWED_FILE_EXTENSIONS"])),
            error_code="UNSUPPORTED_FILE_TYPE")
    mime = _EXT_TO_MIME[ext]
    data = file_storage.read()
    if len(data) == 0:
        raise ValidationError("The file is empty", error_code="FILE_EMPTY")
    if len(data) > cfg["MAX_FILE_SIZE_BYTES"]:
        raise ValidationError(
            f"File is larger than {cfg['MAX_FILE_SIZE_BYTES'] // (1024 * 1024)} MB",
            error_code="FILE_TOO_LARGE")
    if not any(data.startswith(m) for m in _MAGIC[mime]):
        raise ValidationError("The file content does not match its type, or it is corrupted",
                              error_code="FILE_CORRUPTED")
    if cfg["VIRUS_SCAN_ENABLED"]:
        _clamav_scan(data)
    if mime in ("image/png", "image/jpeg"):
        try:
            data = _downscale_image(data, mime)
        except Exception as exc:
            raise ValidationError("The image could not be read", error_code="FILE_CORRUPTED") from exc
    return data, mime, sha256(data).hexdigest()
