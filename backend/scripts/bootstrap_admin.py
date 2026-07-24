"""One-time setup: create the default workspace and the first Admin user.

Not an Alembic migration on purpose — this touches a real secret (the admin
password), so it's an explicit, interactive step you run once, not something
baked into schema history.

Usage (after `alembic upgrade head` has run against a live Postgres):
    python scripts/bootstrap_admin.py

Non-interactive mode (CI, automation, or any environment where getpass can't
get a real TTY — it hangs rather than failing cleanly when stdin is piped,
which is worse than just supporting an explicit non-interactive path):
    BOOTSTRAP_ADMIN_EMAIL=... BOOTSTRAP_ADMIN_PASSWORD=... python scripts/bootstrap_admin.py
    (BOOTSTRAP_ADMIN_DISPLAY_NAME optional, defaults to the email)
"""

from __future__ import annotations

import getpass
import os
import sys
import uuid
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.core.db.session import session_scope  # noqa: E402
from app.core.security.passwords import hash_password  # noqa: E402
from app.domain.identity.models import Role, User, UserRole, Workspace  # noqa: E402

DEFAULT_WORKSPACE_NAME = "default"


def main() -> None:
    with session_scope() as db:
        workspace = db.query(Workspace).filter(Workspace.name == DEFAULT_WORKSPACE_NAME).first()
        if workspace is None:
            workspace = Workspace(id=uuid.uuid4(), name=DEFAULT_WORKSPACE_NAME)
            db.add(workspace)
            db.flush()
            print(f"Created workspace '{DEFAULT_WORKSPACE_NAME}' ({workspace.id})")
        else:
            print(f"Workspace '{DEFAULT_WORKSPACE_NAME}' already exists ({workspace.id})")

        admin_role = db.query(Role).filter(Role.name == "Admin").one_or_none()
        if admin_role is None:
            raise SystemExit(
                "Admin role not found — run 'alembic upgrade head' "
                "(migration 0002 seeds roles/permissions) before this script."
            )

        non_interactive_email = os.environ.get("BOOTSTRAP_ADMIN_EMAIL")

        if non_interactive_email:
            email = non_interactive_email.strip().lower()
            display_name = os.environ.get("BOOTSTRAP_ADMIN_DISPLAY_NAME", email).strip()
            password = os.environ.get("BOOTSTRAP_ADMIN_PASSWORD", "")
            confirm = password
        else:
            email = input("Admin email: ").strip().lower()
            display_name = input("Display name: ").strip() or email
            password = getpass.getpass("Admin password: ")
            confirm = getpass.getpass("Confirm password: ")

        existing = db.query(User).filter(User.email == email).one_or_none()
        if existing is not None:
            raise SystemExit(f"User '{email}' already exists — nothing to do.")

        if password != confirm:
            raise SystemExit("Passwords did not match.")
        if len(password) < 12:
            raise SystemExit("Use at least 12 characters for the admin password.")

        user = User(
            id=uuid.uuid4(),
            workspace_id=workspace.id,
            email=email,
            password_hash=hash_password(password),
            display_name=display_name,
            is_active=True,
        )
        db.add(user)
        db.flush()

        db.add(
            UserRole(user_id=user.id, role_id=admin_role.id, workspace_id=workspace.id)
        )

        print(f"Created Admin user '{email}' in workspace '{DEFAULT_WORKSPACE_NAME}'.")


if __name__ == "__main__":
    main()
