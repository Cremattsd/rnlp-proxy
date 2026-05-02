import os
import psycopg2
import psycopg2.extras
from datetime import datetime, timezone

DATABASE_URL = os.getenv('DATABASE_URL')


def get_db():
    conn = psycopg2.connect(DATABASE_URL)
    return conn


def init_db():
    with get_db() as conn:
        with conn.cursor() as cur:
            cur.execute('''
                CREATE TABLE IF NOT EXISTS serials (
                    serial      TEXT PRIMARY KEY,
                    company_id  TEXT NOT NULL,
                    email       TEXT,
                    plan        TEXT DEFAULT 'basic',
                    expires_at  TEXT,
                    active      INTEGER DEFAULT 1,
                    jwt         TEXT,
                    created_at  TEXT
                )
            ''')
        conn.commit()


def get_serial(serial: str):
    with get_db() as conn:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute('SELECT * FROM serials WHERE serial = %s', (serial,))
            row = cur.fetchone()
            return dict(row) if row else None


def register_serial(serial: str, company_id: str, email: str, plan: str, expires_at):
    now = datetime.now(timezone.utc).isoformat()
    with get_db() as conn:
        with conn.cursor() as cur:
            cur.execute('''
                INSERT INTO serials (serial, company_id, email, plan, expires_at, active, created_at)
                VALUES (%s, %s, %s, %s, %s, 1, %s)
                ON CONFLICT (serial) DO UPDATE SET
                    company_id = EXCLUDED.company_id,
                    email      = EXCLUDED.email,
                    plan       = EXCLUDED.plan,
                    expires_at = EXCLUDED.expires_at,
                    active     = 1
            ''', (serial, company_id, email or '', plan or 'basic', expires_at, now))
        conn.commit()


def revoke_serial(serial: str):
    with get_db() as conn:
        with conn.cursor() as cur:
            cur.execute('UPDATE serials SET active = 0 WHERE serial = %s', (serial,))
        conn.commit()


def get_all_serials():
    with get_db() as conn:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute('SELECT * FROM serials ORDER BY created_at DESC')
            return [dict(r) for r in cur.fetchall()]
