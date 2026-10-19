from __future__ import annotations

"""Low-risk Postgres efficiency patch for Betting Lab.

The legacy Database helper opens and closes a physical psycopg2 connection for
nearly every execute/fetch call. During data backfills that produces thousands
of short-lived TCP/DNS handshakes. This patch keeps the public Database API
unchanged while borrowing connections from a small thread-safe pool.

SQLite behaviour is untouched. Strategy, settlement and betting logic are
untouched.
"""

from contextlib import contextmanager
import os
import threading
from typing import Any, Dict

from db import Database

VERSION = "BETTING_DB_POOL_V1"

_ORIGINAL_INIT = Database.__init__
_ORIGINAL_CONNECTION = Database.connection
_ORIGINAL_EXECUTEMANY = Database.executemany


def _pool_min() -> int:
    return max(1, int(os.getenv("BETTING_DB_POOL_MIN", "1")))


def _pool_max() -> int:
    return max(_pool_min(), int(os.getenv("BETTING_DB_POOL_MAX", "8")))


def _pool_wait_seconds() -> float:
    return max(1.0, float(os.getenv("BETTING_DB_POOL_WAIT_SECONDS", "30")))


def _patched_init(self, *args, **kwargs):
    _ORIGINAL_INIT(self, *args, **kwargs)
    self._betting_pg_pool = None
    self._betting_pg_pool_lock = threading.Lock()
    self._betting_pg_pool_slots = None


def _ensure_pool(self):
    if not self.is_postgres:
        return None
    pool = getattr(self, "_betting_pg_pool", None)
    if pool is not None:
        return pool

    lock = getattr(self, "_betting_pg_pool_lock", None)
    if lock is None:
        lock = threading.Lock()
        self._betting_pg_pool_lock = lock

    with lock:
        pool = getattr(self, "_betting_pg_pool", None)
        if pool is None:
            from psycopg2.pool import ThreadedConnectionPool

            minimum = _pool_min()
            maximum = _pool_max()
            pool = ThreadedConnectionPool(
                minimum,
                maximum,
                self.database_url,
                connect_timeout=10,
                application_name="betting_lab",
            )
            self._betting_pg_pool = pool
            self._betting_pg_pool_slots = threading.BoundedSemaphore(maximum)
    return pool


@contextmanager
def _pooled_connection(self):
    if not self.is_postgres:
        with _ORIGINAL_CONNECTION(self) as conn:
            yield conn
        return

    pool = _ensure_pool(self)
    slots = self._betting_pg_pool_slots
    if slots is None or not slots.acquire(timeout=_pool_wait_seconds()):
        raise TimeoutError("Timed out waiting for a Betting Lab Postgres pool slot")

    conn = None
    discard = False
    try:
        conn = pool.getconn()
        if getattr(conn, "closed", 0):
            try:
                pool.putconn(conn, close=True)
            except Exception:
                pass
            conn = pool.getconn()
        yield conn
        conn.commit()
    except Exception:
        if conn is not None:
            try:
                conn.rollback()
            except Exception:
                discard = True
            if getattr(conn, "closed", 0):
                discard = True
        raise
    finally:
        try:
            if conn is not None:
                if getattr(conn, "closed", 0):
                    discard = True
                try:
                    pool.putconn(conn, close=discard)
                except Exception:
                    try:
                        conn.close()
                    except Exception:
                        pass
        finally:
            slots.release()


def _batched_executemany(self, sql, rows):
    materialized = list(rows)
    if not materialized:
        return None
    if not self.is_postgres:
        return _ORIGINAL_EXECUTEMANY(self, sql, materialized)

    from psycopg2.extras import execute_batch

    page_size = max(1, int(os.getenv("BETTING_DB_EXECUTE_BATCH_SIZE", "500")))
    with self.connection() as conn:
        cur = conn.cursor()
        execute_batch(cur, self._sql(sql), materialized, page_size=page_size)
    return None


def install() -> Dict[str, Any]:
    if getattr(Database, "_betting_db_pool_patch_installed", False):
        return {
            "installed": True,
            "already_installed": True,
            "version": VERSION,
            "pool_min": _pool_min(),
            "pool_max": _pool_max(),
        }

    Database.__init__ = _patched_init
    Database.connection = _pooled_connection
    Database.executemany = _batched_executemany
    Database._betting_db_pool_patch_installed = True
    return {
        "installed": True,
        "already_installed": False,
        "version": VERSION,
        "pool_min": _pool_min(),
        "pool_max": _pool_max(),
        "execute_batch_size": max(1, int(os.getenv("BETTING_DB_EXECUTE_BATCH_SIZE", "500"))),
    }


INSTALL_RESULT = install()
print(f"BETTING_DB_POOL_PATCH {INSTALL_RESULT}", flush=True)
