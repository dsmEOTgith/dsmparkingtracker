import io
import re
import sqlite3
import statistics
import tempfile
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import bcrypt
import cv2
import numpy as np
import pandas as pd
import streamlit as st
from PIL import Image, ImageOps
from pillow_heif import register_heif_opener

register_heif_opener(thumbnails=False)

BASE = Path(__file__).resolve().parent
LOCAL_DB = BASE / "parking_tracker.db"
LOCAL_PHOTOS = BASE / "violation_photos"
LOCAL_PHOTOS.mkdir(exist_ok=True)

VIOLATIONS = [
    "Fire Lane",
    "Reserved Parking",
    "Disabled Parking",
    "No/Expired Parking Permit",
    "Blocking Garbage Bin",
    "Improper Parking",
    "Seasonal Parking",
    "Blocking Driveway",
    "No Parking Zone",
    "Double Parking",
    "Accessible Parking Violation",
    "Parking Outside Marked Space",
    "Other",
]

LOCATIONS = [
    "Disabled Parking"
    "Main Parking Lot",
    "Main Entrance Parking",
    "Main Grass Area Parking",
    "Across Canemont Main",
    "Across Canemont Grass",
    "Reserved Parking Area",
    "Other",
]

# ============================================================
# Configuration / time
# ============================================================

def _secrets_section(name, default=None):
    try:
        if name in st.secrets:
            return st.secrets[name]
    except Exception:
        pass
    return {} if default is None else default


def deployment_mode():
    section = _secrets_section("deployment", {})
    try:
        return str(section.get("mode", "local")).strip().lower()
    except Exception:
        return "local"


def app_timezone_name():
    section = _secrets_section("app", {})
    try:
        return str(section.get("timezone", "America/Chicago"))
    except Exception:
        return "America/Chicago"


def app_tz():
    try:
        return ZoneInfo(app_timezone_name())
    except Exception:
        return ZoneInfo("America/Chicago")


def app_now():
    return datetime.now(app_tz())


def parse_datetime(value):
    if isinstance(value, datetime):
        dt = value
    else:
        raw = str(value or "").strip()
        if not raw:
            return None
        try:
            dt = datetime.fromisoformat(raw.replace("Z", "+00:00"))
        except Exception:
            return None

    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=app_tz())

    return dt.astimezone(app_tz())


def display_datetime(value):
    dt = parse_datetime(value)
    return dt.strftime("%Y-%m-%d %I:%M %p") if dt else str(value or "")


# ============================================================
# Authentication / roles
# ============================================================

def configured_users():
    users = _secrets_section("users", {})
    try:
        return dict(users)
    except Exception:
        return {}


def auth_settings():
    # v1.0 supports both the documented [auth] section and the
    # older [app_auth] section used in an earlier development build.
    section = _secrets_section("auth", None)
    if section:
        return section
    return _secrets_section("app_auth", {})


def auth_configured():
    settings = auth_settings()
    try:
        enabled = bool(settings.get("enabled", True))
    except Exception:
        enabled = True
    return enabled and bool(configured_users())


def login_user(username, password):
    users = configured_users()
    user = users.get(username)
    if not user:
        return False

    try:
        password_hash = str(user["password_hash"]).encode("utf-8")
        valid = bcrypt.checkpw(
            password.encode("utf-8"),
            password_hash,
        )
    except Exception:
        return False

    if not valid:
        return False

    st.session_state["authenticated"] = True
    st.session_state["auth_username"] = username
    st.session_state["auth_display_name"] = str(
        user.get("display_name", username)
    )
    st.session_state["auth_role"] = str(
        user.get("role", "volunteer")
    ).strip().lower()
    return True


def logout_user():
    for key in (
        "authenticated",
        "auth_username",
        "auth_display_name",
        "auth_role",
    ):
        st.session_state.pop(key, None)


def require_login():
    if not auth_configured():
        st.error("Authentication is not configured.")
        st.info(
            "Configure [auth] and [users.*] in Streamlit Secrets, "
            "then restart/redeploy the app."
        )
        st.stop()

    if st.session_state.get("authenticated"):
        return

    st.title("🚗 Church Parking Violation Tracker 🛻")
    st.subheader("Authorized personnel login")
    st.caption(
        "Parking records and photos are restricted to authorized church personnel."
    )

    with st.form("login_form"):
        username = st.text_input(
            "Username",
            autocomplete="username",
        )
        password = st.text_input(
            "Password",
            type="password",
            autocomplete="current-password",
        )
        submitted = st.form_submit_button(
            "Sign in",
            type="primary",
            width="stretch",
        )

    if submitted:
        if login_user(username.strip(), password):
            st.rerun()
        else:
            st.error("Invalid username or password.")

    st.stop()


def current_user():
    return {
        "username": st.session_state.get("auth_username", ""),
        "display_name": st.session_state.get("auth_display_name", ""),
        "role": st.session_state.get("auth_role", "volunteer"),
    }


# ============================================================
# Common data helpers
# ============================================================

def norm_plate(value):
    return re.sub(
        r"[^A-Z0-9]",
        "",
        (value or "").upper().strip(),
    )


def warning_label(number):
    return {
        0: "No Previous Violations",
        1: "First Warning",
        2: "Second Warning",
        3: "Third / Final Warning",
    }.get(
        number,
        "Repeat Violation / Administrative Review",
    )


def rows_to_history_df(rows):
    if not rows:
        return pd.DataFrame()

    df = pd.DataFrame(rows)

    for column in ("warned_by", "entered_by", "photo_path", "notes"):
        if column not in df.columns:
            df[column] = ""
        df[column] = df[column].fillna("")

    if "status" not in df.columns:
        df["status"] = "Active"

    df["recorded_at_display"] = df["recorded_at"].apply(display_datetime)
    df["Photo"] = df["photo_path"].map(lambda value: "Yes" if value else "No")
    return df


def normalize_record(row):
    if not row:
        return None
    return dict(row)


# ============================================================
# Local SQLite backend
# ============================================================

def local_db_conn():
    connection = sqlite3.connect(LOCAL_DB)
    connection.row_factory = sqlite3.Row
    return connection


def init_local_db():
    with local_db_conn() as connection:
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS violations(
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                plate_number TEXT NOT NULL,
                state TEXT NOT NULL DEFAULT 'TX',
                violation_type TEXT NOT NULL,
                location TEXT NOT NULL,
                notes TEXT,
                warning_level INTEGER NOT NULL,
                warning_label TEXT NOT NULL,
                recorded_at TEXT NOT NULL,
                warned_by TEXT,
                entered_by TEXT,
                photo_path TEXT,
                photo_source TEXT,
                status TEXT NOT NULL DEFAULT 'Active',
                dismissed_at TEXT,
                dismissed_by TEXT
            )
            """
        )

        existing = {
            row["name"]
            for row in connection.execute("PRAGMA table_info(violations)")
        }

        for column in (
            "warned_by",
            "entered_by",
            "photo_path",
            "photo_source",
            "dismissed_at",
            "dismissed_by",
        ):
            if column not in existing:
                connection.execute(
                    f"ALTER TABLE violations ADD COLUMN {column} TEXT"
                )

        connection.execute(
            "CREATE INDEX IF NOT EXISTS idx_plate "
            "ON violations(plate_number)"
        )
        connection.commit()


class LocalBackend:
    kind = "local"
    persistent = True
    display_name = "Local SQLite + local photo folder"

    def __init__(self):
        init_local_db()

    def active_count(self, plate):
        with local_db_conn() as connection:
            return int(
                connection.execute(
                    """
                    SELECT COUNT(*) AS c
                    FROM violations
                    WHERE plate_number=? AND status='Active'
                    """,
                    (plate,),
                ).fetchone()["c"]
            )

    def history(self, plate):
        with local_db_conn() as connection:
            rows = connection.execute(
                """
                SELECT *
                FROM violations
                WHERE plate_number=?
                ORDER BY datetime(recorded_at) DESC, id DESC
                """,
                (plate,),
            ).fetchall()
        return [dict(row) for row in rows]

    def get_record(self, record_id):
        with local_db_conn() as connection:
            row = connection.execute(
                "SELECT * FROM violations WHERE id=?",
                (int(record_id),),
            ).fetchone()
        return dict(row) if row else None

    def plate_options(self, limit=1000):
        with local_db_conn() as connection:
            rows = connection.execute(
                """
                SELECT plate_number, MAX(recorded_at) AS last_seen
                FROM violations
                GROUP BY plate_number
                ORDER BY MAX(recorded_at) DESC
                LIMIT ?
                """,
                (int(limit),),
            ).fetchall()
        return [row["plate_number"] for row in rows]

    def delete_photo(self, record_id):
        record = self.get_record(record_id)
        if not record:
            raise RuntimeError("Record not found.")

        photo_path = record.get("photo_path")
        if not photo_path:
            return False

        path = BASE / photo_path
        if path.exists():
            path.unlink()

        with local_db_conn() as connection:
            connection.execute(
                """
                UPDATE violations
                SET photo_path=NULL, photo_source=NULL
                WHERE id=?
                """,
                (int(record_id),),
            )
            connection.commit()
        return True

    def delete_record(self, record_id):
        record = self.get_record(record_id)
        if not record:
            raise RuntimeError("Record not found.")

        photo_path = record.get("photo_path")
        if photo_path:
            path = BASE / photo_path
            if path.exists():
                path.unlink()

        with local_db_conn() as connection:
            connection.execute(
                "DELETE FROM violations WHERE id=?",
                (int(record_id),),
            )
            connection.commit()
        return True

    def duplicate(self, plate, violation, location, violation_datetime):
        start = violation_datetime - timedelta(minutes=10)
        end = violation_datetime + timedelta(minutes=10)

        with local_db_conn() as connection:
            row = connection.execute(
                """
                SELECT id, recorded_at
                FROM violations
                WHERE plate_number=?
                  AND violation_type=?
                  AND location=?
                  AND status='Active'
                  AND datetime(recorded_at)
                      BETWEEN datetime(?) AND datetime(?)
                LIMIT 1
                """,
                (
                    plate,
                    violation,
                    location,
                    start.isoformat(),
                    end.isoformat(),
                ),
            ).fetchone()

        return dict(row) if row else None

    def save_violation(
        self,
        plate,
        state,
        violation,
        location,
        notes,
        warned_by,
        entered_by,
        violation_datetime,
        photo_file,
        photo_source,
    ):
        number = self.active_count(plate) + 1
        label = warning_label(number)

        photo_path = None
        if photo_file is not None:
            photo_path = save_local_photo(
                photo_file,
                plate,
                violation_datetime,
            )

        with local_db_conn() as connection:
            cursor = connection.execute(
                """
                INSERT INTO violations(
                    plate_number,
                    state,
                    violation_type,
                    location,
                    notes,
                    warning_level,
                    warning_label,
                    recorded_at,
                    warned_by,
                    entered_by,
                    photo_path,
                    photo_source,
                    status
                )
                VALUES(?,?,?,?,?,?,?,?,?,?,?,?, 'Active')
                """,
                (
                    plate,
                    state,
                    violation,
                    location,
                    notes.strip(),
                    number,
                    label,
                    violation_datetime.isoformat(),
                    warned_by.strip(),
                    entered_by,
                    photo_path,
                    photo_source if photo_path else None,
                ),
            )
            connection.commit()

        return cursor.lastrowid, label, photo_path

    def dismiss(self, record_id, dismissed_by):
        with local_db_conn() as connection:
            connection.execute(
                """
                UPDATE violations
                SET status='Dismissed',
                    dismissed_at=?,
                    dismissed_by=?
                WHERE id=?
                """,
                (
                    app_now().isoformat(),
                    dismissed_by,
                    int(record_id),
                ),
            )
            connection.commit()

    def dashboard_metrics(self):
        today = app_now().date().isoformat()

        with local_db_conn() as connection:
            total = connection.execute(
                "SELECT COUNT(*) AS c FROM violations WHERE status='Active'"
            ).fetchone()["c"]

            today_count = connection.execute(
                """
                SELECT COUNT(*) AS c
                FROM violations
                WHERE status='Active'
                  AND date(recorded_at)=date(?)
                """,
                (today,),
            ).fetchone()["c"]

            unique = connection.execute(
                """
                SELECT COUNT(DISTINCT plate_number) AS c
                FROM violations
                WHERE status='Active'
                """
            ).fetchone()["c"]

            repeat = len(
                connection.execute(
                    """
                    SELECT plate_number
                    FROM violations
                    WHERE status='Active'
                    GROUP BY plate_number
                    HAVING COUNT(*) >= 3
                    """
                ).fetchall()
            )

        return {
            "total_active": int(total),
            "today_count": int(today_count),
            "unique_vehicles": int(unique),
            "repeat_vehicles": int(repeat),
        }

    def top_vehicles(self, limit=25):
        with local_db_conn() as connection:
            rows = connection.execute(
                """
                SELECT
                    plate_number,
                    COUNT(*) AS active_violations,
                    MAX(recorded_at) AS last_seen
                FROM violations
                WHERE status='Active'
                GROUP BY plate_number
                ORDER BY COUNT(*) DESC, MAX(recorded_at) DESC
                LIMIT ?
                """,
                (int(limit),),
            ).fetchall()
        return [dict(row) for row in rows]

    def all_records(self):
        with local_db_conn() as connection:
            rows = connection.execute(
                "SELECT * FROM violations ORDER BY id"
            ).fetchall()
        return [dict(row) for row in rows]

    def photo_bytes(self, photo_path):
        if not photo_path:
            return None
        path = BASE / photo_path
        return path.read_bytes() if path.exists() else None


# ============================================================
# Supabase persistent backend
# ============================================================

def supabase_settings():
    section = _secrets_section("supabase", {})
    try:
        return {
            "url": str(section.get("url", "")).strip(),
            "secret_key": str(section.get("secret_key", "")).strip(),
            "bucket": str(section.get("bucket", "violation-photos")).strip(),
        }
    except Exception:
        return {
            "url": "",
            "secret_key": "",
            "bucket": "violation-photos",
        }


def supabase_configured():
    settings = supabase_settings()
    return bool(settings["url"] and settings["secret_key"])


@st.cache_resource
def get_supabase_client(url, secret_key):
    from supabase import create_client

    return create_client(url, secret_key)


def response_data(response):
    data = getattr(response, "data", None)
    return data if data is not None else []


def response_count(response):
    count = getattr(response, "count", None)
    if count is not None:
        return int(count)
    data = response_data(response)
    return len(data) if isinstance(data, list) else 0


@st.cache_data(ttl=300, show_spinner=False)
def cached_supabase_photo(url, secret_key, bucket, photo_path):
    client = get_supabase_client(url, secret_key)
    return client.storage.from_(bucket).download(photo_path)


class SupabaseBackend:
    kind = "supabase"
    persistent = True
    display_name = "Supabase PostgreSQL + private Storage"

    def __init__(self):
        settings = supabase_settings()
        if not settings["url"] or not settings["secret_key"]:
            raise RuntimeError("Supabase secrets are not configured.")

        self.url = settings["url"]
        self.secret_key = settings["secret_key"]
        self.bucket = settings["bucket"]
        self.client = get_supabase_client(
            self.url,
            self.secret_key,
        )

    def active_count(self, plate):
        response = (
            self.client.table("violations")
            .select("id", count="exact")
            .eq("plate_number", plate)
            .eq("status", "Active")
            .execute()
        )
        return response_count(response)

    def history(self, plate):
        response = (
            self.client.table("violations")
            .select("*")
            .eq("plate_number", plate)
            .order("recorded_at", desc=True)
            .order("id", desc=True)
            .limit(500)
            .execute()
        )
        return response_data(response) or []

    def get_record(self, record_id):
        response = (
            self.client.table("violations")
            .select("*")
            .eq("id", int(record_id))
            .limit(1)
            .execute()
        )
        rows = response_data(response) or []
        return rows[0] if rows else None

    def plate_options(self, limit=1000):
        # Fetch only lightweight plate/date fields, newest first, then deduplicate.
        # This keeps Record Review responsive without loading histories/photos for
        # every vehicle.
        fetch_limit = min(max(int(limit) * 5, 1000), 5000)
        response = (
            self.client.table("violations")
            .select("plate_number,recorded_at")
            .order("recorded_at", desc=True)
            .limit(fetch_limit)
            .execute()
        )

        seen = set()
        plates = []
        for row in response_data(response) or []:
            plate = norm_plate(row.get("plate_number", ""))
            if plate and plate not in seen:
                seen.add(plate)
                plates.append(plate)
                if len(plates) >= int(limit):
                    break
        return plates

    def delete_photo(self, record_id):
        record = self.get_record(record_id)
        if not record:
            raise RuntimeError("Record not found.")

        photo_path = record.get("photo_path")
        if not photo_path:
            return False

        # Remove the private Storage object first. Only clear the database path
        # after Storage confirms the operation without raising an error.
        self.client.storage.from_(self.bucket).remove([photo_path])

        (
            self.client.table("violations")
            .update({"photo_path": None, "photo_source": None})
            .eq("id", int(record_id))
            .execute()
        )

        st.cache_data.clear()
        return True

    def delete_record(self, record_id):
        record = self.get_record(record_id)
        if not record:
            raise RuntimeError("Record not found.")

        photo_path = record.get("photo_path")
        if photo_path:
            # Prevent an orphaned private photo: remove Storage first, then row.
            self.client.storage.from_(self.bucket).remove([photo_path])

        (
            self.client.table("violations")
            .delete()
            .eq("id", int(record_id))
            .execute()
        )

        st.cache_data.clear()
        return True

    def duplicate(self, plate, violation, location, violation_datetime):
        start = violation_datetime - timedelta(minutes=10)
        end = violation_datetime + timedelta(minutes=10)

        response = (
            self.client.table("violations")
            .select("id,recorded_at")
            .eq("plate_number", plate)
            .eq("violation_type", violation)
            .eq("location", location)
            .eq("status", "Active")
            .gte("recorded_at", start.isoformat())
            .lte("recorded_at", end.isoformat())
            .limit(1)
            .execute()
        )

        rows = response_data(response) or []
        return rows[0] if rows else None

    def _upload_photo(self, photo_bytes, plate, violation_datetime):
        path = (
            f"{violation_datetime:%Y/%m}/"
            f"{violation_datetime:%Y%m%d_%H%M%S}_"
            f"{plate}_{uuid.uuid4().hex[:10]}.jpg"
        )

        # The storage3 client used by Streamlit Cloud may internally call
        # open(file, "rb"), so give it a real temporary filesystem path
        # rather than an in-memory BytesIO object.
        temp_path = None
        try:
            with tempfile.NamedTemporaryFile(
                mode="wb",
                suffix=".jpg",
                delete=False,
            ) as temp_file:
                temp_file.write(photo_bytes)
                temp_path = temp_file.name

            self.client.storage.from_(self.bucket).upload(
                path=path,
                file=temp_path,
                file_options={
                    "content-type": "image/jpeg",
                    "cache-control": "3600",
                    "upsert": "false",
                },
            )
            return path
        finally:
            if temp_path:
                try:
                    Path(temp_path).unlink(missing_ok=True)
                except Exception:
                    pass

    def save_violation(
        self,
        plate,
        state,
        violation,
        location,
        notes,
        warned_by,
        entered_by,
        violation_datetime,
        photo_file,
        photo_source,
    ):
        photo_path = None

        if photo_file is not None:
            photo_path = self._upload_photo(
                normalized_photo_bytes(photo_file),
                plate,
                violation_datetime,
            )

        params = {
            "p_plate_number": plate,
            "p_state": state,
            "p_violation_type": violation,
            "p_location": location,
            "p_notes": notes.strip(),
            "p_recorded_at": violation_datetime.isoformat(),
            "p_warned_by": warned_by.strip(),
            "p_entered_by": entered_by,
            "p_photo_path": photo_path,
            "p_photo_source": photo_source if photo_path else None,
        }

        try:
            response = (
                self.client.rpc(
                    "create_parking_violation",
                    params,
                )
                .execute()
            )

            data = response_data(response)
            if isinstance(data, list):
                row = data[0] if data else {}
            elif isinstance(data, dict):
                row = data
            else:
                row = {}

            record_id = row.get("id")
            assigned_label = row.get("warning_label")

            if record_id is None:
                raise RuntimeError(
                    "Supabase saved no record ID. Confirm that "
                    "supabase_setup.sql was executed."
                )

            return int(record_id), str(assigned_label), photo_path

        except Exception:
            if photo_path:
                try:
                    self.client.storage.from_(self.bucket).remove(
                        [photo_path]
                    )
                except Exception:
                    pass
            raise

    def dismiss(self, record_id, dismissed_by):
        (
            self.client.table("violations")
            .update(
                {
                    "status": "Dismissed",
                    "dismissed_at": datetime.now(timezone.utc).isoformat(),
                    "dismissed_by": dismissed_by,
                }
            )
            .eq("id", int(record_id))
            .execute()
        )

    def dashboard_metrics(self):
        response = (
            self.client.rpc(
                "parking_dashboard_metrics",
                {"p_timezone": app_timezone_name()},
            )
            .execute()
        )

        data = response_data(response)
        if isinstance(data, list):
            row = data[0] if data else {}
        else:
            row = data or {}

        return {
            "total_active": int(row.get("total_active", 0)),
            "today_count": int(row.get("today_count", 0)),
            "unique_vehicles": int(row.get("unique_vehicles", 0)),
            "repeat_vehicles": int(row.get("repeat_vehicles", 0)),
        }

    def top_vehicles(self, limit=25):
        response = (
            self.client.rpc(
                "parking_top_vehicles",
                {"p_limit": int(limit)},
            )
            .execute()
        )
        return response_data(response) or []

    def all_records(self):
        rows = []
        page_size = 1000
        offset = 0

        while True:
            response = (
                self.client.table("violations")
                .select("*")
                .order("id")
                .range(offset, offset + page_size - 1)
                .execute()
            )
            page = response_data(response) or []
            rows.extend(page)

            if len(page) < page_size:
                break

            offset += page_size

        return rows

    def photo_bytes(self, photo_path):
        if not photo_path:
            return None

        try:
            return cached_supabase_photo(
                self.url,
                self.secret_key,
                self.bucket,
                photo_path,
            )
        except Exception:
            return None


def get_backend():
    mode = deployment_mode()

    if supabase_configured():
        try:
            return SupabaseBackend()
        except Exception as exc:
            if mode == "production":
                st.error("Persistent backend initialization failed.")
                st.exception(exc)
                st.stop()

    if mode == "production":
        st.error(
            "Production mode requires Supabase persistent storage. "
            "Add [supabase] secrets before using production mode."
        )
        st.stop()

    return LocalBackend()


# ============================================================
# Phone image normalization / ALPR
# ============================================================

def open_phone_image(data):
    with Image.open(io.BytesIO(data)) as source:
        image = ImageOps.exif_transpose(source).convert("RGB")
        image.load()
    return image


def resize_image(image, max_side=2200):
    width, height = image.size
    longest = max(width, height)

    if longest <= max_side:
        return image

    scale = max_side / longest
    return image.resize(
        (
            max(1, round(width * scale)),
            max(1, round(height * scale)),
        ),
        Image.Resampling.LANCZOS,
    )


def pil_to_bgr(image):
    return cv2.cvtColor(
        np.asarray(image),
        cv2.COLOR_RGB2BGR,
    )


def pil_to_jpeg(image, quality=92):
    buffer = io.BytesIO()
    image.save(
        buffer,
        format="JPEG",
        quality=quality,
        optimize=True,
    )
    return buffer.getvalue()


def image_variants(image):
    image = resize_image(image)
    width, height = image.size

    return [
        ("Full corrected image", image),
        (
            "Center crop",
            image.crop(
                (
                    int(width * 0.08),
                    int(height * 0.15),
                    int(width * 0.92),
                    int(height * 0.92),
                )
            ),
        ),
        (
            "Lower crop",
            image.crop(
                (
                    0,
                    int(height * 0.30),
                    width,
                    height,
                )
            ),
        ),
    ]


@st.cache_resource
def alpr_engine():
    from fast_alpr import ALPR

    return ALPR(
        detector_model="yolo-v9-t-384-license-plate-end2end",
        detector_conf_thresh=0.25,
        ocr_model="cct-xs-v2-global-model",
        ocr_device="cpu",
    )


def ocr_confidence(ocr):
    value = getattr(ocr, "confidence", None)

    if value is None:
        return 0.0

    if isinstance(value, (list, tuple)):
        values = [
            float(item)
            for item in value
            if item is not None
        ]
        return statistics.mean(values) if values else 0.0

    try:
        return float(value)
    except Exception:
        return 0.0


def detection_confidence(detection):
    for name in ("confidence", "score", "conf"):
        value = getattr(detection, name, None)
        if value is not None:
            try:
                return float(value)
            except Exception:
                pass
    return 0.0


@st.cache_data(show_spinner=False)
def run_alpr(data):
    image = resize_image(open_phone_image(data))
    engine = alpr_engine()

    all_rows = []
    best_annotated = None
    best_name = None
    best_score = -1.0

    orientations = [
        ("0°", image),
        ("90°", image.rotate(90, expand=True)),
        ("180°", image.rotate(180, expand=True)),
        ("270°", image.rotate(270, expand=True)),
    ]

    for orientation_name, oriented_image in orientations:
        for crop_name, variant in image_variants(oriented_image):
            view_name = f"{orientation_name} — {crop_name}"
            drawn = engine.draw_predictions(
                pil_to_bgr(variant).copy()
            )

            local_best = -1.0

            for index, result in enumerate(
                drawn.results,
                start=1,
            ):
                detection = getattr(result, "detection", None)
                ocr = getattr(result, "ocr", None)

                raw_text = (
                    str(getattr(ocr, "text", "") or "")
                    if ocr
                    else ""
                )
                plate = norm_plate(raw_text)

                ocr_score = (
                    ocr_confidence(ocr)
                    if ocr
                    else 0.0
                )
                detector_score = (
                    detection_confidence(detection)
                    if detection
                    else 0.0
                )

                rank = (
                    ocr_score
                    + (0.20 if plate else 0.0)
                    + (0.05 * detector_score)
                )

                local_best = max(
                    local_best,
                    rank,
                )

                all_rows.append(
                    {
                        "orientation": orientation_name,
                        "variant": crop_name,
                        "detection": index,
                        "plate": plate,
                        "raw_text": raw_text,
                        "ocr_confidence": round(ocr_score, 3),
                        "detection_confidence": round(
                            detector_score,
                            3,
                        ),
                        "_rank": rank,
                    }
                )

            if local_best > best_score:
                best_score = local_best
                best_name = view_name
                best_annotated = cv2.cvtColor(
                    drawn.image,
                    cv2.COLOR_BGR2RGB,
                )

    best_by_plate = {}

    for row in all_rows:
        plate = row["plate"]

        if (
            plate
            and (
                plate not in best_by_plate
                or row["_rank"]
                > best_by_plate[plate]["_rank"]
            )
        ):
            best_by_plate[plate] = row.copy()

    readable = sorted(
        best_by_plate.values(),
        key=lambda row: row["_rank"],
        reverse=True,
    )

    diagnostics = sorted(
        all_rows,
        key=lambda row: row["_rank"],
        reverse=True,
    )

    for collection in (
        readable,
        diagnostics,
    ):
        for row in collection:
            row.pop("_rank", None)

    return {
        "readable": readable,
        "diagnostics": diagnostics,
        "annotated": best_annotated,
        "best_variant": best_name,
        "normalized_jpeg": pil_to_jpeg(image),
        "size": image.size,
    }


def reset_alpr():
    for key in (
        "alpr_candidates",
        "alpr_diagnostics",
        "alpr_annotated",
        "alpr_variant",
        "alpr_error",
        "normalized_preview",
        "normalized_size",
    ):
        st.session_state.pop(key, None)


def process_photo_widget(widget_key):
    photo = st.session_state.get(widget_key)
    reset_alpr()

    if photo is None:
        st.session_state["plate_confirmed"] = False
        return

    try:
        result = run_alpr(photo.getvalue())

        st.session_state["alpr_candidates"] = result["readable"]
        st.session_state["alpr_diagnostics"] = result["diagnostics"]
        st.session_state["alpr_annotated"] = result["annotated"]
        st.session_state["alpr_variant"] = result["best_variant"]
        st.session_state["normalized_preview"] = result["normalized_jpeg"]
        st.session_state["normalized_size"] = result["size"]

        st.session_state["record_plate"] = (
            result["readable"][0]["plate"]
            if result["readable"]
            else ""
        )
        st.session_state["plate_confirmed"] = False

    except Exception as exc:
        st.session_state["alpr_error"] = str(exc)
        st.session_state["plate_confirmed"] = False


def uppercase_plate():
    st.session_state["record_plate"] = norm_plate(
        st.session_state.get("record_plate", "")
    )
    st.session_state["plate_confirmed"] = False


def choose_plate(plate):
    st.session_state["record_plate"] = norm_plate(plate)
    st.session_state["plate_confirmed"] = False


def normalized_photo_bytes(uploaded_file):
    image = open_phone_image(
        uploaded_file.getvalue()
    )
    image = resize_image(
        image,
        max_side=2600,
    )
    return pil_to_jpeg(
        image,
        quality=93,
    )


def save_local_photo(
    uploaded_file,
    plate,
    violation_datetime,
):
    filename = (
        f"{violation_datetime:%Y%m%d_%H%M%S}_"
        f"{plate}_{uuid.uuid4().hex[:8]}.jpg"
    )
    path = LOCAL_PHOTOS / filename
    path.write_bytes(
        normalized_photo_bytes(uploaded_file)
    )
    return str(path.relative_to(BASE))


# ============================================================
# Display helpers
# ============================================================

def show_history(df):
    if df.empty:
        st.info("No records found.")
        return

    columns = [
        "id",
        "recorded_at_display",
        "warned_by",
        "entered_by",
        "violation_type",
        "location",
        "warning_label",
        "Photo",
        "status",
        "notes",
    ]

    view = df[columns].copy()
    view.columns = [
        "Record ID",
        "Violation Date / Time",
        "Warning Given By",
        "Entered By",
        "Violation",
        "Location",
        "Warning",
        "Photo",
        "Status",
        "Notes",
    ]

    st.dataframe(
        view,
        width="stretch",
        hide_index=True,
    )


def show_photo(backend, record):
    if not record or not record.get("photo_path"):
        st.info("No photo is attached to this violation.")
        return

    photo = backend.photo_bytes(
        record["photo_path"]
    )

    if photo:
        st.image(
            photo,
            caption=(
                f"Record #{record['id']} — "
                f"{record['plate_number']}"
            ),
            width="stretch",
        )
    else:
        st.warning(
            "The photo could not be retrieved from storage."
        )


# ============================================================
# App initialization
# ============================================================

st.set_page_config(
    page_title="Church Parking Violation Tracker",
    page_icon="🚗",
    layout="wide",
)

st.markdown(
    """
    <style>
    .st-key-record_plate input {
        text-transform: uppercase;
        font-weight: 650;
        letter-spacing: .06em;
    }
    </style>
    """,
    unsafe_allow_html=True,
)

require_login()
user = current_user()
backend = get_backend()
mode = deployment_mode()

st.session_state.setdefault(
    "record_plate",
    "",
)
st.session_state.setdefault(
    "warning_person",
    user["display_name"],
)
st.session_state.setdefault(
    "plate_confirmed",
    False,
)

with st.sidebar:
    st.markdown("### Signed in")
    st.write(user["display_name"])
    st.caption(
        f"Role: {user['role'].title()}"
    )

    st.markdown("### Data storage")
    if backend.kind == "supabase":
        st.success("Persistent cloud database + private photo storage")
    else:
        st.info("Local SQLite + local photo storage")

    st.caption(
        f"Timezone: {app_timezone_name()}"
    )

    if st.button(
        "Log out",
        width="stretch",
    ):
        logout_user()
        st.rerun()

st.title("🚗 Church Parking Violation Tracker 🛻")
st.caption(
    "Version 1.0.2 — persistent storage, cleaner ALPR, and admin record management, "
    "secure login, and automatic plate recognition"
)

if mode == "production" and backend.kind == "supabase":
    st.success(
        "Production mode: violations and photos are stored persistently in Supabase."
    )
elif backend.kind == "supabase":
    st.info(
        "Supabase persistent backend is connected. "
        "Set [deployment] mode = 'production' when ready for live use."
    )
else:
    st.warning(
        "Local development mode. Cloud production requires Supabase secrets."
    )

record_tab, search_tab, dashboard_tab, admin_tab = st.tabs(
    [
        "➕ Record Violation",
        "🔎 Search Vehicle",
        "📊 Dashboard",
        "⚙️ Record Review",
    ]
)


# ============================================================
# Record violation
# ============================================================

with record_tab:
    st.subheader("Record a Parking Violation")

    entry_method = st.radio(
        "License plate entry method",
        [
            "Direct Camera",
            "Phone Photo / Upload",
            "Manual Entry",
        ],
    )

    photo = None
    photo_source = None

    if entry_method == "Direct Camera":
        photo = st.camera_input(
            "Take a vehicle/license plate photo",
            key="direct_camera",
            on_change=process_photo_widget,
            args=("direct_camera",),
            resolution="1080p",
            width="stretch",
        )
        photo_source = "Direct Camera"

    elif entry_method == "Phone Photo / Upload":
        photo = st.file_uploader(
            "Take or choose a vehicle/license plate photo",
            type=[
                "jpg",
                "jpeg",
                "png",
                "webp",
                "heic",
                "heif",
            ],
            key="photo_upload",
            on_change=process_photo_widget,
            args=("photo_upload",),
        )
        photo_source = "Phone Photo / Upload"

    else:
        reset_alpr()
        st.session_state["plate_confirmed"] = True

    alpr_error = st.session_state.get("alpr_error")

    if alpr_error:
        st.error(
            "Automatic plate recognition could not process this image. "
            "You can still enter the plate manually."
        )
        with st.expander("Technical detail"):
            st.code(alpr_error)

    preview = st.session_state.get(
        "normalized_preview"
    )

    if photo is not None:
        if preview:
            st.image(
                preview,
                caption=(
                    "Photo after orientation/format normalization"
                ),
                width="stretch",
            )
        else:
            st.image(
                photo,
                caption="Vehicle photo",
                width="stretch",
            )

    annotated = st.session_state.get(
        "alpr_annotated"
    )
    candidates = st.session_state.get(
        "alpr_candidates",
        [],
    )

    if photo is not None:
        st.markdown(
            "#### Automatic Plate Detection"
        )

        if annotated is not None:
            st.image(
                annotated,
                caption=(
                    "Best ALPR view: "
                    f"{st.session_state.get('alpr_variant') or 'Best view'}"
                ),
                width="stretch",
            )

        if candidates:
            best = candidates[0]
            st.success(
                f"Detected plate: **{best['plate']}**"
            )

            options = [
                candidate["plate"]
                for candidate in candidates
            ]

            if len(options) > 1:
                selected = st.selectbox(
                    "Other detected plates",
                    options,
                )
                st.button(
                    "Use Selected Plate",
                    width="content",
                    on_click=choose_plate,
                    args=(selected,),
                )

        elif not alpr_error:
            st.warning(
                "No readable plate was found. "
                "Try a closer/clearer photo or type the plate manually."
            )

        if user["role"] == "admin":
            with st.expander(
                "ALPR Diagnostic / Troubleshooting (Admin only)"
            ):
                diagnostics = (
                    st.session_state.get(
                        "alpr_diagnostics",
                        [],
                    )
                )

                if diagnostics:
                    st.dataframe(
                        pd.DataFrame(diagnostics),
                        width="stretch",
                        hide_index=True,
                    )
                else:
                    st.write(
                        "No plate detections were returned."
                    )

    st.markdown(
        "#### Violation Information"
    )

    plate_col, state_col = st.columns(
        [2, 1]
    )

    raw_plate = plate_col.text_input(
        "License Plate",
        key="record_plate",
        placeholder="ABC1234",
        on_change=uppercase_plate,
    )

    state = state_col.selectbox(
        "State / Jurisdiction",
        ["TX", "Other"],
        index=0,
    )

    plate = norm_plate(raw_plate)

    if photo is not None:
        confirmed = st.checkbox(
            "I checked the photo and confirm the license plate above is correct.",
            key="plate_confirmed",
        )
    else:
        confirmed = bool(plate)
        st.session_state[
            "plate_confirmed"
        ] = confirmed

    now = app_now()

    date_col, time_col, person_col = st.columns(
        [1, 1, 2]
    )

    violation_date = date_col.date_input(
        "Violation Date",
        value=now.date(),
    )

    violation_time = time_col.time_input(
        "Violation Time",
        value=now.time().replace(
            second=0,
            microsecond=0,
        ),
    )

    warned_by = person_col.text_input(
        "Person Who Gave the Warning",
        key="warning_person",
        placeholder="Required",
    )

    violation = st.selectbox(
        "Violation Type",
        VIOLATIONS,
        index=5,
    )

    location = st.selectbox(
        "Parking Location",
        LOCATIONS,
        index=0,
    )

    notes = st.text_area(
        "Notes",
        placeholder="Optional",
    )

    violation_datetime = datetime.combine(
        violation_date,
        violation_time,
        tzinfo=app_tz(),
    )

    if plate:
        previous = backend.active_count(plate)
        current = previous + 1

        metric_1, metric_2, metric_3 = st.columns(3)

        metric_1.metric(
            "Previous Active Violations",
            previous,
        )
        metric_2.metric(
            "This Would Be Violation",
            f"#{current}",
        )
        metric_3.metric(
            "Warning Level",
            warning_label(current),
        )

        if current == 2:
            st.warning(
                "This vehicle will receive a SECOND WARNING."
            )
        elif current == 3:
            st.error(
                "This vehicle will receive a THIRD / FINAL WARNING."
            )
        elif current > 3:
            st.error(
                "Repeat violation — follow the church administrative procedure."
            )

        history_rows = backend.history(plate)

        if history_rows:
            with st.expander(
                "Previous history for this plate"
            ):
                show_history(
                    rows_to_history_df(
                        history_rows
                    )
                )

    duplicate_record = (
        backend.duplicate(
            plate,
            violation,
            location,
            violation_datetime,
        )
        if plate
        else None
    )

    duplicate_confirmed = False

    if duplicate_record:
        st.warning(
            f"Possible duplicate: Record #{duplicate_record['id']} "
            "is within 10 minutes for the same plate, violation, and location."
        )

        duplicate_confirmed = st.checkbox(
            "I reviewed the nearby record and still want to save another violation."
        )

    missing = []

    if not plate:
        missing.append(
            "License Plate"
        )

    if not warned_by.strip():
        missing.append(
            "Person Who Gave the Warning"
        )

    if (
        photo is not None
        and not confirmed
    ):
        missing.append(
            "Plate confirmation"
        )

    if (
        duplicate_record is not None
        and not duplicate_confirmed
    ):
        missing.append(
            "Duplicate confirmation"
        )

    if missing:
        st.info(
            "Complete before saving: "
            + ", ".join(missing)
        )
    else:
        st.success(
            "Required information is complete. You can save."
        )

    if st.button(
        "💾 Save Violation",
        type="primary",
        disabled=bool(missing),
        width="stretch",
    ):
        try:
            record_id, assigned_label, saved_photo = (
                backend.save_violation(
                    plate=plate,
                    state=state,
                    violation=violation,
                    location=location,
                    notes=notes,
                    warned_by=warned_by,
                    entered_by=user["display_name"],
                    violation_datetime=violation_datetime,
                    photo_file=photo,
                    photo_source=photo_source,
                )
            )

            st.success(
                f"Saved Record #{record_id} for plate {plate}. "
                f"Assigned: {assigned_label}."
                + (
                    " Photo saved to private persistent storage."
                    if saved_photo
                    else ""
                )
            )

            st.cache_data.clear()

        except Exception as exc:
            st.error(
                "The violation could not be saved."
            )
            with st.expander(
                "Technical detail"
            ):
                st.exception(exc)


# ============================================================
# Search
# ============================================================

with search_tab:
    st.subheader(
        "Search Vehicle History"
    )

    search_raw = st.text_input(
        "Enter License Plate",
        key="search_plate",
        placeholder="ABC1234",
    )

    if st.button(
        "Search",
        type="primary",
    ):
        st.session_state[
            "searched_plate"
        ] = norm_plate(
            search_raw
        )

    searched_plate = (
        st.session_state.get(
            "searched_plate",
            "",
        )
    )

    if searched_plate:
        rows = backend.history(
            searched_plate
        )

        if not rows:
            st.info(
                f"No records found for {searched_plate}."
            )
        else:
            history_df = rows_to_history_df(
                rows
            )

            st.markdown(
                f"### {searched_plate}"
            )

            active = int(
                (
                    history_df["status"]
                    == "Active"
                ).sum()
            )

            col_1, col_2 = st.columns(2)

            col_1.metric(
                "Active Violations",
                active,
            )
            col_2.metric(
                "Current Status",
                warning_label(active),
            )

            show_history(
                history_df
            )

            photo_rows = history_df[
                history_df["photo_path"] != ""
            ]

            if not photo_rows.empty:
                st.markdown(
                    "#### Attached Photo"
                )

                selected_record_id = (
                    st.selectbox(
                        "Select Record ID",
                        photo_rows["id"]
                        .astype(int)
                        .tolist(),
                    )
                )

                show_photo(
                    backend,
                    backend.get_record(
                        selected_record_id
                    ),
                )


# ============================================================
# Dashboard
# ============================================================

with dashboard_tab:
    if user["role"] != "admin":
        st.warning(
            "Administrator access is required for the Dashboard."
        )
    else:
        st.subheader(
            "Parking Dashboard"
        )

        try:
            metrics = backend.dashboard_metrics()

            metric_1, metric_2, metric_3, metric_4 = (
                st.columns(4)
            )

            metric_1.metric(
                "Active Violations",
                metrics["total_active"],
            )
            metric_2.metric(
                "Recorded Today",
                metrics["today_count"],
            )
            metric_3.metric(
                "Unique Vehicles",
                metrics["unique_vehicles"],
            )
            metric_4.metric(
                "Vehicles With 3+ Violations",
                metrics["repeat_vehicles"],
            )

            top_rows = backend.top_vehicles(
                limit=25
            )

            if top_rows:
                summary = pd.DataFrame(
                    top_rows
                )

                if "last_seen" in summary.columns:
                    summary["last_seen"] = (
                        summary["last_seen"]
                        .apply(display_datetime)
                    )

                summary = summary.rename(
                    columns={
                        "plate_number": "Plate",
                        "active_violations": "Active Violations",
                        "last_seen": "Last Seen",
                    }
                )

                st.markdown(
                    "#### Active Vehicle Summary"
                )
                st.dataframe(
                    summary,
                    width="stretch",
                    hide_index=True,
                )

            st.markdown(
                "#### Backup / Export"
            )

            all_rows = backend.all_records()
            export_df = pd.DataFrame(
                all_rows
            )

            if not export_df.empty:
                st.download_button(
                    "Download violation records as CSV",
                    data=export_df.to_csv(
                        index=False
                    ).encode("utf-8"),
                    file_name=(
                        f"parking_violations_"
                        f"{app_now():%Y%m%d_%H%M}.csv"
                    ),
                    mime="text/csv",
                    width="content",
                )

        except Exception as exc:
            st.error(
                "Dashboard data could not be loaded."
            )
            with st.expander(
                "Technical detail"
            ):
                st.exception(exc)


# ============================================================
# Admin record review
# ============================================================

with admin_tab:
    if user["role"] != "admin":
        st.warning(
            "Administrator access is required for Record Review."
        )
    else:
        st.subheader("Record Review & Administration")
        st.caption(
            "Select a license plate, then choose the specific violation record. "
            "Use Dismiss for normal corrections. Permanent deletion is intended "
            "only for records that truly should not remain in the system."
        )

        try:
            plate_options = backend.plate_options(limit=1000)
        except Exception as exc:
            plate_options = []
            st.error("License plate list could not be loaded.")
            with st.expander("Technical detail"):
                st.exception(exc)

        selected_plate = st.selectbox(
            "License Plate",
            options=plate_options,
            index=None,
            placeholder="Type or select a license plate...",
            key="admin_review_plate",
        )

        record = None

        if selected_plate:
            review_rows = backend.history(selected_plate)

            if not review_rows:
                st.info("No records were found for this license plate.")
            else:
                record_by_label = {}
                record_labels = []

                for row in review_rows:
                    label = (
                        f"{display_datetime(row.get('recorded_at'))}  •  "
                        f"{row.get('violation_type', '')}  •  "
                        f"{row.get('warning_label', '')}  •  "
                        f"{row.get('status', '')}  •  Record #{row.get('id')}"
                    )
                    record_labels.append(label)
                    record_by_label[label] = row

                previous_record_label = st.session_state.get(
                    "admin_review_record_label"
                )
                if previous_record_label not in record_labels:
                    st.session_state.pop("admin_review_record_label", None)

                selected_record_label = st.selectbox(
                    "Violation Record",
                    options=record_labels,
                    key="admin_review_record_label",
                )

                record = record_by_label.get(selected_record_label)

        if record:
            st.markdown("#### Selected Record")

            c1, c2, c3, c4 = st.columns(4)
            c1.metric("Record ID", record.get("id", ""))
            c2.metric("Plate", record.get("plate_number", ""))
            c3.metric("Status", record.get("status", ""))
            c4.metric("Warning", record.get("warning_label", ""))

            detail_rows = {
                "Violation Date / Time": display_datetime(record.get("recorded_at")),
                "Violation": record.get("violation_type", ""),
                "Location": record.get("location", ""),
                "Warning Given By": record.get("warned_by", ""),
                "Entered By": record.get("entered_by", ""),
                "State / Jurisdiction": record.get("state", ""),
                "Notes": record.get("notes", "") or "",
            }

            st.dataframe(
                pd.DataFrame(
                    [{"Field": key, "Value": value} for key, value in detail_rows.items()]
                ),
                width="stretch",
                hide_index=True,
            )

            if record.get("photo_path"):
                with st.expander("View violation photo", expanded=True):
                    show_photo(backend, record)
            else:
                st.info("This record does not currently have a stored photo.")

            st.markdown("#### Record Actions")

            if record.get("status") == "Active":
                dismiss_confirmed = st.checkbox(
                    "I confirm this record should be dismissed and no longer "
                    "count toward active warning levels.",
                    key=f"dismiss_confirm_{record['id']}",
                )

                if st.button(
                    "Dismiss Record",
                    disabled=not dismiss_confirmed,
                    key=f"dismiss_record_{record['id']}",
                ):
                    try:
                        backend.dismiss(
                            record["id"],
                            user["display_name"],
                        )
                        st.cache_data.clear()
                        st.session_state.pop("admin_review_record_label", None)
                        st.success(
                            f"Record #{record['id']} was dismissed. "
                            "Its history remains available."
                        )
                        st.rerun()
                    except Exception as exc:
                        st.error("The record could not be dismissed.")
                        with st.expander("Technical detail"):
                            st.exception(exc)
            else:
                st.info(
                    "This record is already dismissed and does not count toward "
                    "active warning levels."
                )

            if record.get("photo_path"):
                with st.expander("Photo Storage Management"):
                    st.write(
                        "Delete only the stored photo while keeping the violation "
                        "record, warning history, date, notes, and audit information."
                    )
                    photo_delete_confirmed = st.checkbox(
                        "I understand the photo will be permanently removed but "
                        "the violation record will remain.",
                        key=f"photo_delete_confirm_{record['id']}",
                    )

                    if st.button(
                        "Delete Photo Only",
                        disabled=not photo_delete_confirmed,
                        key=f"delete_photo_{record['id']}",
                    ):
                        try:
                            deleted = backend.delete_photo(record["id"])
                            if deleted:
                                st.success(
                                    "The photo was permanently deleted. "
                                    "The violation record was preserved."
                                )
                            else:
                                st.info("This record had no stored photo to delete.")
                            st.cache_data.clear()
                            st.rerun()
                        except Exception as exc:
                            st.error("The photo could not be deleted.")
                            with st.expander("Technical detail"):
                                st.exception(exc)

            with st.expander("Danger Zone — Permanently Delete Record"):
                st.error(
                    "Permanent deletion removes this violation from the database. "
                    "If a photo is attached, the photo is permanently removed too. "
                    "This changes the active/history record for this vehicle. "
                    "Use Dismiss instead when you want to preserve an audit trail."
                )

                expected = f"DELETE {record.get('plate_number', '')}"
                delete_text = st.text_input(
                    f"Type `{expected}` to confirm",
                    key=f"delete_record_text_{record['id']}",
                )

                delete_enabled = delete_text.strip().upper() == expected.upper()

                if st.button(
                    "Permanently Delete Record",
                    type="primary",
                    disabled=not delete_enabled,
                    key=f"delete_record_{record['id']}",
                ):
                    try:
                        deleted_id = record["id"]
                        deleted_plate = record.get("plate_number", "")
                        backend.delete_record(deleted_id)
                        st.cache_data.clear()
                        st.session_state.pop("admin_review_record_label", None)
                        st.success(
                            f"Record #{deleted_id} for {deleted_plate} was "
                            "permanently deleted."
                        )
                        st.rerun()
                    except Exception as exc:
                        st.error("The record could not be permanently deleted.")
                        with st.expander("Technical detail"):
                            st.exception(exc)


st.divider()
st.caption(
    "Automatic plate recognition assists the user. "
    "Always visually confirm the plate before saving. "
    "Production records and photos are stored in a private persistent backend."
)
