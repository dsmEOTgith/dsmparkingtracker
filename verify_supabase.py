r"""
Verify the v1.0 Supabase database, RPC functions, and private photo bucket.

Run:
    .\.venv\Scripts\python.exe verify_supabase.py
"""

from pathlib import Path

try:
    import tomllib
except ModuleNotFoundError:
    import tomli as tomllib

from supabase import create_client

BASE = Path(__file__).resolve().parent
SECRETS = BASE / ".streamlit" / "secrets.toml"

if not SECRETS.exists():
    raise SystemExit(
        "Missing .streamlit/secrets.toml. "
        "Create it from secrets.toml.example first."
    )

with SECRETS.open("rb") as handle:
    config = tomllib.load(handle)

settings = config.get("supabase", {})
url = str(settings.get("url", "")).strip()
secret_key = str(settings.get("secret_key", "")).strip()
bucket = str(settings.get("bucket", "violation-photos")).strip()
timezone_name = str(
    config.get("app", {}).get("timezone", "America/Chicago")
)

if not url or not secret_key:
    raise SystemExit(
        "[supabase] url and secret_key are required."
    )

client = create_client(url, secret_key)

print("1/4 Checking violations table...")
table_result = (
    client.table("violations")
    .select("id", count="exact")
    .limit(1)
    .execute()
)
print("    OK - current record count:", table_result.count or 0)

print("2/4 Checking dashboard RPC...")
metrics = client.rpc(
    "parking_dashboard_metrics",
    {"p_timezone": timezone_name},
).execute()
print("    OK -", metrics.data)

print("3/4 Checking top-vehicles RPC...")
top = client.rpc(
    "parking_top_vehicles",
    {"p_limit": 5},
).execute()
print("    OK - returned", len(top.data or []), "row(s)")

print("4/4 Checking private Storage bucket...")
objects = (
    client.storage
    .from_(bucket)
    .list(
        "",
        {
            "limit": 1,
            "offset": 0,
            "sortBy": {
                "column": "name",
                "order": "asc",
            },
        },
    )
)
print("    OK - bucket is accessible")

print()
print("Supabase v1.0 backend verification PASSED.")
