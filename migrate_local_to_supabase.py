r"""
Optional one-time migration of an existing local v0.x SQLite database
and its violation photos into the v1.0 Supabase backend.

Run from the project folder after:
1) supabase_setup.sql has been executed,
2) .streamlit/secrets.toml contains [supabase],
3) requirements.txt has been installed.

Command:
    .\.venv\Scripts\python.exe migrate_local_to_supabase.py
"""

import io
import sqlite3
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

try:
    import tomllib
except ModuleNotFoundError:
    import tomli as tomllib

from supabase import create_client

BASE = Path(__file__).resolve().parent
DB = BASE / "parking_tracker.db"
PHOTO_DIR = BASE / "violation_photos"
SECRETS = BASE / ".streamlit" / "secrets.toml"

if not DB.exists():
    raise SystemExit(f"Local database not found: {DB}")

if not SECRETS.exists():
    raise SystemExit(
        "Create .streamlit/secrets.toml with [supabase] settings first."
    )

with SECRETS.open("rb") as handle:
    config = tomllib.load(handle)

supabase_cfg = config.get("supabase", {})
url = str(supabase_cfg.get("url", "")).strip()
secret_key = str(supabase_cfg.get("secret_key", "")).strip()
bucket = str(supabase_cfg.get("bucket", "violation-photos")).strip()
timezone_name = str(
    config.get("app", {}).get("timezone", "America/Chicago")
)
tz = ZoneInfo(timezone_name)

if not url or not secret_key:
    raise SystemExit("[supabase] url and secret_key are required.")

client = create_client(url, secret_key)

local = sqlite3.connect(DB)
local.row_factory = sqlite3.Row

rows = local.execute(
    "SELECT * FROM violations ORDER BY id"
).fetchall()

print(f"Found {len(rows)} local records.")

migrated = 0
skipped = 0
failed = 0

for local_row in rows:
    row = dict(local_row)
    local_id = int(row["id"])

    existing = (
        client.table("violations")
        .select("id")
        .eq("legacy_local_id", local_id)
        .limit(1)
        .execute()
        .data
        or []
    )

    if existing:
        print(f"SKIP #{local_id}: already migrated")
        skipped += 1
        continue

    photo_path = None
    uploaded_path = None

    try:
        old_photo = row.get("photo_path")

        if old_photo:
            source = BASE / old_photo
            if source.exists():
                uploaded_path = (
                    f"legacy/{local_id}/"
                    f"{source.name}"
                )

                with source.open("rb") as photo_file:
                    client.storage.from_(bucket).upload(
                        path=uploaded_path,
                        file=photo_file,
                        file_options={
                            "content-type": "image/jpeg",
                            "cache-control": "3600",
                            "upsert": "false",
                        },
                    )

                photo_path = uploaded_path

        recorded_at = str(row.get("recorded_at") or "")
        dt = datetime.fromisoformat(
            recorded_at.replace("Z", "+00:00")
        )

        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=tz)

        payload = {
            "legacy_local_id": local_id,
            "plate_number": row.get("plate_number"),
            "state": row.get("state") or "TX",
            "violation_type": row.get("violation_type") or "Other",
            "location": row.get("location") or "Other",
            "notes": row.get("notes") or None,
            "warning_level": int(row.get("warning_level") or 1),
            "warning_label": row.get("warning_label") or "First Warning",
            "recorded_at": dt.isoformat(),
            "warned_by": row.get("warned_by") or None,
            "entered_by": row.get("entered_by") or "Legacy local migration",
            "photo_path": photo_path,
            "photo_source": row.get("photo_source") or (
                "Legacy migration" if photo_path else None
            ),
            "status": row.get("status") or "Active",
            "dismissed_at": row.get("dismissed_at") or None,
            "dismissed_by": row.get("dismissed_by") or None,
        }

        client.table("violations").insert(payload).execute()

        print(f"OK   #{local_id}")
        migrated += 1

    except Exception as exc:
        failed += 1
        print(f"FAIL #{local_id}: {exc}")

        if uploaded_path:
            try:
                client.storage.from_(bucket).remove(
                    [uploaded_path]
                )
            except Exception:
                pass

print()
print("Migration complete.")
print(f"Migrated: {migrated}")
print(f"Skipped:  {skipped}")
print(f"Failed:   {failed}")
