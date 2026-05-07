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
            cur.execute("ALTER TABLE serials ADD COLUMN IF NOT EXISTS domain TEXT DEFAULT ''")
            cur.execute("ALTER TABLE serials ADD COLUMN IF NOT EXISTS product_type TEXT DEFAULT ''")
            cur.execute('''
                CREATE TABLE IF NOT EXISTS reports (
                    id         SERIAL PRIMARY KEY,
                    serial     TEXT,
                    domain     TEXT,
                    event      TEXT,
                    payload    TEXT,
                    created_at TEXT
                )
            ''')
        conn.commit()


def get_serial(serial: str):
    with get_db() as conn:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute('SELECT * FROM serials WHERE serial = %s', (serial,))
            row = cur.fetchone()
            return dict(row) if row else None


def register_serial(serial: str, company_id: str, email: str, plan: str, expires_at, jwt: str = '', product_type: str = ''):
    now = datetime.now(timezone.utc).isoformat()
    with get_db() as conn:
        with conn.cursor() as cur:
            cur.execute('''
                INSERT INTO serials (serial, company_id, email, plan, expires_at, active, jwt, created_at, product_type)
                VALUES (%s, %s, %s, %s, %s, 1, %s, %s, %s)
                ON CONFLICT (serial) DO UPDATE SET
                    company_id = EXCLUDED.company_id,
                    email      = EXCLUDED.email,
                    plan       = EXCLUDED.plan,
                    expires_at = EXCLUDED.expires_at,
                    active     = 1,
                    jwt        = EXCLUDED.jwt,
                    product_type = EXCLUDED.product_type
            ''', (serial, company_id, email or '', plan or 'basic', expires_at, jwt or '', now, product_type or ''))
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


def log_report(serial: str, domain: str, event: str, payload: str = ''):
    now = datetime.now(timezone.utc).isoformat()
    with get_db() as conn:
        with conn.cursor() as cur:
            cur.execute(
                'INSERT INTO reports (serial, domain, event, payload, created_at) VALUES (%s, %s, %s, %s, %s)',
                (serial, domain, event, payload, now),
            )
        conn.commit()


def get_all_reports():
    with get_db() as conn:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute('SELECT * FROM reports ORDER BY created_at DESC LIMIT 500')
            return [dict(r) for r in cur.fetchall()]


def update_serial_domain(serial: str, domain: str):
    with get_db() as conn:
        with conn.cursor() as cur:
            cur.execute('UPDATE serials SET domain = %s WHERE serial = %s', (domain, serial))
        conn.commit()
