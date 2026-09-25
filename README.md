# Church Parking Violation Tracker — v1.0.1

Version 1.0.1 adds **persistent production storage** and includes a Streamlit Cloud photo-upload compatibility hotfix.


## v1.0.1 photo upload hotfix

v1.0 could fail on Streamlit Cloud with:

`TypeError: expected str, bytes or os.PathLike object, not BytesIO`

The installed `storage3` client was attempting to open the supplied upload
object as a filesystem path. v1.0.1 writes the already-normalized JPEG to a
short-lived temporary `.jpg` file, uploads that filepath to the private
Supabase bucket, and deletes the temporary file immediately afterward.

The Supabase database schema and SQL functions are unchanged.

**You do not need to rerun `supabase_setup.sql`.**

## Architecture

### Streamlit
- HTTPS web/mobile user interface
- Admin / Parking Volunteer login
- Direct camera and photo upload
- FastALPR automatic plate recognition
- 0° / 90° / 180° / 270° rotation handling

### Supabase
- PostgreSQL persistent violation database
- Private persistent Storage bucket for violation photos
- Server-side secret key stored only in Streamlit Secrets

### Local development
If Supabase secrets are absent and deployment mode is not `production`,
the app still runs with the existing local SQLite database and local photo folder.

---

# 1. Create a Supabase project

Create a project at Supabase.

From the project dashboard, obtain:

- **Project URL**
- **Secret API key** beginning with `sb_secret_...`

Use the server-side Secret key, not the publishable key.

Do not place the Secret key in GitHub.

---

# 2. Create the persistent table and private photo bucket

Open:

**Supabase Dashboard → SQL Editor**

Copy the entire contents of:

`supabase_setup.sql`

and run it once.

It creates:

- `public.violations`
- indexes
- atomic warning-assignment function
- dashboard functions
- private `violation-photos` Storage bucket

The database table has Row Level Security enabled and no direct browser access.
The Streamlit server uses the Supabase Secret key.

---

# 3. Update Streamlit Secrets

In Streamlit Community Cloud:

**App → Manage app → Settings → Secrets**

Add the Supabase section to your existing login secrets.

Example:

```toml
[deployment]
mode = "production"

[app]
timezone = "America/Chicago"

[auth]
enabled = true

[users.admin]
display_name = "Church Administrator"
role = "admin"
password_hash = "$2b$12$YOUR_EXISTING_ADMIN_HASH"

[users.parking]
display_name = "Parking Volunteer"
role = "volunteer"
password_hash = "$2b$12$YOUR_EXISTING_VOLUNTEER_HASH"

[supabase]
url = "https://YOUR_PROJECT_REF.supabase.co"
secret_key = "sb_secret_YOUR_SECRET_KEY"
bucket = "violation-photos"
```

You can reuse the same bcrypt login hashes from v0.9.

---


## Verify Supabase before production

After creating your local `.streamlit/secrets.toml`, run:

```powershell
.\.venv\Scripts\python.exe verify_supabase.py
```

It verifies:

- the `violations` table
- dashboard RPC
- top-vehicles RPC
- the private Storage bucket

You should see:

`Supabase v1.0 backend verification PASSED.`

---

# 4. Push v1.0 files to GitHub

Upload / commit:

- `app.py`
- `requirements.txt`
- `README.md`
- `run_app.bat`
- `.gitignore`
- `.streamlit/config.toml`
- `.streamlit/secrets.toml.example`
- `supabase_setup.sql`
- `generate_password_hash.py`
- `migrate_local_to_supabase.py`
- `verify_supabase.py`

Do **not** upload:

- `.streamlit/secrets.toml`
- `.venv`
- `parking_tracker.db`
- `violation_photos`

After pushing, Streamlit Community Cloud should redeploy automatically.

---

# 5. Confirm production connection

After login, the sidebar should show:

**Persistent cloud database + private photo storage**

The top of the app should show:

**Production mode: violations and photos are stored persistently in Supabase.**

Test with a fake plate first.

Then restart/reboot the Streamlit app and confirm the test record and photo still exist.
That verifies persistence.

---

# 6. Optional: migrate your existing local v0.x records

First back up:

- `parking_tracker.db`
- `violation_photos`

Then make sure your **local** `.streamlit/secrets.toml` contains the Supabase settings.

Run:

```powershell
.\.venv\Scripts\python.exe migrate_local_to_supabase.py
```

The migration script:

- preserves existing violation history
- preserves warning levels
- copies available photos to private Supabase Storage
- avoids re-importing the same local record by using `legacy_local_id`

Run it once, review the results, and verify records in the app.

---

# Security notes

- The Supabase Secret key bypasses Row Level Security. It belongs only in
  Streamlit Secrets / local `secrets.toml`, never in GitHub or browser code.
- The photo bucket is private.
- App users do not receive the Supabase key.
- Photos are downloaded server-side only after the user has authenticated.
- Keep the GitHub repository private if possible.
- Retain license-plate records/photos only as long as needed for the church's
  parking-management policy.

---

# v1.0 production test checklist

- [ ] Admin login works
- [ ] Volunteer login works
- [ ] Direct phone camera works over HTTPS
- [ ] Plate is detected automatically
- [ ] Rotated photo is detected
- [ ] Violation saves
- [ ] Photo saves
- [ ] Search finds the vehicle
- [ ] Photo opens from Search
- [ ] Warning count increments
- [ ] Admin can dismiss a record
- [ ] Dismissed record stops counting toward the next warning
- [ ] Dashboard loads
- [ ] CSV export works
- [ ] App restart does not remove records
- [ ] App restart does not remove photos
