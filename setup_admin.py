import os
import sys
from pathlib import Path

# Set up project path and Django environment
BASE_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(BASE_DIR))

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "intellidrain.settings")

import django
django.setup()

from django.conf import settings
from django.contrib.auth import get_user_model, authenticate

db_path = settings.DATABASES['default']['NAME']
resolved_db_path = Path(db_path).resolve()
print(f"DATABASE_PATH_RAW: {db_path}")
print(f"DATABASE_PATH_RESOLVED: {resolved_db_path}")
print(f"AUTH_USER_MODEL: {getattr(settings, 'AUTH_USER_MODEL', 'Default (auth.User)')}")
print(f"AUTHENTICATION_BACKENDS: {getattr(settings, 'AUTHENTICATION_BACKENDS', ['django.contrib.auth.backends.ModelBackend'])}")

User = get_user_model()
print(f"Resolved User Model: {User.__module__}.{User.__name__}")

# Inspect existing users
print("\n--- Existing Users Before Update ---")
users = list(User.objects.all())
if not users:
    print("No users found in database.")
for u in users:
    print(f"User: id={u.id}, username='{u.username}', email='{u.email}', is_staff={u.is_staff}, is_superuser={u.is_superuser}, is_active={u.is_active}")

from dotenv import load_dotenv
load_dotenv()

# Create or update superuser
username = os.environ.get("DJANGO_SUPERUSER_USERNAME")
password = os.environ.get("DJANGO_SUPERUSER_PASSWORD")
email = os.environ.get("DJANGO_SUPERUSER_EMAIL", "")

if not username or not password:
    import getpass
    print("Environment variables DJANGO_SUPERUSER_USERNAME / DJANGO_SUPERUSER_PASSWORD not set.")
    print("Please enter superuser credentials interactively (password will not be echoed):")
    if not username:
        username = input("Username: ").strip()
    if not password:
        password = getpass.getpass("Password: ").strip()

if not username or not password:
    print("Error: Both username and password are required to create/update a superuser.")
    sys.exit(1)

user, created = User.objects.get_or_create(username=username)
if email:
    user.email = email
user.is_staff = True
user.is_superuser = True
user.is_active = True
user.set_password(password)
user.save()

action = "Created" if created else "Updated"
print(f"\n--- Superuser Setup ---")
print(f"{action} superuser '{username}' successfully.")
print(f"Attributes: is_staff={user.is_staff}, is_superuser={user.is_superuser}, is_active={user.is_active}")

# Test direct authentication
print("\n--- Authentication Test ---")
auth_user = authenticate(username=username, password=password)
if auth_user is not None:
    print(f"SUCCESS: Authentication check succeeded for user '{username}'.")
else:
    print("FAILURE: authenticate() returned None! Check backends or password hash.")
