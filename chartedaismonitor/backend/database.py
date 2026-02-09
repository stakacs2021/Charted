"""
Database connection and helpers. Uses DATABASE_URL from environment.
"""
import os
import psycopg2
from psycopg2.extras import RealDictCursor
from contextlib import contextmanager

DATABASE_URL = os.getenv(
    "DATABASE_URL",
    "postgresql://ais_user:ais_pass@localhost:5432/ais",
)


@contextmanager
def get_cursor():
    conn = psycopg2.connect(DATABASE_URL)
    conn.autocommit = False
    try:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            yield cur
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


@contextmanager
def get_connection():
    conn = psycopg2.connect(DATABASE_URL)
    try:
        yield conn
    finally:
        conn.close()
