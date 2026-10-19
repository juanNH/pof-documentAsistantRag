"""Add PDF citations and a human review step for extracted invoice fields."""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision = "0002_pdf_review"
down_revision = "0001_initial"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.drop_constraint("ck_documents_status", "documents", type_="check")
    op.create_check_constraint(
        "ck_documents_status",
        "documents",
        "status IN ('queued', 'processing', 'needs_review', 'ready', 'failed')",
    )
    op.add_column("documents", sa.Column("approved_fields", postgresql.JSONB(), nullable=True))
    op.add_column("documents", sa.Column("reviewed_at", sa.DateTime(timezone=True), nullable=True))
    op.add_column("document_chunks", sa.Column("page_start", sa.Integer(), nullable=True))
    op.add_column("document_chunks", sa.Column("page_end", sa.Integer(), nullable=True))
    op.create_index("ix_document_chunks_page", "document_chunks", ["document_id", "page_start", "ordinal"])


def downgrade() -> None:
    op.drop_index("ix_document_chunks_page", table_name="document_chunks")
    op.drop_column("document_chunks", "page_end")
    op.drop_column("document_chunks", "page_start")
    op.drop_column("documents", "reviewed_at")
    op.drop_column("documents", "approved_fields")
    op.drop_constraint("ck_documents_status", "documents", type_="check")
    op.create_check_constraint(
        "ck_documents_status",
        "documents",
        "status IN ('queued', 'processing', 'ready', 'failed')",
    )
