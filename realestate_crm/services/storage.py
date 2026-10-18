import asyncio
from pathlib import Path
from urllib.parse import quote

import httpx

from realestate_crm.config import Settings

BUCKET = "crm-imports"
BACKUP_BUCKET = "crm-backups"


def _headers(settings: Settings) -> dict[str, str]:
    key = settings.supabase_service_role_key.get_secret_value()
    return {"apikey": key, "authorization": f"Bearer {key}"}


async def upload_import(settings: Settings, job_id: str, path: Path) -> str:
    headers = _headers(settings)
    storage_path = f"{job_id}/{path.name}"
    base = settings.supabase_url.rstrip("/")
    async with httpx.AsyncClient(timeout=60) as client:
        bucket_response = await client.post(
            f"{base}/storage/v1/bucket",
            headers={**headers, "content-type": "application/json"},
            json={
                "id": BUCKET,
                "name": BUCKET,
                "public": False,
                "file_size_limit": settings.max_import_file_mb * 1024 * 1024,
            },
        )
        if bucket_response.status_code not in (200, 201, 409):
            bucket_response.raise_for_status()
        with path.open("rb") as file_handle:

            async def stream_file():
                while chunk := await asyncio.to_thread(file_handle.read, 64 * 1024):
                    yield chunk

            response = await client.post(
                f"{base}/storage/v1/object/{BUCKET}/{quote(storage_path, safe='/')}",
                headers={
                    **headers,
                    "content-type": "application/octet-stream",
                    "x-upsert": "false",
                },
                content=stream_file(),
            )
            response.raise_for_status()
    return storage_path


async def download_import(settings: Settings, storage_path: str, target: Path) -> None:
    url = f"{settings.supabase_url.rstrip('/')}/storage/v1/object/authenticated/{BUCKET}/{quote(storage_path, safe='/')}"
    async with httpx.AsyncClient(timeout=60) as client:
        async with client.stream("GET", url, headers=_headers(settings)) as response:
            response.raise_for_status()
            with target.open("wb") as output:
                async for chunk in response.aiter_bytes(64 * 1024):
                    output.write(chunk)


async def delete_import(settings: Settings, storage_path: str) -> None:
    url = f"{settings.supabase_url.rstrip('/')}/storage/v1/object/{BUCKET}/{quote(storage_path, safe='/')}"
    async with httpx.AsyncClient(timeout=20) as client:
        response = await client.delete(url, headers=_headers(settings))
        if response.status_code not in (200, 204, 404):
            response.raise_for_status()


async def upload_backup(settings: Settings, backup_name: str, path: Path) -> str:
    """Store a private timestamped workbook in Supabase Storage."""
    headers = _headers(settings)
    base = settings.supabase_url.rstrip("/")
    async with httpx.AsyncClient(timeout=120) as client:
        bucket_response = await client.post(
            f"{base}/storage/v1/bucket",
            headers={**headers, "content-type": "application/json"},
            json={"id": BACKUP_BUCKET, "name": BACKUP_BUCKET, "public": False},
        )
        if bucket_response.status_code not in (200, 201, 409):
            bucket_response.raise_for_status()

        storage_path = f"daily/{backup_name}"
        with path.open("rb") as file_handle:

            async def stream_file():
                while chunk := await asyncio.to_thread(file_handle.read, 64 * 1024):
                    yield chunk

            response = await client.post(
                f"{base}/storage/v1/object/{BACKUP_BUCKET}/{quote(storage_path, safe='/')}",
                headers={
                    **headers,
                    "content-type": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                    "x-upsert": "false",
                },
                content=stream_file(),
            )
            response.raise_for_status()
    return storage_path


async def backup_download_url(
    settings: Settings, storage_path: str, expires_seconds: int = 7 * 24 * 60 * 60
) -> str:
    """Create a temporary download link for an object in the private backup bucket."""
    base = settings.supabase_url.rstrip("/")
    async with httpx.AsyncClient(timeout=30) as client:
        response = await client.post(
            f"{base}/storage/v1/object/sign/{BACKUP_BUCKET}/{quote(storage_path, safe='/')}",
            headers={**_headers(settings), "content-type": "application/json"},
            json={"expiresIn": expires_seconds},
        )
        response.raise_for_status()
    signed_url = response.json().get("signedURL")
    if not isinstance(signed_url, str) or not signed_url:
        raise RuntimeError("Supabase Storage did not return a signed backup URL")
    if signed_url.startswith("https://"):
        return signed_url
    if signed_url.startswith("/storage/v1/"):
        return f"{base}{signed_url}"
    return f"{base}/storage/v1{signed_url if signed_url.startswith('/') else '/' + signed_url}"
