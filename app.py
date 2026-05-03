"""
RealNex Listings Pro — Proxy Server
Validates plugin serials and proxies listing requests to the RealNex Search API.
"""

from flask import Flask, request, jsonify
from flask_cors import CORS
from dotenv import load_dotenv
from datetime import datetime, timezone
from urllib.parse import urlencode, quote
import requests
import os

from db import init_db, get_serial, register_serial, revoke_serial, get_all_serials
from auth import require_admin_key

load_dotenv()

app = Flask(__name__)
CORS(app, origins="*")

REALNEX_SEARCH_API = 'https://searchv2.realnex.com/api/v2/SearchListing1'
REALNEX_CRM_API    = 'https://sync.realnex.com'
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
        'startIndex':    '0',
        'NoOfRecords':   '1',
        'SortBy':        'updated',
        'SearchType':    '',
        'PropertyTypes': '',
        'AgentIDs':      'false',
        'CompanyIDs':    row['company_id'],
        'Id':            str(prop_id_int),
    }

    try:
        print(f'[/property] fetching id={prop_id_int} company={row["company_id"]}')
        resp = requests.post(
            REALNEX_SEARCH_API,
            data=payload,
            headers={'Content-Type': 'application/x-www-form-urlencoded'},
            timeout=30,
        )
        print(f'[/property] status={resp.status_code}')
        print(f'[/property] body={resp.text[:500]}')
        resp.raise_for_status()
        result = resp.json()
    except requests.Timeout:
        return jsonify({'error': 'RealNex API timeout'}), 504
    except requests.RequestException as exc:
        return jsonify({'error': str(exc)}), 502

    # SearchListing1 returns [listings_array, total, ...] — extract listings_array[0]
    listings = result[0] if isinstance(result, list) and result else []

    # Fallback: if Id filter returns 0 results, do a broader search and find by Id
    if not listings:
        print(f'[/property] id-filtered search returned 0 results, trying fallback')
        try:
            fb_payload = {
                'startIndex':    '0',
                'NoOfRecords':   '100',
                'SortBy':        'updated',
                'SearchType':    '',
                'PropertyTypes': '',
                'AgentIDs':      'false',
                'CompanyIDs':    row['company_id'],
            }
            fb_resp = requests.post(
                REALNEX_SEARCH_API,
                data=fb_payload,
                headers={'Content-Type': 'application/x-www-form-urlencoded'},
                timeout=30,
            )
            fb_resp.raise_for_status()
            fb_result = fb_resp.json()
            fb_all = fb_result[0] if isinstance(fb_result, list) and fb_result else []
            matched = [p for p in fb_all if str(p.get('Id', '')) == str(property_id)]
            listings = matched if matched else fb_all[:1]
            print(f'[/property] fallback returned {len(listings)} result(s)')
        except Exception as exc:
            print(f'[/property] fallback error: {exc}')

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
    jwt        = data.get('jwt', '')

    if not serial or not company_id:
        return jsonify({'error': 'serial and company_id are required'}), 400

    try:
        register_serial(serial, company_id, email, plan, expires_at, jwt)
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


@app.route('/lead', methods=['POST'])
def lead():
    """
    POST /lead
    Body: { "serial", "name", "email", "phone", "message", "property_id", "property_name" }

    Full CRM pipeline using the JWT stored against the serial:
      1. Find or create contact by email
      2. Find Projects linked to property (by listingId, fallback by notes)
      3. Add contact as lead to each project
      4. Create history entry
      5. Link history to contact
      6. Link history to each project
    """
    data          = request.get_json(silent=True) or {}
    serial        = data.get('serial', '').strip()
    name          = data.get('name', '').strip()
    email         = data.get('email', '').strip()
    phone         = data.get('phone', '').strip()
    message       = data.get('message', '').strip()
    property_id   = data.get('property_id', '').strip()
    property_name = data.get('property_name', '').strip()

    if not serial:
        return jsonify({'error': 'serial required'}), 400
    if not name or not email:
        return jsonify({'error': 'name and email required'}), 400

    row = get_serial(serial)
    if not row or not row['active']:
        return jsonify({'error': 'Invalid or expired serial'}), 403
    if _is_expired(row['expires_at']):
        return jsonify({'error': 'Serial expired'}), 403

    jwt_token = (row.get('jwt') or '').strip()
    if not jwt_token:
        return jsonify({'error': 'CRM not configured for this serial (no jwt)'}), 500

    parts      = name.split(' ', 1)
    first_name = parts[0]
    last_name  = parts[1] if len(parts) > 1 else ''

    crm = REALNEX_CRM_API
    hdrs = {
        'Content-Type':        'application/json',
        'Authorization':       f'Bearer {jwt_token}',
        'Crm-ApplicationName': 'RealNexListingsPro',
    }

    # ── STEP 1: Find or create contact ────────────────────────────────────
    contact_key = None
    try:
        sr = requests.get(
            f'{crm}/api/v1/CrmOData/Contacts',
            params={'$filter': f"email eq '{email}'", '$top': '1'},
            headers=hdrs, timeout=15,
        )
        if sr.status_code == 200:
            contacts = sr.json().get('value', [])
            if contacts:
                contact_key = contacts[0].get('key') or contacts[0].get('Key')
    except Exception:
        pass

    if not contact_key:
        try:
            cr = requests.post(
                f'{crm}/api/v1/Crm/contact',
                json={'firstName': first_name, 'lastName': last_name,
                      'email': email, 'mobile': phone, 'prospect': True},
                headers=hdrs, timeout=15,
            )
            if cr.status_code in (200, 201, 202):
                contact_key = cr.json().get('key')
        except Exception as exc:
            return jsonify({'error': f'Contact creation failed: {exc}'}), 502

    if not contact_key:
        return jsonify({'error': 'Could not find or create CRM contact'}), 502

    # ── STEP 2: Find projects linked to property ───────────────────────────
    projects_linked = []
    if property_id:
        try:
            pp = requests.get(
                f'{crm}/api/v1/CrmOData/Properties',
                params={'$filter': f'listingId eq {property_id}', '$expand': 'projects'},
                headers=hdrs, timeout=15,
            )
            if pp.status_code == 200:
                for prop in pp.json().get('value', []):
                    for proj in prop.get('projects', prop.get('Projects', [])):
                        pk = proj.get('key') or proj.get('Key')
                        if pk and pk not in projects_linked:
                            projects_linked.append(pk)
        except Exception:
            pass

        if not projects_linked:
            try:
                fb = requests.get(
                    f'{crm}/api/v1/CrmOData/Projects',
                    params={'$filter': f"contains(notes,'{property_id}')"},
                    headers=hdrs, timeout=15,
                )
                if fb.status_code == 200:
                    for proj in fb.json().get('value', []):
                        pk = proj.get('key') or proj.get('Key')
                        if pk and pk not in projects_linked:
                            projects_linked.append(pk)
            except Exception:
                pass

    # ── STEP 3: Add contact as lead to each project ────────────────────────
    linked_count = 0
    for proj_key in projects_linked:
        try:
            lr = requests.post(
                f'{crm}/api/v1/Crm/project/{quote(proj_key, safe="")}/lead',
                json={'contactKey': contact_key, 'notes': 'Web lead from property inquiry'},
                headers=hdrs, timeout=15,
            )
            if lr.status_code in (200, 201, 202):
                linked_count += 1
        except Exception:
            pass

    # ── STEP 4: Create history entry ───────────────────────────────────────
    history_key = None
    now_iso = datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%S')
    notes_text = 'Lead submitted via website inquiry form.\n\n'
    if property_name:
        notes_text += f'Property: {property_name}\n'
    if property_id:
        notes_text += f'Property ID: {property_id}\n'
    notes_text += (f'Auto-linked to {linked_count} project(s).\n' if linked_count
                   else 'No projects found — contact added without project link.\n')
    if message:
        notes_text += f'\nMessage from contact:\n{message}'

    try:
        hr = requests.post(
            f'{crm}/api/v1/Crm/history',
            json={
                'subject':      f'Web Lead \u2014 {property_name or "Property Inquiry"}',
                'notes':        notes_text,
                'startDate':    now_iso,
                'endDate':      now_iso,
                'timeless':     False,
                'eventTypeKey': 1,
                'published':    True,
            },
            headers=hdrs, timeout=15,
        )
        if hr.status_code in (200, 201, 202):
            history_key = hr.json().get('key')
    except Exception:
        pass

    # ── STEP 5 & 6: Link history to contact + each project ────────────────
    if history_key:
        hk_enc = quote(history_key, safe='')
        for obj_key, obj_type in [(contact_key, 'contact')] + [(pk, 'project') for pk in projects_linked]:
            try:
                requests.post(
                    f'{crm}/api/v1/Crm/history/{hk_enc}/object',
                    json={'objectKey': obj_key, 'objectType': obj_type},
                    headers=hdrs, timeout=10,
                )
            except Exception:
                pass

    return jsonify({
        'success':      True,
        'contact_key':  contact_key,
        'projects':     projects_linked,
        'linked_count': linked_count,
    })


if __name__ == '__main__':
    app.run(debug=False)
