from __future__ import annotations

import json
from urllib.error import HTTPError, URLError
from urllib.parse import quote
from urllib.request import Request, urlopen

from app.config import SUPABASE_SCORE_BUCKET, SUPABASE_SECRET_KEY, SUPABASE_URL


class StorageUnavailable(RuntimeError):
    pass


class ScoreStorage:
    """Small private-bucket adapter using Supabase Storage's HTTP API."""

    def _settings(self) -> tuple[str, str, str]:
        if not SUPABASE_URL or not SUPABASE_SECRET_KEY:
            raise StorageUnavailable("Score file storage is not configured")
        return SUPABASE_URL, SUPABASE_SECRET_KEY, SUPABASE_SCORE_BUCKET

    @staticmethod
    def _path(key: str) -> str:
        return "/".join(quote(part, safe="") for part in key.split("/"))

    @staticmethod
    def _request(url: str, *, method: str = "GET", headers: dict[str, str], body: bytes | None = None) -> bytes:
        request = Request(url, data=body, headers=headers, method=method)
        try:
            with urlopen(request, timeout=30) as response:
                return response.read()
        except (HTTPError, URLError, TimeoutError) as error:
            raise StorageUnavailable("Supabase Storage request failed") from error

    def _signed(self, key: str, *, upload: bool) -> dict[str, object]:
        base_url, secret_key, bucket = self._settings()
        operation = "upload/sign" if upload else "sign"
        endpoint = f"{base_url}/storage/v1/object/{operation}/{quote(bucket, safe='')}/{self._path(key)}"
        response = self._request(
            endpoint,
            method="POST",
            headers={"apikey": secret_key, "Content-Type": "application/json"},
            body=json.dumps({"expiresIn": 600}).encode("utf-8"),
        )
        try:
            payload = json.loads(response)
        except (json.JSONDecodeError, UnicodeDecodeError) as error:
            raise StorageUnavailable("Storage returned an invalid signed URL response") from error
        signed_path = payload.get("url") or payload.get("signedURL") or payload.get("signedUrl")
        if not isinstance(signed_path, str) or not signed_path:
            raise StorageUnavailable("Storage did not return a signed URL")
        return {
            "url": f"{base_url}/storage/v1{signed_path}",
            "expiresInSeconds": 600,
        }

    def create_signed_upload_url(self, key: str, mime_type: str) -> dict[str, object]:
        result = self._signed(key, upload=True)
        return {
            "uploadUrl": result["url"],
            "uploadMethod": "PUT",
            "uploadHeaders": {"Content-Type": mime_type},
            "expiresInSeconds": result["expiresInSeconds"],
        }

    def create_signed_download_url(self, key: str) -> dict[str, object]:
        result = self._signed(key, upload=False)
        return {"sourceUrl": result["url"], "expiresInSeconds": result["expiresInSeconds"]}

    def download(self, key: str) -> bytes:
        base_url, secret_key, bucket = self._settings()
        url = f"{base_url}/storage/v1/object/{quote(bucket, safe='')}/{self._path(key)}"
        return self._request(url, headers={"apikey": secret_key})

    def remove(self, key: str) -> None:
        base_url, secret_key, bucket = self._settings()
        url = f"{base_url}/storage/v1/object/{quote(bucket, safe='')}/{self._path(key)}"
        self._request(url, method="DELETE", headers={"apikey": secret_key})
