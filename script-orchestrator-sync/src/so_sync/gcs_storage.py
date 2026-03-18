"""Minimal Google Cloud Storage client helpers for mirror/read workflows."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any


class GcsStorageError(RuntimeError):
    pass


def parse_gs_uri(uri: str) -> tuple[str, str]:
    raw = str(uri or "").strip()
    if not raw.startswith("gs://"):
        raise GcsStorageError(f"Invalid gs:// URI: {uri!r}")
    body = raw[5:]
    if "/" not in body:
        raise GcsStorageError(f"Invalid gs:// URI (missing object path): {uri!r}")
    bucket, obj = body.split("/", 1)
    bucket = bucket.strip()
    obj = obj.strip()
    if not bucket or not obj:
        raise GcsStorageError(f"Invalid gs:// URI: {uri!r}")
    return bucket, obj


def _auth_hint(exc: Exception) -> GcsStorageError:
    creds_path = os.environ.get("GOOGLE_APPLICATION_CREDENTIALS", "").strip()
    if creds_path:
        creds_hint = f"GOOGLE_APPLICATION_CREDENTIALS is set (basename={Path(creds_path).name})."
    else:
        creds_hint = "GOOGLE_APPLICATION_CREDENTIALS is not set."
    return GcsStorageError(
        "Google Cloud Storage API call failed. "
        f"{creds_hint} "
        "Set GOOGLE_APPLICATION_CREDENTIALS to a valid service-account key JSON, "
        "or verify ADC with: gcloud auth application-default print-access-token. "
        f"original_error={exc}"
    )


def _build_storage_client() -> Any:
    try:
        from google.cloud import storage
    except Exception as exc:
        raise GcsStorageError("google-cloud-storage is required for GCS operations.") from exc
    try:
        return storage.Client()
    except Exception as exc:
        raise GcsStorageError(
            "Google Cloud Storage auth failed. "
            "Set GOOGLE_APPLICATION_CREDENTIALS to a GCS-enabled service account key JSON."
        ) from exc


@dataclass(frozen=True)
class GcsStorageClient:
    client: Any
    bucket_name: str

    @classmethod
    def from_adc(cls, bucket_name: str) -> "GcsStorageClient":
        bucket = str(bucket_name or "").strip()
        if not bucket:
            raise GcsStorageError("bucket_name is required for GCS client.")
        return cls(client=_build_storage_client(), bucket_name=bucket)

    def _bucket(self) -> Any:
        return self.client.bucket(self.bucket_name)

    def list_objects(self, prefix: str = "") -> list[dict[str, Any]]:
        try:
            bucket = self._bucket()
            out: list[dict[str, Any]] = []
            for blob in self.client.list_blobs(bucket, prefix=str(prefix or "")):
                out.append(
                    {
                        "name": str(blob.name or ""),
                        "size": int(blob.size or 0),
                        "updated": str(blob.updated or ""),
                        "generation": str(blob.generation or ""),
                    }
                )
            return out
        except Exception as exc:
            raise _auth_hint(exc) from exc

    def object_exists(self, object_name: str) -> bool:
        obj = str(object_name or "").strip()
        if not obj:
            return False
        try:
            blob = self._bucket().blob(obj)
            return bool(blob.exists())
        except Exception as exc:
            raise _auth_hint(exc) from exc

    def upload_file(
        self,
        local_path: str,
        object_name: str,
        *,
        content_type: str = "",
        metadata: dict[str, str] | None = None,
    ) -> str:
        path = Path(local_path)
        if not path.exists():
            raise GcsStorageError(f"upload source path does not exist: {local_path}")
        obj = str(object_name or "").strip()
        if not obj:
            raise GcsStorageError("object_name is required for upload_file")
        try:
            blob = self._bucket().blob(obj)
            if metadata:
                blob.metadata = {str(k): str(v) for k, v in metadata.items()}
            kwargs = {}
            if content_type:
                kwargs["content_type"] = str(content_type)
            blob.upload_from_filename(str(path), **kwargs)
            return f"gs://{self.bucket_name}/{obj}"
        except Exception as exc:
            raise _auth_hint(exc) from exc

    def upload_text(
        self,
        text: str,
        object_name: str,
        *,
        content_type: str = "application/json",
        metadata: dict[str, str] | None = None,
    ) -> str:
        obj = str(object_name or "").strip()
        if not obj:
            raise GcsStorageError("object_name is required for upload_text")
        try:
            blob = self._bucket().blob(obj)
            if metadata:
                blob.metadata = {str(k): str(v) for k, v in metadata.items()}
            blob.upload_from_string(str(text), content_type=content_type)
            return f"gs://{self.bucket_name}/{obj}"
        except Exception as exc:
            raise _auth_hint(exc) from exc

    def download_text(self, object_name: str) -> str:
        obj = str(object_name or "").strip()
        if not obj:
            raise GcsStorageError("object_name is required for download_text")
        try:
            blob = self._bucket().blob(obj)
            return str(blob.download_as_text())
        except Exception as exc:
            raise _auth_hint(exc) from exc

    def download_to_path(self, object_name: str, dst_path: str) -> str:
        obj = str(object_name or "").strip()
        if not obj:
            raise GcsStorageError("object_name is required for download_to_path")
        path = Path(dst_path)
        path.parent.mkdir(parents=True, exist_ok=True)
        try:
            blob = self._bucket().blob(obj)
            blob.download_to_filename(str(path))
            return str(path)
        except Exception as exc:
            raise _auth_hint(exc) from exc

    def delete_object(self, object_name: str) -> None:
        obj = str(object_name or "").strip()
        if not obj:
            raise GcsStorageError("object_name is required for delete_object")
        try:
            self._bucket().blob(obj).delete()
        except Exception as exc:
            raise _auth_hint(exc) from exc

    def copy_object(self, src_object_name: str, dst_object_name: str) -> str:
        src = str(src_object_name or "").strip()
        dst = str(dst_object_name or "").strip()
        if not src or not dst:
            raise GcsStorageError("src_object_name and dst_object_name are required for copy_object")
        try:
            bucket = self._bucket()
            src_blob = bucket.blob(src)
            bucket.copy_blob(src_blob, bucket, new_name=dst)
            return f"gs://{self.bucket_name}/{dst}"
        except Exception as exc:
            raise _auth_hint(exc) from exc


def from_env(bucket_env: str = "CLIPS_LIBRARY_MIRROR_BUCKET") -> GcsStorageClient:
    bucket = os.environ.get(bucket_env, "").strip()
    if not bucket:
        raise GcsStorageError(f"{bucket_env} is required for GCS mirror operations.")
    return GcsStorageClient.from_adc(bucket)
