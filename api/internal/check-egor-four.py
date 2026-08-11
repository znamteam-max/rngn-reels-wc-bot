from __future__ import annotations

import json
from http.server import BaseHTTPRequestHandler

from bot import db

IDS = (
    "DZ9L2qftnM5",
    "DZ-0N70NqpE",
    "DaBLWrpsLgv",
    "DaFrdOHNcE7",
)


class handler(BaseHTTPRequestHandler):
    def do_GET(self) -> None:
        rows = db.fetch_all(
            """
            SELECT id, instagram_id, status, author_name, added_by_username, project_name,
                   publish_date, video_type
            FROM videos
            WHERE instagram_id = ANY(%s)
            ORDER BY id
            """,
            (list(IDS),),
        )
        found = {str(row["instagram_id"]): row for row in rows}
        payload = {
            "ok": True,
            "results": [
                {
                    "instagram_id": iid,
                    "exists": iid in found,
                    "video_id": int(found[iid]["id"]) if iid in found else None,
                    "status": found[iid].get("status") if iid in found else None,
                    "author_name": found[iid].get("author_name") if iid in found else None,
                    "added_by_username": found[iid].get("added_by_username") if iid in found else None,
                    "project_name": found[iid].get("project_name") if iid in found else None,
                    "publish_date": found[iid]["publish_date"].isoformat() if iid in found and found[iid].get("publish_date") else None,
                    "video_type": found[iid].get("video_type") if iid in found else None,
                }
                for iid in IDS
            ],
        }
        body = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)
