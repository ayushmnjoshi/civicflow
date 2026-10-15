"""Seed script — inserts roles, departments, system user, and officer1.

Usage (from backend/ directory):
    python -m database.seed
  or:
    python database/seed.py
"""

import os
import secrets
import sys
from pathlib import Path

# Allow running from backend/ or from civicflow/
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from dotenv import load_dotenv

load_dotenv(dotenv_path=Path(__file__).resolve().parents[3] / ".env", override=False)
# Fallback path
if not os.environ.get("DATABASE_URL"):
    load_dotenv(dotenv_path=Path(__file__).resolve().parents[2] / ".env", override=False)

from sqlalchemy import create_engine, text
from sqlalchemy.orm import sessionmaker

DATABASE_URL = os.environ["DATABASE_URL"]

engine = create_engine(DATABASE_URL)
Session = sessionmaker(bind=engine)


def seed() -> None:
    with Session() as session:
        # ── roles ──────────────────────────────────────────────────────────
        existing_roles = {
            row[0]
            for row in session.execute(text("SELECT name FROM roles")).fetchall()
        }
        roles_to_insert = [
            {"name": "citizen"},
            {"name": "officer"},
            {"name": "dept_head"},
            {"name": "admin"},
            {"name": "system"},
        ]
        new_roles = [r for r in roles_to_insert if r["name"] not in existing_roles]
        if new_roles:
            session.execute(
                text("INSERT INTO roles (name) VALUES (:name)"),
                new_roles,
            )
            print(f"Inserted {len(new_roles)} role(s): {[r['name'] for r in new_roles]}")
        else:
            print("Roles already seeded — skipping.")

        # ── departments ────────────────────────────────────────────────────
        existing_depts = {
            row[0]
            for row in session.execute(text("SELECT name FROM departments")).fetchall()
        }
        departments_to_insert = [
            {"name": "Municipal Corporation", "service_type": "civic_services"},
            {"name": "Revenue Department", "service_type": "land_records"},
        ]
        new_depts = [d for d in departments_to_insert if d["name"] not in existing_depts]
        if new_depts:
            session.execute(
                text(
                    "INSERT INTO departments (name, service_type) VALUES (:name, :service_type)"
                ),
                new_depts,
            )
            print(
                f"Inserted {len(new_depts)} department(s): {[d['name'] for d in new_depts]}"
            )
        else:
            print("Departments already seeded — skipping.")

        # ── system user ────────────────────────────────────────────────────
        # This account has an unusable password — the random secret is generated
        # and discarded here; it is never logged or stored anywhere.
        existing_system = session.execute(
            text("SELECT id FROM users WHERE email = 'system@civicflow.internal'")
        ).fetchone()

        if existing_system is None:
            import bcrypt  # only needed during seeding

            system_role_id = session.execute(
                text("SELECT id FROM roles WHERE name = 'system'")
            ).scalar()

            # Generate and immediately discard a random secret — nobody can log in
            _throwaway = secrets.token_bytes(64)
            unusable_hash = bcrypt.hashpw(_throwaway, bcrypt.gensalt(rounds=12)).decode()
            del _throwaway  # no longer needed

            session.execute(
                text(
                    "INSERT INTO users (email, password_hash, role_id, full_name)"
                    " VALUES (:email, :password_hash, :role_id, :full_name)"
                ),
                {
                    "email": "system@civicflow.internal",
                    "password_hash": unusable_hash,
                    "role_id": system_role_id,
                    "full_name": "CivicFlow System",
                },
            )
            print("Inserted system user: system@civicflow.internal")
        else:
            print("System user already seeded — skipping.")

        # ── officer1 ───────────────────────────────────────────────────────
        # Ensures the seeded officer1 user has a row in the officers table.
        # Uses the first seeded department (Municipal Corporation).
        # Idempotent: checks by email before inserting.
        existing_officer1 = session.execute(
            text("SELECT id FROM users WHERE email = 'officer1@civicflow.dev'")
        ).fetchone()

        if existing_officer1 is not None:
            officer1_user_id = existing_officer1[0]
            # Check if officers row already exists for this user
            existing_officer_row = session.execute(
                text("SELECT id FROM officers WHERE user_id = :uid"),
                {"uid": officer1_user_id},
            ).fetchone()
            if existing_officer_row is None:
                # Insert the officers row
                first_dept_id = session.execute(
                    text("SELECT id FROM departments WHERE name = 'Municipal Corporation'")
                ).scalar()
                if first_dept_id is not None:
                    session.execute(
                        text(
                            "INSERT INTO officers (user_id, department_id, designation)"
                            " VALUES (:uid, :dept_id, :designation)"
                        ),
                        {
                            "uid": officer1_user_id,
                            "dept_id": first_dept_id,
                            "designation": "Officer",
                        },
                    )
                    print(f"Inserted officers row for officer1 (user_id={officer1_user_id}).")
                else:
                    print("Municipal Corporation department not found — cannot insert officer row.")
            else:
                print("officer1 officers row already exists — skipping.")
        else:
            print("officer1 user not found — run create_staff_user.py first.")

        session.commit()
        print("Seed complete.")


if __name__ == "__main__":
    seed()
