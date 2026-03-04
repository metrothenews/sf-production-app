from __future__ import annotations

import argparse
import json
import tempfile
from pathlib import Path

from googleapiclient.http import MediaFileUpload

from render_agent.auth_diag import describe_google_auth
from render_agent.drive_listing import list_folder_video_files
from so_sync.drive_upload import from_env as drive_upload_from_env


def _list_preview(folder_id: str) -> list[dict[str, str]]:
    rows = list_folder_video_files(folder_id)
    return rows[:3]


def _write_probe(out_folder_id: str) -> dict[str, str]:
    client = drive_upload_from_env()
    with tempfile.NamedTemporaryFile("w", suffix=".txt", delete=False) as tmp:
        tmp.write("drive_api_smoke\n")
        tmp_path = Path(tmp.name)
    try:
        resp = (
            client.service.files()
            .create(
                body={"name": tmp_path.name, "parents": [out_folder_id]},
                media_body=MediaFileUpload(str(tmp_path), mimetype="text/plain", resumable=False),
                fields="id,name,webViewLink",
            )
            .execute()
        )
        file_id = str(resp.get("id") or "")
        if file_id:
            client.service.files().delete(fileId=file_id).execute()
        return {
            "file_id": file_id,
            "name": str(resp.get("name") or ""),
            "webViewLink": str(resp.get("webViewLink") or ""),
        }
    finally:
        tmp_path.unlink(missing_ok=True)


def main() -> int:
    parser = argparse.ArgumentParser(description="Smoke test local Drive API auth/list/write")
    parser.add_argument("--folder", required=True, help="Source folder id to list")
    parser.add_argument("--out-folder", required=True, help="Output folder id to write/delete probe file")
    args = parser.parse_args()

    diag = describe_google_auth()
    print(json.dumps({"auth_diag": diag}, sort_keys=True))
    if diag.get("error"):
        print("Drive auth diagnostic reported an error; fix credentials before continuing.")
        return 1

    try:
        preview = _list_preview(args.folder)
        print(json.dumps({"source_folder_id": args.folder, "video_preview": preview}, sort_keys=True))
        probe = _write_probe(args.out_folder)
        print(json.dumps({"out_folder_id": args.out_folder, "write_delete_probe": probe}, sort_keys=True))
        return 0
    except Exception as exc:
        print(f"drive_api_smoke_failed: {exc}")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
