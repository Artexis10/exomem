"""Fence readers that conflate queued renders with physical exposure."""

revision = "13"
down_revision = "12"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Only installation metadata changes; schema 12 writers cannot preserve its historical authority.
    pass
