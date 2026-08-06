"""Schema migration boundary.

During phase 1, schema setup remains inside ``Database.setup``. Move the SQL
from that method here when performing the physical extraction.
"""
from .database import Database


def setup_schema(database: Database) -> None:
    database.setup()
