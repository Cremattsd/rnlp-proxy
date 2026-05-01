import os
import sqlite3
from datetime import datetime, timezone

DATABASE = os.getenv('DATABASE_URL', 'serials.db')


def get_db():
    conn = sqlite3.connect(DATABASE)
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
    with get_db() as conn:
        conn.execute('''
            CREATE TABLE IF NOT EXISTS serials (
                serial      TEXT PRIMARY KEY,
                company_id  TEXT NOT NULL,
                email       TEXT,
                plan        TEXT DEFAULT 'basic',
                expires_at  TEXT,
                active      INTEGER DEFAULT 1,
                created_at  TEXT
            )
        ''')
        conn.commit()


def get_serial(serial: str):
    with get_db() as conn:
        row = conn.execute(
            'SELECT * FROM serials WHERE serial = ?', (serial,)
        ).fetchone()
        return dict(row) if row else None


def register_serial(serial: str, company_id: str, email: str, plan: str, expires_at):
    now = datetime.now(timezone.utc).isoformat()
    with get_db() as conn:
        conn.execute('''
            INSERT INTO serials (serial, company_id, email, plan, expires_at, active, created_at)
            VALUES (?, ?, ?, ?, ?, 1, ?)
            ON CONFLICT(serial) DO UPDATE SET
                company_id = excluded.company_id,
                email      = excluded.email,
                plan       = excluded.plan,
                expires_at = excluded.expires_at,
                active     = 1
        ''', (serial, company_id, email or '', plan or 'basic', expires_at, now))
        conn.commit()


def revoke_serial(serial: str):
    with get_db() as conn:
        conn.execute('UPDATE serials SET active = 0 WHERE serial = ?', (serial,))
        conn.commit()


def get_all_serials():
    with get_db() as conn:
        rows = conn.execute(
            'SELECT * FROM serials ORDER BY created_at DESC'
        ).fetchall()
        return [dict(r) for r in rows]
