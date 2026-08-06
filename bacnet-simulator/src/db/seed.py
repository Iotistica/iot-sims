"""Default building seed boundary."""
from .database import Database


def seed_default(database: Database) -> None:
    database.seed_default()
