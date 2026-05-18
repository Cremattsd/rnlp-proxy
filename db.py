import os
import hashlib
import psycopg2
import psycopg2.extras
from datetime import datetime, timezone, timedelta

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
                CREATE TABLE IF NOT EXISTS flagged_ips (
                    id         SERIAL PRIMARY KEY,
                    ip         TEXT,
                    email      TEXT,
                    serial     TEXT,
                    reason     TEXT,
                    created_at TEXT
                )
            ''')
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
            cur.execute('''
                CREATE TABLE IF NOT EXISTS crm_key_cache (
                    cache_key   TEXT PRIMARY KEY,
                    cache_value TEXT NOT NULL,
                    cache_type  TEXT NOT NULL,
                    expires_at  TEXT NOT NULL,
                    created_at  TEXT NOT NULL
                )
            ''')
            cur.execute('''
                CREATE TABLE IF NOT EXISTS lead_submissions (
                    id               SERIAL PRIMARY KEY,
                    dedup_signature  TEXT NOT NULL,
                    email            TEXT,
                    property_id      TEXT,
                    serial           TEXT,
                    crm_contact_key  TEXT DEFAULT '',
                    crm_project_key  TEXT DEFAULT '',
                    crm_history_key  TEXT DEFAULT '',
                    status           TEXT DEFAULT 'pending',
                    created_at       TEXT NOT NULL,
                    completed_at     TEXT DEFAULT ''
                )
            ''')
            cur.execute('CREATE INDEX IF NOT EXISTS idx_cache_type_expires ON crm_key_cache (cache_type, expires_at)')
            cur.execute('CREATE INDEX IF NOT EXISTS idx_dedup_sig_created ON lead_submissions (dedup_signature, created_at)')
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


def check_lead_rate(email: str, ip: str):
    from datetime import timedelta
    now = datetime.now(timezone.utc)
    cutoff_24h = (now - timedelta(hours=24)).isoformat()
    cutoff_1h  = (now - timedelta(hours=1)).isoformat()

    with get_db() as conn:
        with conn.cursor() as cur:
            cur.execute(
                'SELECT COUNT(*) FROM flagged_ips WHERE email = %s AND created_at > %s',
                (email, cutoff_24h),
            )
            if cur.fetchone()[0] >= 5:
                return 'Too many submissions for this email. Try again later.'

            cur.execute(
                'SELECT COUNT(*) FROM flagged_ips WHERE ip = %s AND created_at > %s',
                (ip, cutoff_1h),
            )
            if cur.fetchone()[0] >= 10:
                return 'Too many submissions from this address. Try again later.'

    return None


def log_lead_attempt(ip: str, email: str, serial: str):
    now = datetime.now(timezone.utc).isoformat()
    with get_db() as conn:
        with conn.cursor() as cur:
            cur.execute(
                'INSERT INTO flagged_ips (ip, email, serial, reason, created_at) VALUES (%s, %s, %s, %s, %s)',
                (ip, email, serial, 'lead_attempt', now),
            )
        conn.commit()


# ── CRM key cache ────────────────────────────────────────────────────────

def cache_get(key: str, key_type: str):
    now = datetime.now(timezone.utc).isoformat()
    try:
        with get_db() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    'SELECT cache_value FROM crm_key_cache WHERE cache_key = %s AND cache_type = %s AND expires_at > %s',
                    (key, key_type, now),
                )
                row = cur.fetchone()
                return row[0] if row else None
    except Exception:
        return None


def cache_set(key: str, value: str, key_type: str, ttl_seconds: int = 3600):
    now = datetime.now(timezone.utc)
    expires = (now + timedelta(seconds=ttl_seconds)).isoformat()
    try:
        with get_db() as conn:
            with conn.cursor() as cur:
                cur.execute('''
                    INSERT INTO crm_key_cache (cache_key, cache_value, cache_type, expires_at, created_at)
                    VALUES (%s, %s, %s, %s, %s)
                    ON CONFLICT (cache_key) DO UPDATE SET
                        cache_value = EXCLUDED.cache_value,
                        expires_at  = EXCLUDED.expires_at
                ''', (key, value, key_type, expires, now.isoformat()))
            conn.commit()
    except Exception:
        pass


# ── Lead dedup ───────────────────────────────────────────────────────────

def make_dedup_signature(email: str, property_id: str, serial: str) -> str:
    raw = f'{email.lower().strip()}|{property_id.strip()}|{serial.strip()}'
    return hashlib.sha256(raw.encode()).hexdigest()[:16]


def check_dedup(signature: str, window_seconds: int = 60):
    cutoff = (datetime.now(timezone.utc) - timedelta(seconds=window_seconds)).isoformat()
    try:
        with get_db() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    'SELECT id FROM lead_submissions WHERE dedup_signature = %s AND created_at > %s LIMIT 1',
                    (signature, cutoff),
                )
                return cur.fetchone() is not None
    except Exception:
        return False


def log_submission(signature: str, email: str, property_id: str, serial: str):
    now = datetime.now(timezone.utc).isoformat()
    try:
        with get_db() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    'INSERT INTO lead_submissions (dedup_signature, email, property_id, serial, created_at) VALUES (%s, %s, %s, %s, %s)',
                    (signature, email, property_id, serial, now),
                )
            conn.commit()
    except Exception:
        pass
