"""Staff account creation script.

Usage (from the civicflow/ root or backend/ directory):

    python database/seed/create_staff_user.py \\
        --email officer1@civicflow.dev \\
        --full-name "Officer One" \\
        --role officer \\
        --department "Municipal Corporation"

    python database/seed/create_staff_user.py \\
        --email admin1@civicflow.dev \\
        --full-name "Admin One" \\
        --role admin

Password is read interactively via getpass — never as a CLI argument.

Constraints
-----------
- --role must be officer, dept_head, or admin.
- --department is required for officer and dept_head; ignored for admin.
- Duplicate email is rejected with a clear message.
- Unknown department name is rejected with a clear message.
- Database URL is loaded from civicflow/.env via the app's Settings class.
"""

from __future__ import annotations

import argparse
import getpass
import sys
from pathlib import Path

# Ensure the backend/ directory is on sys.path so `app.*` imports work when
# the script is called from civicflow/ or from backend/.
_BACKEND = Path(__file__).resolve().parents[2] / "backend"
if str(_BACKEND) not in sys.path:
    sys.path.insert(0, str(_BACKEND))

from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from app.auth.security import hash_password
from app.config import settings
from app.models.department import Department
from app.models.officer import Officer
from app.models.role import Role
from app.models.user import User

_STAFF_ROLES = {"officer", "dept_head", "admin"}
# "system" is intentionally excluded — it is seeded once by database/seed/seed.py


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Create a CivicFlow staff account.")
    parser.add_argument("--email", required=True, help="Staff user email address.")
    parser.add_argument("--full-name", required=True, dest="full_name", help="Full name.")
    parser.add_argument(
        "--role",
        required=True,
        choices=list(_STAFF_ROLES),
        help="Role: officer | dept_head | admin",
    )
    parser.add_argument(
        "--department",
        default=None,
        help="Department name (required for officer and dept_head).",
    )
    return parser.parse_args()


def main() -> None:
    args = _parse_args()

    # Validate department requirement
    needs_dept = args.role in {"officer", "dept_head"}
    if needs_dept and not args.department:
        print(
            f"Error: --department is required for role '{args.role}'.",
            file=sys.stderr,
        )
        sys.exit(1)

    # Read password securely
    password = getpass.getpass(prompt=f"Password for {args.email}: ")
    if len(password) < 8:
        print("Error: password must be at least 8 characters.", file=sys.stderr)
        sys.exit(1)

    engine = create_engine(settings.database_url)

    with Session(engine) as db:
        # Check duplicate email
        if db.query(User).filter(User.email == args.email.lower()).first():
            print(f"Error: email '{args.email}' is already registered.", file=sys.stderr)
            sys.exit(1)

        # Resolve role
        role_row = db.query(Role).filter(Role.name == args.role).first()
        if role_row is None:
            print(
                f"Error: role '{args.role}' not found in DB. Run the seed script first.",
                file=sys.stderr,
            )
            sys.exit(1)

        # Resolve department (if needed)
        dept_row: Department | None = None
        if needs_dept:
            dept_row = (
                db.query(Department).filter(Department.name == args.department).first()
            )
            if dept_row is None:
                print(
                    f"Error: department '{args.department}' not found. "
                    "Check the name or run the seed script.",
                    file=sys.stderr,
                )
                sys.exit(1)

        # Create user
        user = User(
            email=args.email.lower(),
            password_hash=hash_password(password),
            role_id=role_row.id,
            full_name=args.full_name,
        )
        db.add(user)
        db.flush()  # get user.id without committing yet

        # Create officer row if needed
        if needs_dept and dept_row is not None:
            officer = Officer(
                user_id=user.id,
                department_id=dept_row.id,
                designation=args.role,
            )
            db.add(officer)

        db.commit()
        db.refresh(user)

        print(f"[OK] Created {args.role} account: id={user.id}  email={user.email}")
        if needs_dept:
            print(f"     Department: {dept_row.name}")


if __name__ == "__main__":
    main()
