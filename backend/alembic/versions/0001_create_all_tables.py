"""create_all_tables

Revision ID: 0001
Revises: 
Create Date: 2026-09-25

Creates all 12 CivicFlow tables with their columns, primary keys, and
foreign keys exactly as specified.  Down migration drops every table and
the applicationstatus enum in the correct reverse order.
"""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

# revision identifiers
revision = "0001"
down_revision = None
branch_labels = None
depends_on = None


# ── ApplicationStatus enum ────────────────────────────────────────────────────
application_status = postgresql.ENUM(
    "SUBMITTED",
    "DOCUMENT_PENDING",
    "VALIDATING",
    "VALIDATED",
    "ADDITIONAL_DOCUMENT_REQUIRED",
    "DOCUMENT_UPLOADED",
    "ASSIGNED",
    "UNDER_REVIEW",
    "APPROVED",
    "REJECTED",
    name="applicationstatus",
    create_type=False,   # we create it manually below so downgrade can drop it
)


def upgrade() -> None:
    # Create the Postgres enum type first
    application_status.create(op.get_bind(), checkfirst=True)

    # 1. roles
    op.create_table(
        "roles",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("name", sa.String(), nullable=False, unique=True),
    )

    # 2. departments
    op.create_table(
        "departments",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("name", sa.String(), nullable=False),
        sa.Column("service_type", sa.String(), nullable=False),
    )

    # 3. users  (depends on roles)
    op.create_table(
        "users",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("email", sa.String(), nullable=False, unique=True),
        sa.Column("password_hash", sa.String(), nullable=False),
        sa.Column("role_id", sa.Integer(), sa.ForeignKey("roles.id"), nullable=False),
        sa.Column("full_name", sa.String(), nullable=False),
        sa.Column("phone", sa.String(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
    )

    # 4. officers  (depends on users, departments)
    op.create_table(
        "officers",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("user_id", sa.Integer(), sa.ForeignKey("users.id"), nullable=False),
        sa.Column(
            "department_id",
            sa.Integer(),
            sa.ForeignKey("departments.id"),
            nullable=False,
        ),
        sa.Column("designation", sa.String(), nullable=False),
    )

    # 5. applications  (depends on users, departments)
    op.create_table(
        "applications",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column(
            "citizen_id", sa.Integer(), sa.ForeignKey("users.id"), nullable=False
        ),
        sa.Column(
            "department_id",
            sa.Integer(),
            sa.ForeignKey("departments.id"),
            nullable=False,
        ),
        sa.Column("service_type", sa.String(), nullable=False),
        sa.Column(
            "status",
            sa.Enum(
                "SUBMITTED",
                "DOCUMENT_PENDING",
                "VALIDATING",
                "VALIDATED",
                "ADDITIONAL_DOCUMENT_REQUIRED",
                "DOCUMENT_UPLOADED",
                "ASSIGNED",
                "UNDER_REVIEW",
                "APPROVED",
                "REJECTED",
                name="applicationstatus",
                create_type=False,
            ),
            nullable=False,
            server_default="SUBMITTED",
        ),
        sa.Column("priority", sa.Integer(), nullable=False, server_default="0"),
        sa.Column(
            "is_duplicate_flag",
            sa.Boolean(),
            nullable=False,
            server_default=sa.false(),
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
    )

    # 6. documents  (depends on applications)
    op.create_table(
        "documents",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column(
            "application_id",
            sa.Integer(),
            sa.ForeignKey("applications.id"),
            nullable=False,
        ),
        sa.Column("doc_type", sa.String(), nullable=False),
        sa.Column("storage_ref", sa.String(), nullable=False),
        sa.Column("original_filename", sa.String(), nullable=False),
        sa.Column(
            "validation_status",
            sa.String(),
            nullable=False,
            server_default="pending",
        ),
        sa.Column(
            "uploaded_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
    )

    # 7. assignments  (depends on applications, officers)
    op.create_table(
        "assignments",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column(
            "application_id",
            sa.Integer(),
            sa.ForeignKey("applications.id"),
            nullable=False,
        ),
        sa.Column(
            "officer_id",
            sa.Integer(),
            sa.ForeignKey("officers.id"),
            nullable=False,
        ),
        sa.Column(
            "assigned_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.Column("unassigned_at", sa.DateTime(timezone=True), nullable=True),
    )

    # 8. application_status_history  (depends on applications, users)
    op.create_table(
        "application_status_history",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column(
            "application_id",
            sa.Integer(),
            sa.ForeignKey("applications.id"),
            nullable=False,
        ),
        sa.Column("from_status", sa.String(), nullable=True),
        sa.Column("to_status", sa.String(), nullable=False),
        sa.Column(
            "changed_by", sa.Integer(), sa.ForeignKey("users.id"), nullable=False
        ),
        sa.Column(
            "changed_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.Column("remarks", sa.String(), nullable=True),
    )

    # 9. notifications  (depends on applications, users)
    op.create_table(
        "notifications",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column(
            "application_id",
            sa.Integer(),
            sa.ForeignKey("applications.id"),
            nullable=False,
        ),
        sa.Column(
            "recipient_id", sa.Integer(), sa.ForeignKey("users.id"), nullable=False
        ),
        sa.Column("channel", sa.String(), nullable=False),
        sa.Column("message", sa.String(), nullable=False),
        sa.Column(
            "status", sa.String(), nullable=False, server_default="pending"
        ),
        sa.Column("sent_at", sa.DateTime(timezone=True), nullable=True),
    )

    # 10. escalations  (depends on applications, users)
    op.create_table(
        "escalations",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column(
            "application_id",
            sa.Integer(),
            sa.ForeignKey("applications.id"),
            nullable=False,
        ),
        sa.Column(
            "escalated_to", sa.Integer(), sa.ForeignKey("users.id"), nullable=False
        ),
        sa.Column("reason", sa.String(), nullable=False),
        sa.Column("escalated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("resolved_at", sa.DateTime(timezone=True), nullable=True),
    )

    # 11. ai_analysis  (depends on documents)
    op.create_table(
        "ai_analysis",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column(
            "document_id",
            sa.Integer(),
            sa.ForeignKey("documents.id"),
            nullable=False,
        ),
        sa.Column("extracted_text", sa.Text(), nullable=True),
        sa.Column("extracted_fields", postgresql.JSONB(), nullable=True),
        sa.Column("validation_result", postgresql.JSONB(), nullable=True),
        sa.Column("processed_at", sa.DateTime(timezone=True), nullable=False),
    )

    # 12. audit_logs  (depends on users)
    op.create_table(
        "audit_logs",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column(
            "user_id", sa.Integer(), sa.ForeignKey("users.id"), nullable=False
        ),
        sa.Column("action", sa.String(), nullable=False),
        sa.Column("entity_type", sa.String(), nullable=False),
        sa.Column("entity_id", sa.Integer(), nullable=False),
        sa.Column("details", postgresql.JSONB(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
    )


def downgrade() -> None:
    # Drop in reverse dependency order
    op.drop_table("audit_logs")
    op.drop_table("ai_analysis")
    op.drop_table("escalations")
    op.drop_table("notifications")
    op.drop_table("application_status_history")
    op.drop_table("assignments")
    op.drop_table("documents")
    op.drop_table("applications")
    op.drop_table("officers")
    op.drop_table("users")
    op.drop_table("departments")
    op.drop_table("roles")

    # Drop the custom Postgres enum type
    application_status.drop(op.get_bind(), checkfirst=True)
