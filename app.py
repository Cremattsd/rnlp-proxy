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

from db import init_db, get_serial, register_serial, revoke_serial, get_all_serials, log_report, get_all_reports, update_serial_domain
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
    raw = row['company_id']
    filters['CompanyIDs'] = [c.strip() for c in raw.split(',')]

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
    Fetch ACS 5-year census demographics for a ZIP code via direct ZCTA query.
    Returns {population, median_income, median_home_value, unemployment} or None.
    """
    if not zip_code:
        return None
    try:
        zc = zip_code.strip()
        url = (f'{CENSUS_API}?get=B01003_001E,B19013_001E,B25077_001E,B23025_005E'
               f'&for=zip%20code%20tabulation%20area:{zc}')
        resp = requests.get(url, timeout=10)
        resp.raise_for_status()
        data = resp.json()
        if len(data) < 2:
            return None
        hdrs = data[0]
        vals = data[1]

        def _safe(key):
            try:
                v = vals[hdrs.index(key)]
                return int(v) if v and v != '-666666666' else None
            except (ValueError, IndexError):
                return None

        print(f'[census] zip={zc} raw={vals}')
        return {
            'population':        _safe('B01003_001E'),
            'median_income':     _safe('B19013_001E'),
            'median_home_value': _safe('B25077_001E'),
            'unemployment':      _safe('B23025_005E'),
        }
    except Exception as e:
        print(f'Census error for zip {zip_code}: {e}')
        return None


def _fetch_walk_score(lat: float, lon: float, address: str) -> dict | None:
    """Fetch Walk Score (walk/transit/bike) if WALKSCORE_API_KEY is set."""
    key = os.getenv('WALKSCORE_API_KEY', '')
    if not key or not lat or not lon:
        return None
    try:
        resp = requests.get(
            'https://api.walkscore.com/score',
            params={
                'format':   'json',
                'address':  address,
                'lat':      lat,
                'lon':      lon,
                'transit':  1,
                'bike':     1,
                'wsapikey': key,
            },
            timeout=8,
        )
        if resp.status_code == 200:
            d = resp.json()
            return {
                'walk':         d.get('walkscore'),
                'walk_desc':    d.get('description'),
                'transit':      d['transit']['score']       if d.get('transit') else None,
                'transit_desc': d['transit']['description'] if d.get('transit') else None,
                'bike':         d['bike']['score']          if d.get('bike') else None,
                'bike_desc':    d['bike']['description']    if d.get('bike') else None,
            }
    except Exception as e:
        print(f'Walk Score error: {e}')
    return None


def _fetch_neighborhood(lat: float, lon: float) -> dict | None:
    """Reverse-geocode lat/lon via Nominatim to get neighborhood context."""
    if not lat or not lon:
        return None
    try:
        resp = requests.get(
            'https://nominatim.openstreetmap.org/reverse',
            params={'lat': lat, 'lon': lon, 'format': 'json', 'addressdetails': '1'},
            headers={'User-Agent': 'RealNexListingsPro/1.0'},
            timeout=8,
        )
        if resp.status_code == 200:
            d    = resp.json()
            addr = d.get('address', {})
            return {
                'neighborhood': (addr.get('neighbourhood') or addr.get('suburb')
                                 or addr.get('quarter')),
                'county':  addr.get('county'),
                'city':    addr.get('city') or addr.get('town'),
                'state':   addr.get('state'),
                'display': d.get('display_name', '')[:120],
            }
    except Exception as e:
        print(f'Nominatim error: {e}')
    return None


@app.route('/property', methods=['POST'])
def property_detail():
    """
    POST /property
    Body: { "serial": "...", "property_id": <int or str> }
    Returns: { "property": {...}, "demographics": {...}|null, "neighborhood": {...}|null, "walk_score": null }
    """
    try:
        data = request.get_json(silent=True) or {}
        serial      = data.get('serial', '')
        property_id = data.get('property_id')

        print(f'PROPERTY REQUEST: serial={serial}, property_id={property_id}')

        serial_data = get_serial(serial)
        print(f'SERIAL DATA: {serial_data}')

        if not serial_data:
            return jsonify({'error': 'Invalid serial'}), 403

        raw = serial_data.get('company_id', '')
        company_ids = [c.strip() for c in raw.split(',')]

        payload = {
            'startIndex':    0,
            'NoOfRecords':   1,
            'SortBy':        'updated',
            'SearchType':    '',
            'PropertyTypes': '',
            'AgentIDs':      'false',
            'CompanyIDs':    company_ids[0],
            'Id':            int(property_id),
        }

        response = requests.post(
            'https://searchv2.realnex.com/api/v2/SearchListing1',
            data=payload,
            headers={'Content-Type': 'application/x-www-form-urlencoded'},
            timeout=30,
        )
        print(f'REALNEX STATUS: {response.status_code}')
        print(f'REALNEX BODY: {response.text[:500]}')

        result   = response.json()
        listings = result[0] if isinstance(result, list) and len(result) > 0 else []
        prop     = listings[0] if isinstance(listings, list) and len(listings) > 0 else None

        if not prop:
            return jsonify({'error': 'Property not found'}), 404

        # ── Census demographics ────────────────────────────────────────────
        zip_code     = prop.get('Zip', '')
        demographics = None
        if zip_code:
            try:
                census_url = (
                    f'https://api.census.gov/data/2022/acs/acs5'
                    f'?get=B01003_001E,B19013_001E,B25077_001E,B23025_005E'
                    f'&for=zip%20code%20tabulation%20area:{zip_code}'
                )
                census_r = requests.get(census_url, timeout=10)
                if census_r.status_code == 200:
                    census_data = census_r.json()
                    if len(census_data) > 1:
                        h = census_data[0]
                        v = census_data[1]
                        def safe_int(val):
                            try:
                                n = int(val)
                                return n if n > 0 else None
                            except Exception:
                                return None
                        demographics = {
                            'population':        safe_int(v[h.index('B01003_001E')]),
                            'median_income':     safe_int(v[h.index('B19013_001E')]),
                            'median_home_value': safe_int(v[h.index('B25077_001E')]),
                            'unemployment':      safe_int(v[h.index('B23025_005E')]),
                        }
            except Exception as e:
                print(f'Census error: {e}')

        # ── Nominatim neighborhood ─────────────────────────────────────────
        neighborhood = None
        lat = prop.get('AddrLatitude')
        lon = prop.get('AddrLongitude')
        if lat and lon:
            try:
                nom_r = requests.get(
                    f'https://nominatim.openstreetmap.org/reverse'
                    f'?lat={lat}&lon={lon}&format=json&addressdetails=1',
                    headers={'User-Agent': 'RealNexListingsPro/1.0'},
                    timeout=8,
                )
                if nom_r.status_code == 200:
                    nom_data = nom_r.json()
                    addr     = nom_data.get('address', {})
                    neighborhood = {
                        'neighborhood': (addr.get('neighbourhood') or addr.get('suburb')
                                         or addr.get('quarter')),
                        'county':  addr.get('county'),
                        'city':    addr.get('city') or addr.get('town'),
                        'state':   addr.get('state'),
                        'display': nom_data.get('display_name', '')[:120],
                    }
            except Exception as e:
                print(f'Nominatim error: {e}')

        return jsonify({
            'property':     prop,
            'demographics': demographics,
            'neighborhood': neighborhood,
            'walk_score':   None,
        })

    except Exception as e:
        import traceback
        tb = traceback.format_exc()
        print(f'PROPERTY ERROR: {tb}')
        return jsonify({'error': str(e), 'detail': tb}), 500


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

    domain = data.get('domain', '').strip()

    try:
        register_serial(serial, company_id, email, plan, expires_at, jwt)
        if domain:
            update_serial_domain(serial, domain)
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


def _get_listings_ssr(company_id: str) -> list:
    """Fetch up to 50 listings for SSR — used by /embed for bots."""
    try:
        payload = {
            'startIndex': '0', 'NoOfRecords': '50', 'SortBy': 'updated',
            'SearchType': '', 'PropertyTypes': '', 'AgentIDs': 'false',
            'CompanyIDs': [c.strip() for c in company_id.split(',')],
        }
        resp = requests.post(
            REALNEX_SEARCH_API, data=payload,
            headers={'Content-Type': 'application/x-www-form-urlencoded'}, timeout=30,
        )
        resp.raise_for_status()
        result = resp.json()
        return result[0] if isinstance(result, list) and result else []
    except Exception as exc:
        print(f'[SSR] listings fetch error: {exc}')
        return []


def _render_seo_html(listings: list, company_name: str) -> str:
    """Generate a static HTML page of listings for search-engine crawlers."""
    cards = ''
    for p in listings[:50]:
        name  = p.get('PropertyName', '')
        addr  = ', '.join(filter(None, [p.get('Street'), p.get('City'), p.get('State'), p.get('Zip')]))
        price = ''
        if p.get('PriceDisclosed') and p.get('ListPrice'):
            price = f'${int(p["ListPrice"]):,}'
        img = ''
        for att in (p.get('Attachments') or []):
            if att.get('AttachmentType') == 'photo' and att.get('FileName'):
                img = att['FileName'] + '?h=400&mode=max&autorotate=true'
                break
        cards += (
            f'<article class="rnlp-ssr-card">'
            f'{f"<img src=\"{img}\" alt=\"{name}\" loading=\"lazy\">" if img else ""}'
            f'<div class="rnlp-ssr-body">'
            f'<h3>{name}</h3><p>{addr}</p>'
            f'{f"<p><strong>{price}</strong></p>" if price else ""}'
            f'<p>{p.get("ListingType", "")}</p>'
            f'</div></article>'
        )
    return (
        f'<!DOCTYPE html><html lang="en"><head>'
        f'<meta charset="UTF-8"><meta name="viewport" content="width=device-width,initial-scale=1">'
        f'<title>{company_name} — Commercial Real Estate Listings</title>'
        f'<style>'
        f'body{{font-family:sans-serif;margin:0;padding:20px;background:#f8fafc}}'
        f'h1{{text-align:center;color:#013161;margin-bottom:24px}}'
        f'.rnlp-ssr-grid{{display:grid;grid-template-columns:repeat(auto-fill,minmax(280px,1fr));gap:20px;max-width:1200px;margin:0 auto}}'
        f'.rnlp-ssr-card{{background:#fff;border-radius:8px;overflow:hidden;box-shadow:0 1px 4px rgba(0,0,0,.08)}}'
        f'.rnlp-ssr-card img{{width:100%;height:180px;object-fit:cover;display:block}}'
        f'.rnlp-ssr-body{{padding:16px}}'
        f'.rnlp-ssr-body h3{{margin:0 0 4px;font-size:16px}}'
        f'.rnlp-ssr-body p{{margin:2px 0;font-size:13px;color:#64748b}}'
        f'</style></head><body>'
        f'<h1>{company_name}</h1>'
        f'<div class="rnlp-ssr-grid">{cards}</div>'
        f'</body></html>'
    )


def _render_embed_html(serial: str, company_name: str, theme: str) -> str:
    """Generate the browser-facing embed page that bootstraps widget.js."""
    return (
        f'<!DOCTYPE html><html lang="en"><head>'
        f'<meta charset="UTF-8"><meta name="viewport" content="width=device-width,initial-scale=1">'
        f'<title>{company_name} — Listings</title>'
        f'</head><body>'
        f'<div id="rnlp-embed"></div>'
        f'<script>window.RNLP_CONFIG = {{serial:"{serial}",theme:"{theme}",target:"#rnlp-embed"}};</script>'
        f'<script src="/widget.js" async></script>'
        f'</body></html>'
    )


@app.route('/embed', methods=['GET'])
def embed():
    """
    GET /embed?serial=...&company=...&theme=...
    Returns SSR HTML for search-engine crawlers, JS embed page for browsers.
    """
    serial = request.args.get('serial', '').strip()
    if not serial:
        return jsonify({'error': 'serial required'}), 400

    row = get_serial(serial)
    if not row or not row['active']:
        return jsonify({'error': 'Invalid or revoked serial'}), 403

    if _is_expired(row['expires_at']):
        return jsonify({'error': 'Serial expired'}), 403

    company_name = request.args.get('company', 'Commercial Listings')
    theme        = request.args.get('theme', 'classic')
    ua           = request.headers.get('User-Agent', '').lower()
    is_bot       = any(b in ua for b in [
        'googlebot', 'bingbot', 'slurp', 'duckduckbot', 'baiduspider',
        'yandexbot', 'facebot', 'crawler', 'spider', 'bot',
    ])

    if is_bot:
        listings = _get_listings_ssr(row['company_id'])
        html     = _render_seo_html(listings, company_name)
    else:
        html = _render_embed_html(serial, company_name, theme)

    return html, 200, {'Content-Type': 'text/html; charset=utf-8'}


@app.route('/site/<serial>', methods=['GET'])
def serve_site(serial):
    """
    GET /site/<serial>?theme=...&company=...
    Serves a full standalone listing page for the given serial.
    """
    row = get_serial(serial)
    if not row or not row.get('active', True):
        return (
            '<!DOCTYPE html><html><body style="font-family:sans-serif;'
            'background:#0d1828;color:#fff;display:flex;align-items:center;'
            'justify-content:center;height:100vh;text-align:center;margin:0">'
            '<div><div style="font-size:48px;margin-bottom:16px">&#128274;</div>'
            '<h2 style="margin-bottom:10px">Invalid Serial</h2>'
            '<p style="color:#7a90b0">Contact '
            '<a href="mailto:msmith@initial3development.com" style="color:#c9a84c">'
            'msmith@initial3development.com</a></p></div></body></html>'
        ), 403, {'Content-Type': 'text/html; charset=utf-8'}

    if _is_expired(row['expires_at']):
        return (
            '<!DOCTYPE html><html><body style="font-family:sans-serif;'
            'background:#0d1828;color:#fff;display:flex;align-items:center;'
            'justify-content:center;height:100vh;text-align:center;margin:0">'
            '<div><div style="font-size:48px;margin-bottom:16px">&#9203;</div>'
            '<h2 style="margin-bottom:10px">Serial Expired</h2>'
            '<p style="color:#7a90b0">Contact '
            '<a href="mailto:msmith@initial3development.com" style="color:#c9a84c">'
            'msmith@initial3development.com</a> to renew.</p></div></body></html>'
        ), 403, {'Content-Type': 'text/html; charset=utf-8'}

    theme   = request.args.get('theme', 'darkgold')
    company = request.args.get('company', 'Commercial Real Estate')

    template_path = os.path.join(os.path.dirname(__file__), 'templates', 'embed.html')
    try:
        with open(template_path, 'r', encoding='utf-8') as f:
            html = f.read()
    except FileNotFoundError:
        return jsonify({'error': 'embed template not found'}), 500

    html = html.replace('{{SERIAL}}', serial)
    html = html.replace('{{THEME}}', theme)
    html = html.replace('{{COMPANY}}', company)
    return html, 200, {'Content-Type': 'text/html; charset=utf-8'}


@app.route('/widget.js', methods=['GET'])
def widget_js():
    """
    GET /widget.js
    Standalone widget bootstrap script. Reads window.RNLP_CONFIG and renders
    listings into the target element — works on any HTML page, no WordPress needed.
    """
    js = r"""(function(){
var cfg=window.RNLP_CONFIG||{};
if(!cfg.serial){console.warn('[RNLP] no serial configured');return;}
var target=document.querySelector(cfg.target||'#rnlp-embed');
if(!target){console.warn('[RNLP] target element not found');return;}
var proxy='https://rnlp-proxy.onrender.com';
target.innerHTML='<p style="font-family:sans-serif;color:#64748b;padding:20px">Loading listings\u2026</p>';
fetch(proxy+'/listings',{
  method:'POST',
  headers:{'Content-Type':'application/json'},
  body:JSON.stringify({serial:cfg.serial,filters:{startIndex:0,NoOfRecords:50,SortBy:'updated',SortHow:'desc',SearchType:''}})
})
.then(function(r){return r.json();})
.then(function(data){
  var listings=Array.isArray(data)?data[0]:(data.listings||[]);
  if(!listings||!listings.length){target.innerHTML='<p style="font-family:sans-serif;color:#94a3b8;padding:20px">No listings found.</p>';return;}
  var html='<div style="display:grid;grid-template-columns:repeat(auto-fill,minmax(280px,1fr));gap:20px;font-family:sans-serif">';
  listings.forEach(function(p){
    var img='';var atts=p.Attachments||[];
    for(var i=0;i<atts.length;i++){if(atts[i].AttachmentType==='photo'&&atts[i].FileName){img=atts[i].FileName+'?h=400&mode=max&autorotate=true';break;}}
    var addr=[p.Street,p.City,p.State,p.Zip].filter(Boolean).join(', ');
    var price=(p.PriceDisclosed&&p.ListPrice)?'$'+parseInt(p.ListPrice).toLocaleString():'';
    html+='<div style="background:#fff;border-radius:8px;overflow:hidden;box-shadow:0 1px 4px rgba(0,0,0,.1)">';
    if(img)html+='<img src="'+img+'" alt="" style="width:100%;height:160px;object-fit:cover;display:block">';
    html+='<div style="padding:14px"><div style="font-weight:600;font-size:15px;margin-bottom:4px">'+(p.PropertyName||'')+'</div>';
    html+='<div style="font-size:12px;color:#64748b">'+addr+'</div>';
    if(price)html+='<div style="font-size:13px;font-weight:600;color:#013161;margin-top:6px">'+price+'</div>';
    html+='<div style="font-size:11px;color:#94a3b8;margin-top:4px">'+(p.ListingType||'')+'</div></div></div>';
  });
  html+='</div>';
  target.innerHTML=html;
})
.catch(function(e){console.error('[RNLP]',e);target.innerHTML='<p style="font-family:sans-serif;color:#ef4444;padding:20px">Failed to load listings.</p>';});
})();"""
    return js, 200, {'Content-Type': 'application/javascript; charset=utf-8'}


@app.route('/report', methods=['POST'])
def report():
    """
    POST /report  (no auth — fire-and-forget from plugin)
    Body: { "serial", "domain", "event", "timestamp", ... }
    Logs tamper/anomaly events to the reports table.
    """
    import json
    data   = request.get_json(silent=True) or {}
    serial = data.get('serial', '').strip()
    domain = data.get('domain', '').strip()
    event  = data.get('event', 'unknown').strip()
    try:
        log_report(serial, domain, event, json.dumps(data))
    except Exception as exc:
        print(f'[/report] log error: {exc}')
    return jsonify({'ok': True}), 200


@app.route('/reports', methods=['GET'])
@require_admin_key
def reports():
    """
    GET /reports  (X-Admin-Key required)
    Returns recent tamper/anomaly reports.
    """
    return jsonify(get_all_reports())


if __name__ == '__main__':
    app.run(debug=False)
