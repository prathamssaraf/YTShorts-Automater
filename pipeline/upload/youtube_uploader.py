"""YouTube Data API v3 uploader with OAuth2. Supports --auth-only and --dry-run."""
from __future__ import annotations

import argparse
import json
import logging
import sys
import time
from pathlib import Path
from typing import Any

from ..config import load_settings, resolve_path

log = logging.getLogger(__name__)

_SCOPES = ["https://www.googleapis.com/auth/youtube.upload"]


class UploadError(RuntimeError):
    pass


def _load_credentials(credentials_file: Path, client_secrets_file: Path):
    from google.auth.transport.requests import Request
    from google.oauth2.credentials import Credentials
    from google_auth_oauthlib.flow import InstalledAppFlow

    creds = None
    if credentials_file.exists():
        try:
            creds = Credentials.from_authorized_user_file(str(credentials_file), _SCOPES)
        except Exception as exc:  # noqa: BLE001
            log.warning("failed to load saved credentials (%s); re-authenticating", exc)
            creds = None
    if creds and creds.valid:
        return creds
    if creds and creds.expired and creds.refresh_token:
        try:
            creds.refresh(Request())
            credentials_file.write_text(creds.to_json())
            return creds
        except Exception as exc:  # noqa: BLE001
            log.warning("refresh failed (%s); re-authenticating", exc)
    if not client_secrets_file.exists():
        raise UploadError(
            f"Missing {client_secrets_file}. Download OAuth2 client_secrets.json from Google Cloud Console and place it there."
        )
    flow = InstalledAppFlow.from_client_secrets_file(str(client_secrets_file), _SCOPES)
    creds = flow.run_local_server(port=0)
    credentials_file.parent.mkdir(parents=True, exist_ok=True)
    credentials_file.write_text(creds.to_json())
    return creds


def upload(
    *,
    video_path: Path,
    title: str,
    description: str,
    tags: list[str],
    dry_run: bool = False,
) -> dict[str, Any]:
    """Upload the given video. Returns {'video_id','url'} or {'dry_run': True,...}."""
    cfg = load_settings()["upload"]
    if dry_run:
        log.info("[dry-run] would upload %s with title=%r", video_path, title)
        return {"dry_run": True, "path": str(video_path), "title": title}

    from googleapiclient.discovery import build
    from googleapiclient.http import MediaFileUpload

    creds = _load_credentials(
        resolve_path(cfg["credentials_file"]),
        resolve_path(cfg["client_secrets_file"]),
    )
    youtube = build("youtube", "v3", credentials=creds, cache_discovery=False)

    body = {
        "snippet": {
            "title": title[:100],
            "description": (description + cfg.get("description_footer", ""))[:5000],
            "tags": tags[: int(cfg.get("tags_max", 15))],
            "categoryId": str(cfg.get("category_id", "17")),
        },
        "status": {
            "privacyStatus": cfg.get("privacy_status", "public"),
            "selfDeclaredMadeForKids": bool(cfg.get("made_for_kids", False)),
        },
    }
    media = MediaFileUpload(str(video_path), mimetype="video/mp4", resumable=True, chunksize=8 * 1024 * 1024)
    request = youtube.videos().insert(part="snippet,status", body=body, media_body=media)

    response: dict[str, Any] | None = None
    attempt = 0
    while response is None:
        attempt += 1
        try:
            status, response = request.next_chunk()
            if status and not response:
                log.info("upload progress: %.1f%%", status.progress() * 100)
        except Exception as exc:  # noqa: BLE001
            msg = str(exc)
            if "quotaExceeded" in msg or "uploadLimitExceeded" in msg:
                if attempt > 3:
                    raise UploadError(f"YouTube upload quota exhausted: {exc}") from exc
                log.warning("quota hiccup — sleeping 60s then retrying (%d/3)", attempt)
                time.sleep(60)
                continue
            raise UploadError(f"upload failed: {exc}") from exc
    if not response or "id" not in response:
        raise UploadError(f"unexpected YouTube response: {response}")
    return {
        "video_id": response["id"],
        "url": f"https://youtube.com/shorts/{response['id']}",
    }


def _cli() -> int:
    parser = argparse.ArgumentParser(description="YouTube uploader CLI")
    parser.add_argument("--auth-only", action="store_true", help="Run OAuth flow then exit.")
    parser.add_argument("--video", type=Path, help="Video to upload.")
    parser.add_argument("--title", default="Test Upload")
    parser.add_argument("--description", default="")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    cfg = load_settings()["upload"]
    if args.auth_only:
        _load_credentials(resolve_path(cfg["credentials_file"]), resolve_path(cfg["client_secrets_file"]))
        print("OAuth token saved.")
        return 0
    if not args.video:
        parser.error("--video required unless --auth-only")
    result = upload(
        video_path=args.video,
        title=args.title,
        description=args.description,
        tags=[],
        dry_run=args.dry_run,
    )
    print(json.dumps(result, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(_cli())
