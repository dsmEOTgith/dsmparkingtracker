import getpass
import bcrypt

print("Church Parking Tracker - Password Hash Generator")
password = getpass.getpass("Enter password: ")
confirm = getpass.getpass("Confirm password: ")

if password != confirm:
    raise SystemExit("Passwords do not match.")

if len(password) < 10:
    raise SystemExit("Use a password with at least 10 characters.")

hashed = bcrypt.hashpw(
    password.encode("utf-8"),
    bcrypt.gensalt(rounds=12),
).decode("utf-8")

print("\nCopy this into password_hash in Streamlit Secrets:\n")
print(hashed)
