"""
RealNex Listings Pro — Proxy Server
Validates plugin serials and proxies listing requests to the RealNex Search API.
"""

from flask import Flask, request, jsonify
from flask_cors import CORS
from dotenv import load_dotenv
from datetime import datetime, timezone
from urllib.parse import urlencode
import requests
import os

from db import init_db, get_serial, register_serial, revoke_serial, get_all_serials
from auth import require_admin_key

load_dotenv()

app = Flask(__name__)
CORS(app, origins="*")

REALNEX_SEARCH_API = 'https://searchv2.realnex.com/api/v2/SearchListing1'
CENSUS_API         = 'https://api.census.gov/data/2022/acs/acs5'


# ── DB init ────────────────────────────────────────────────────────────────
with app.app_context():
    init_db()


# ── Helpers ────────────────────────────────────────────────────────────────

def _is_expired(expires_at: str) -> bool:
    """Return True if the expires_at ISO string is in the past."""
    if not expires_at:
        return False
    try:
        exp = datetime.fromisoformat(expires_at)
        if exp.tzinfo is None:
            exp = exp.replace(tzinfo=timezone.utc)
        return exp < datetime.now(timezone.utc)
    except Exception:
        return False


def _php_build_query(data, prefix='') -> list:
    """
    Replicate PHP's http_build_query for nested dicts/lists so the RealNex
    API receives the same encoding it expects (key[0]=v1&key[1]=v2 style).
    """
    pairs = []
    if isinstance(data, dict):
        for key, value in data.items():
            full_key = f'{prefix}[{key}]' if prefix else str(key)
            pairs.extend(_php_build_query(value, full_key))
    elif isinstance(data, list):
        for i, value in enumerate(data):
            full_key = f'{prefix}[{i}]'
            pairs.extend(_php_build_query(value, full_key))
    elif data is None or data is False:
        pairs.append((prefix, ''))
    else:
        pairs.append((prefix, str(data)))
    return pairs


def _encode_filters(filters: dict) -> str:
    """Convert a filter dict to a PHP http_build_query-compatible form string."""
    return urlencode(_php_build_query(filters))


# ── Routes ─────────────────────────────────────────────────────────────────

@app.route('/validate', methods=['POST'])
def validate():
    """
    POST /validate
    Body: { "serial": "XXXX-XXXX-XXXX" }
    Returns: { "valid": bool, "company_id": str, "plan": str, "expires_at": str }
    """
    data = request.get_json(silent=True) or {}
    serial = data.get('serial', '').strip()
    if not serial:
        return jsonify({'valid': False, 'error': 'serial required'}), 400

    row = get_serial(serial)
    if not row:
        return jsonify({'valid': False}), 200

    if not row['active'] or _is_expired(row['expires_at']):
        return jsonify({'valid': False}), 200

    return jsonify({
        'valid':      True,
        'company_id': row['company_id'],
        'plan':       row['plan'],
        'expires_at': row['expires_at'],
    })


@app.route('/listings', methods=['POST'])
def listings():
    """
    POST /listings
    Body: { "serial": "...", "filters": { ...RealNex filter params... } }
    Injects company_id from DB, proxies to RealNex SearchListing1.
    """
    data = request.get_json(silent=True) or {}
    serial = data.get('serial', '').strip()
    filters = dict(data.get('filters', {}))

    if not serial:
        return jsonify({'error': 'serial required'}), 400

    row = get_serial(serial)
    if not row or not row['active']:
        return jsonify({'error': 'Invalid or expired serial'}), 403

    if _is_expired(row['expires_at']):
        return jsonify({'error': 'Serial expired'}), 403

    # Inject company_id — never trust client-supplied CompanyIDs
    filters.pop('CompanyIDs', None)
    filters['CompanyIDs'] = [row['company_id']]

    try:
        body = _encode_filters(filters)
        resp = requests.post(
            REALNEX_SEARCH_API,
            data=body,
            headers={'Content-Type': 'application/x-www-form-urlencoded'},
            timeout=30,
        )
        resp.raise_for_status()
        return jsonify(resp.json()), resp.status_code
    except requests.Timeout:
        return jsonify({'error': 'RealNex API timeout'}), 504
    except requests.RequestException as exc:
        return jsonify({'error': str(exc)}), 502


def _fetch_demographics(zip_code: str) -> dict | None:
    """
    Fetch ACS 5-year census demographics for a ZIP code.
    Returns {population, median_income, median_home_value} or None on any failure.
    """
    if not zip_code:
        return None
    try:
        resp = requests.get(
            CENSUS_API,
            params={
                'get':  'B01003_001E,B19013_001E,B25077_001E',
                'for':  f'zip code tabulation area:{zip_code.strip()}',
                'in':   'state:*',
            },
            timeout=10,
        )
        resp.raise_for_status()
        rows = resp.json()          # [[header...], [values...]]
        if len(rows) < 2:
            return None
        headers = rows[0]
        values  = rows[1]
        row = dict(zip(headers, values))
        def _int(key):
            try:
                v = int(row.get(key, -1))
                return v if v >= 0 else None
            except (TypeError, ValueError):
                return None
        return {
            'population':        _int('B01003_001E'),
            'median_income':     _int('B19013_001E'),
            'median_home_value': _int('B25077_001E'),
        }
    except Exception:
        return None


@app.route('/property', methods=['POST'])
def property_detail():
    """
    POST /property
    Body: { "serial": "...", "property_id": "..." }

    Uses SearchListing1 with Id filter (NoOfRecords=1) to retrieve the full
    listing object, then appends census demographics keyed on Zip.

    Returns: { "property": {...}, "demographics": {...} | null }
    """
    data        = request.get_json(silent=True) or {}
    serial      = data.get('serial', '').strip()
    property_id = data.get('property_id', '').strip()

    if not serial:
        return jsonify({'error': 'serial required'}), 400
    if not property_id:
        return jsonify({'error': 'property_id required'}), 400

    row = get_serial(serial)
    if not row or not row['active']:
        return jsonify({'error': 'Invalid or expired serial'}), 403

    if _is_expired(row['expires_at']):
        return jsonify({'error': 'Serial expired'}), 403

    # ── Fetch listing via SearchListing1 with Id filter ───────────────────
    try:
        prop_id_int = int(property_id)
    except (ValueError, TypeError):
        prop_id_int = property_id

    payload = {
        'startIndex':    0,
        'NoOfRecords':   1,
        'SortBy':        'updated',
        'SearchType':    '',
        'PropertyTypes': '',
        'AgentIDs':      False,
        'CompanyIDs':    [row['company_id']],
        'Id':            prop_id_int,
    }

    try:
        resp = requests.post(
            REALNEX_SEARCH_API,
            json=payload,
            timeout=30,
        )
        resp.raise_for_status()
        result = resp.json()
    except requests.Timeout:
        return jsonify({'error': 'RealNex API timeout'}), 504
    except requests.RequestException as exc:
        return jsonify({'error': str(exc)}), 502

    # SearchListing1 returns [listings_array, total, ...] — extract listings_array[0]
    listings = result[0] if isinstance(result, list) and result else []
    if not listings:
        return jsonify({'error': 'Property not found'}), 404

    prop = listings[0]

    # ── Census demographics ───────────────────────────────────────────────
    zip_code     = prop.get('Zip', '')
    demographics = _fetch_demographics(zip_code)

    return jsonify({'property': prop, 'demographics': demographics})


@app.route('/register', methods=['POST'])
@require_admin_key
def register():
    """
    POST /register  (X-Admin-Key required)
    Body: { "serial", "company_id", "email", "plan", "expires_at" }
    """
    data = request.get_json(silent=True) or {}
    serial     = data.get('serial', '').strip()
    company_id = data.get('company_id', '').strip()
    email      = data.get('email', '')
    plan       = data.get('plan', 'basic')
    expires_at = data.get('expires_at')

    if not serial or not company_id:
        return jsonify({'error': 'serial and company_id are required'}), 400

    try:
        register_serial(serial, company_id, email, plan, expires_at)
        return jsonify({'success': True, 'serial': serial})
    except Exception as exc:
        return jsonify({'error': str(exc)}), 500


@app.route('/revoke', methods=['POST'])
@require_admin_key
def revoke():
    """
    POST /revoke  (X-Admin-Key required)
    Body: { "serial": "..." }
    """
    data = request.get_json(silent=True) or {}
    serial = data.get('serial', '').strip()
    if not serial:
        return jsonify({'error': 'serial required'}), 400
    revoke_serial(serial)
    return jsonify({'success': True})


@app.route('/serials', methods=['GET'])
@require_admin_key
def serials():
    """
    GET /serials  (X-Admin-Key required)
    Returns all serials with metadata.
    """
    return jsonify(get_all_serials())


if __name__ == '__main__':
    app.run(debug=False)
