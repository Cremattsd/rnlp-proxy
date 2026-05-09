"""
RealNex Listings Pro — Proxy Server
Validates plugin serials and proxies listing requests to the RealNex Search API.
"""

from __future__ import annotations

from flask import Flask, request, jsonify, send_file
from flask_cors import CORS
from dotenv import load_dotenv
from datetime import datetime, timezone
from urllib.parse import urlencode, quote
from email.message import EmailMessage
import requests
import os
import json
import smtplib
import re

from db import init_db, get_serial, register_serial, revoke_serial, get_all_serials, log_report, get_all_reports, update_serial_domain, check_lead_rate, log_lead_attempt
from auth import require_admin_key

load_dotenv()

app = Flask(__name__)
CORS(app, origins="*")

SERVICE_NAME = 'realnex-marketplace-proxy'
SERVICE_VERSION = os.getenv('SERVICE_VERSION', '3.5.0')
PLUGIN_VERSION = os.getenv('PLUGIN_VERSION', '3.6.0')
PLUGIN_ZIP_PATH = os.getenv('PLUGIN_ZIP_PATH', 'realnex-listings-pro-latest.zip')
ENVIRONMENT = os.getenv('ENVIRONMENT', 'production')
PUBLIC_API_BASE = os.getenv('PUBLIC_API_BASE', 'https://api.initial3development.com')
REALNEX_SEARCH_API = 'https://searchv2.realnex.com/api/v2/SearchListing1'
REALNEX_CRM_API    = 'https://sync.realnex.com'
CENSUS_API         = 'https://api.census.gov/data/2022/acs/acs5'
PUBLIC_LEAD_SUCCESS = 'Thanks. Your inquiry was sent to the listing team.'


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


def _extract_listing_rows(payload) -> list:
    if isinstance(payload, list) and payload and isinstance(payload[0], list):
        return payload[0]
    if isinstance(payload, list):
        return payload
    if isinstance(payload, dict):
        for key in ('listings', 'data', 'items'):
            if isinstance(payload.get(key), list):
                return payload[key]
    return []


def _listing_count(payload, rows: list) -> int:
    if isinstance(payload, list):
        for item in payload[1:]:
            if isinstance(item, int):
                return item
    return len(rows)


def _fetch_company_listings(company_id: str, limit: int = 12) -> tuple[list, int]:
    filters = {
        'startIndex': '0',
        'NoOfRecords': str(limit),
        'SortBy': 'updated',
        'SortHow': 'desc',
        'SearchType': '',
        'CompanyIDs': [c.strip() for c in company_id.split(',') if c.strip()],
    }
    resp = requests.post(
        REALNEX_SEARCH_API,
        data=_encode_filters(filters),
        headers={'Content-Type': 'application/x-www-form-urlencoded'},
        timeout=30,
    )
    resp.raise_for_status()
    payload = resp.json()
    rows = _extract_listing_rows(payload)
    return rows, _listing_count(payload, rows)


def _company_name_from_listing(listing: dict) -> str:
    company = listing.get('company') if isinstance(listing.get('company'), dict) else {}
    return str(company.get('CompanyName') or listing.get('CompanyName') or '').strip()


def _client_code(name: str, email: str = '', domain: str = '', fallback: str = 'CLIENT') -> str:
    source = name or domain or (email.split('@')[-1] if email else '')
    if name:
        words = re.findall(r'[A-Za-z0-9]+', name.upper())
        code = ''.join(word[0] for word in words[:4])
        if len(code) < 3 and words:
            code = ''.join(words)[:4]
    else:
        cleaned = re.sub(r'^(www\.)', '', source.lower()).split('.')[0]
        code = re.sub(r'[^A-Za-z0-9]', '', cleaned).upper()[:4]
    return (code or fallback)[:8]


def _product_config(product_type: str) -> dict:
    product = (product_type or 'mp_premier_2_0').strip()
    if product == 'iframe_only':
        return {
            'product_type': 'iframe_only',
            'plan': 'iframe',
            'serial_prefix': 'RNLP-IFRAME',
            'iframe_allowed': True,
            'plugin_allowed': False,
        }
    return {
        'product_type': 'mp_premier_2_0',
        'plan': 'pro',
        'serial_prefix': 'RNLP',
        'iframe_allowed': True,
        'plugin_allowed': True,
    }


def _product_from_row(row: dict) -> dict:
    raw_product = (row.get('product_type') or '').strip()
    raw_plan = (row.get('plan') or '').strip()
    if raw_product:
        return _product_config(raw_product)
    if raw_plan in ('iframe', 'iframe_only'):
        return _product_config('iframe_only')
    return _product_config('mp_premier_2_0')


def _generate_serial(product_type: str, client_code: str) -> str:
    config = _product_config(product_type)
    code = re.sub(r'[^A-Z0-9]', '', (client_code or 'CLIENT').upper())[:8] or 'CLIENT'
    prefix = f'{config["serial_prefix"]}-{code}-'
    try:
        existing = get_all_serials()
    except Exception:
        existing = []
    used = []
    for row in existing:
        serial = str(row.get('serial') or '')
        if serial.startswith(prefix):
            try:
                used.append(int(serial.rsplit('-', 1)[-1]))
            except Exception:
                pass
    next_num = max(used or [0]) + 1
    return f'{prefix}{next_num:03d}'


def _valid_plugin_serial(serial: str) -> bool:
    if not serial:
        return False
    row = get_serial(serial)
    if not row or not row.get('active') or _is_expired(row.get('expires_at')):
        return False
    return bool(_product_from_row(row).get('plugin_allowed'))


# ── In-memory enrichment cache (24 hr TTL) ────────────────────────────────
_enrich_cache: dict = {}

# ── Routes ─────────────────────────────────────────────────────────────────

@app.route('/', methods=['GET'])
def root():
    return jsonify({
        'success': True,
        'service': SERVICE_NAME,
        'status': 'ok',
        'health': '/health',
        'version': '/version',
    })


@app.route('/health', methods=['GET'])
def health():
    return jsonify({
        'success': True,
        'service': SERVICE_NAME,
        'status': 'ok',
        'timestamp': datetime.now(timezone.utc).isoformat(),
    })


@app.route('/version', methods=['GET'])
def version():
    return jsonify({
        'success': True,
        'service': SERVICE_NAME,
        'version': SERVICE_VERSION,
        'plugin_version': PLUGIN_VERSION,
        'plugin_info': f'{PUBLIC_API_BASE.rstrip("/")}/plugin-info',
        'environment': ENVIRONMENT,
    })


@app.route('/plugin-info', methods=['GET'])
def plugin_info():
    return jsonify({
        'version': PLUGIN_VERSION,
        'download_url': f'{PUBLIC_API_BASE.rstrip("/")}/download',
        'url': 'https://initial3development.com',
        'requires': '6.0',
        'tested': '6.7',
        'last_updated': datetime.now(timezone.utc).isoformat(),
        'changelog': 'Private RealNex Listings Pro release distributed by Initial3 Development.',
    })


@app.route('/download', methods=['GET'])
def download_plugin():
    serial = request.args.get('serial', '').strip()
    if not _valid_plugin_serial(serial):
        return jsonify({'error': 'Invalid serial'}), 403

    zip_path = os.path.abspath(PLUGIN_ZIP_PATH)
    if not os.path.exists(zip_path):
        return jsonify({'error': 'Plugin package not found'}), 404

    return send_file(
        zip_path,
        as_attachment=True,
        download_name='realnex-listings-pro.zip',
        mimetype='application/zip',
    )

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

    product = _product_from_row(row)
    return jsonify({
        'valid':          True,
        'company_id':     row['company_id'],
        'plan':           row['plan'],
        'product_type':   product['product_type'],
        'iframe_allowed': product['iframe_allowed'],
        'plugin_allowed': product['plugin_allowed'],
        'expires_at':     row['expires_at'],
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
                        'lat':     float(lat),
                        'lon':     float(lon),
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


@app.route('/enrich', methods=['POST'])
def enrich():
    """
    POST /enrich
    Body: { "serial": "...", "lat": <float>, "lon": <float>, "zip": "...", "address": "..." }
    Returns combined enrichment: demographics, walk_score, traffic, environment, solar, amenities
    Results cached 24 hrs in memory.
    """
    try:
        data    = request.get_json(silent=True) or {}
        serial  = data.get('serial', '')
        lat     = data.get('lat')
        lon     = data.get('lon')
        zip_code = str(data.get('zip', '')).strip()
        address  = str(data.get('address', '')).strip()

        serial_data = get_serial(serial)
        if not serial_data:
            return jsonify({'error': 'Invalid serial'}), 403

        cache_key = f'{lat},{lon},{zip_code}'
        now = datetime.now(timezone.utc)
        if cache_key in _enrich_cache:
            cached_at, cached_data = _enrich_cache[cache_key]
            if (now - cached_at).total_seconds() < 86400:
                return jsonify(cached_data)

        result = {
            'demographics': None,
            'walk_score':   None,
            'traffic':      None,
            'environment':  None,
            'solar':        None,
            'amenities':    None,
        }

        def safe_int(val):
            try:
                n = int(val)
                return n if n > 0 else None
            except Exception:
                return None

        # 1. Census demographics
        if zip_code:
            try:
                census_url = (
                    f'https://api.census.gov/data/2022/acs/acs5'
                    f'?get=B01003_001E,B19013_001E,B25077_001E,B23025_005E'
                    f'&for=zip%20code%20tabulation%20area:{zip_code}'
                )
                cr = requests.get(census_url, timeout=10)
                if cr.status_code == 200:
                    cd = cr.json()
                    if len(cd) > 1:
                        h, v = cd[0], cd[1]
                        result['demographics'] = {
                            'population':        safe_int(v[h.index('B01003_001E')]),
                            'median_income':     safe_int(v[h.index('B19013_001E')]),
                            'median_home_value': safe_int(v[h.index('B25077_001E')]),
                            'unemployment':      safe_int(v[h.index('B23025_005E')]),
                        }
            except Exception as e:
                print(f'[/enrich] census error: {e}')

        if lat and lon:
            # 2. Walk Score
            ws_key = os.environ.get('WALKSCORE_API_KEY', '')
            if ws_key:
                try:
                    ws_url = (
                        f'https://api.walkscore.com/score?format=json'
                        f'&address={quote(address)}&lat={lat}&lon={lon}'
                        f'&transit=1&bike=1&wsapikey={ws_key}'
                    )
                    ws_r = requests.get(ws_url, timeout=10)
                    if ws_r.status_code == 200:
                        ws = ws_r.json()
                        result['walk_score'] = {
                            'walk':         ws.get('walkscore'),
                            'walk_desc':    ws.get('description'),
                            'transit':      ws.get('transit', {}).get('score') if ws.get('transit') else None,
                            'transit_desc': ws.get('transit', {}).get('description') if ws.get('transit') else None,
                            'bike':         ws.get('bike', {}).get('score') if ws.get('bike') else None,
                            'bike_desc':    ws.get('bike', {}).get('description') if ws.get('bike') else None,
                        }
                except Exception as e:
                    print(f'[/enrich] walk score error: {e}')

            # 3. Traffic via Overpass (nearest major road)
            try:
                tq = (f'[out:json][timeout:8];'
                      f'(way["highway"~"motorway|trunk|primary|secondary"](around:500,{lat},{lon}););out 1;')
                tr_r = requests.get('https://overpass-api.de/api/interpreter',
                                    params={'data': tq}, timeout=10)
                if tr_r.status_code == 200:
                    ways = tr_r.json().get('elements', [])
                    if ways:
                        tags = ways[0].get('tags', {})
                        hw   = tags.get('highway', '')
                        name = tags.get('name', 'Major Road')
                        tc_map = {
                            'motorway':  {'label': 'Freeway',       'daily': '100,000+',      'level': 'very_high'},
                            'trunk':     {'label': 'Arterial',      'daily': '40,000–80,000', 'level': 'high'},
                            'primary':   {'label': 'Primary Road',  'daily': '20,000–40,000', 'level': 'high'},
                            'secondary': {'label': 'Secondary Road','daily': '5,000–20,000',  'level': 'medium'},
                        }
                        tc = tc_map.get(hw, {'label': 'Local Road', 'daily': '1,000–5,000', 'level': 'low'})
                        result['traffic'] = {'road_name': name, 'road_type': tc['label'],
                                             'daily_estimate': tc['daily'], 'level': tc['level']}
            except Exception as e:
                print(f'[/enrich] traffic error: {e}')

            # 4. EPA EJScreen
            try:
                ej_url = (
                    f'https://ejscreen.epa.gov/mapper/ejscreenRESTbroker.aspx'
                    f'?namestr=&geometry={{"x":{lon},"y":{lat}}}'
                    f'&distance=1&unit=9035&f=pjson'
                )
                ej_r = requests.get(ej_url, timeout=12)
                if ej_r.status_code == 200:
                    ej = ej_r.json()
                    props = (ej.get('data', {}).get('blockgroup_properties')
                             or ej.get('blockgroup_properties') or {})
                    if props:
                        result['environment'] = {
                            'air_quality_pctile':        props.get('P_PM25',  props.get('PM25')),
                            'traffic_proximity_pctile':  props.get('P_PTRAF', props.get('PTRAF')),
                            'superfund_pctile':          props.get('P_PNPL',  props.get('PNPL')),
                            'flood_risk_pctile':         props.get('P_UST',   props.get('UST')),
                        }
            except Exception as e:
                print(f'[/enrich] EJScreen error: {e}')

            # 5. NREL Solar
            nrel_key = os.environ.get('NREL_API_KEY', 'DEMO_KEY')
            try:
                nrel_url = (
                    f'https://developer.nrel.gov/api/solar/solar_resource/v1.json'
                    f'?api_key={nrel_key}&lat={lat}&lon={lon}'
                )
                nrel_r = requests.get(nrel_url, timeout=10)
                if nrel_r.status_code == 200:
                    outputs = nrel_r.json().get('outputs', {})
                    annual  = (outputs.get('avg_ghi') or {}).get('annual')
                    if annual:
                        ghi = round(float(annual), 2)
                        result['solar'] = {
                            'annual_ghi': ghi,
                            'label': 'Excellent' if ghi >= 5.5 else 'Good' if ghi >= 4.5 else 'Moderate',
                        }
            except Exception as e:
                print(f'[/enrich] NREL error: {e}')

            # 6. Overpass amenities
            try:
                aq = (f'[out:json][timeout:10];'
                      f'(node["amenity"~"restaurant|cafe|bank|pharmacy|hospital|parking|supermarket"]'
                      f'(around:800,{lat},{lon}););out body 30;')
                am_r = requests.get('https://overpass-api.de/api/interpreter',
                                    params={'data': aq}, timeout=12)
                if am_r.status_code == 200:
                    amenities: dict = {}
                    for el in am_r.json().get('elements', []):
                        t = el.get('tags', {}).get('amenity')
                        if t:
                            amenities[t] = amenities.get(t, 0) + 1
                    if amenities:
                        result['amenities'] = amenities
            except Exception as e:
                print(f'[/enrich] amenities error: {e}')

        _enrich_cache[cache_key] = (now, result)
        return jsonify(result)

    except Exception as e:
        import traceback
        print(f'[/enrich] ERROR: {traceback.format_exc()}')
        return jsonify({'error': str(e)}), 500


@app.route('/register', methods=['POST'])
@require_admin_key
def register():
    """
    POST /register  (X-Admin-Key required)
    Body: { "serial"?, "company_id", "email"?, "domain"?, "product_type", "expires_at" }
    """
    data = request.get_json(silent=True) or {}
    company_id = data.get('company_id', '').strip()
    email      = data.get('email', '').strip()
    domain     = data.get('domain', '').strip()
    expires_at = data.get('expires_at')
    jwt        = data.get('jwt', '').strip()
    config     = _product_config(data.get('product_type') or data.get('plan'))
    plan       = config['plan']

    if not company_id:
        return jsonify({'error': 'company_id is required'}), 400

    preview = {}
    company_name = data.get('company_name', '').strip()
    try:
        rows, count = _fetch_company_listings(company_id, 5)
        if rows and not company_name:
            company_name = _company_name_from_listing(rows[0])
        preview = {'listing_count': count, 'company_name': company_name}
    except Exception as exc:
        preview = {'warning': f'Company preview unavailable during registration: {exc}'}

    client_code = _client_code(data.get('client_code', '').strip() or company_name, email, domain)
    serial = data.get('serial', '').strip() or _generate_serial(config['product_type'], client_code)

    try:
        register_serial(serial, company_id, email, plan, expires_at, jwt, config['product_type'])
        if domain:
            update_serial_domain(serial, domain)
        embed_src = f'{PUBLIC_API_BASE.rstrip("/")}/embed?serial={quote(serial, safe="")}'
        return jsonify({
            'success': True,
            'serial': serial,
            'company_id': company_id,
            'company_name': company_name,
            'plan': plan,
            'product_type': config['product_type'],
            'expires_at': expires_at,
            'plugin_allowed': config['plugin_allowed'],
            'iframe_allowed': config['iframe_allowed'],
            'plugin_download_url': 'https://www.initial3development.com/realnex-listings-pro' if config['plugin_allowed'] else '',
            'shortcode': '[realnex_listings]' if config['plugin_allowed'] else '',
            'iframe_src': embed_src,
            'iframe_code': (
                f'<iframe\n'
                f'  src="{embed_src}"\n'
                f'  width="100%"\n'
                f'  height="900"\n'
                f'  style="border:0;width:100%;"\n'
                f'  loading="lazy">\n'
                f'</iframe>'
            ),
            'preview': preview,
        })
    except Exception as exc:
        return jsonify({'error': str(exc)}), 500


@app.route('/company-preview', methods=['POST'])
@require_admin_key
def company_preview():
    """
    POST /company-preview  (X-Admin-Key required)
    Body: { "company_id": "35853" }
    """
    data = request.get_json(silent=True) or {}
    company_id = data.get('company_id', '').strip()
    if not company_id:
        return jsonify({'error': 'company_id is required'}), 400

    try:
        rows, count = _fetch_company_listings(company_id, 8)
    except Exception as exc:
        return jsonify({'success': False, 'error': str(exc)}), 502

    company_name = _company_name_from_listing(rows[0]) if rows else ''
    brokers = {}
    samples = []
    for listing in rows[:5]:
        user = (listing.get('UserList') or [listing.get('User') or {}])[0] or {}
        email = str(user.get('Email') or '').strip()
        name = ' '.join([str(user.get('FirstName') or '').strip(), str(user.get('LastName') or '').strip()]).strip()
        if email and email not in brokers:
            brokers[email] = {'name': name, 'email': email, 'phone': user.get('Phone') or ''}
        samples.append({
            'id': listing.get('Id'),
            'title': listing.get('PropertyName') or listing.get('Street') or 'Untitled Listing',
            'address': ', '.join([str(listing.get(k) or '').strip() for k in ('Street', 'City', 'State', 'Zip') if str(listing.get(k) or '').strip()]),
            'status': listing.get('Status') or '',
            'listing_type': listing.get('ListingType') or '',
            'property_type': ', '.join(listing.get('PropertyTypes') or []),
            'broker_email': email,
        })

    return jsonify({
        'success': True,
        'company_id': company_id,
        'listing_count': count,
        'company_name': company_name,
        'client_code': _client_code(company_name),
        'sample_listings': samples,
        'brokers': list(brokers.values()),
        'warning': 'No listings found for this company_id.' if count == 0 else '',
    })


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


def _admin_debug_enabled(data: dict) -> bool:
    admin_key = os.getenv('ADMIN_KEY', '')
    supplied = request.headers.get('X-Admin-Key') or data.get('admin_key') or ''
    return bool(admin_key and supplied and supplied == admin_key)


def _lead_log(serial: str, event: str, payload: dict) -> None:
    try:
        log_report(
            serial,
            request.headers.get('Origin') or request.headers.get('Referer') or '',
            event,
            json.dumps(payload, default=str),
        )
    except Exception as exc:
        print(f'[/lead] log error: {exc}')


def _pick_broker_email(data: dict, row: dict, warnings: list[str]) -> str:
    candidates = [
        data.get('broker_email', ''),
        data.get('listing_broker_email', ''),
        row.get('email', '') if row else '',
        os.getenv('LEAD_EMAIL_FALLBACK', ''),
    ]
    for candidate in candidates:
        email = str(candidate or '').strip()
        if '@' in email:
            return email
    warnings.append('Broker email missing; no email recipient configured')
    return ''


def _send_lead_email(recipient: str, subject: str, body: str) -> dict:
    if not recipient:
        return {
            'sent': False,
            'queued': True,
            'simulated': True,
            'status': 'simulated_no_recipient',
            'warning': 'Broker email missing',
        }

    sendgrid_key = os.getenv('SENDGRID_API_KEY', '').strip()
    from_email = (
        os.getenv('SENDGRID_FROM_EMAIL', '').strip()
        or os.getenv('SMTP_FROM_EMAIL', '').strip()
        or os.getenv('LEAD_EMAIL_FROM', '').strip()
        or os.getenv('LEAD_EMAIL_FALLBACK', '').strip()
    )

    if sendgrid_key and from_email:
        try:
            resp = requests.post(
                'https://api.sendgrid.com/v3/mail/send',
                headers={
                    'Authorization': f'Bearer {sendgrid_key}',
                    'Content-Type': 'application/json',
                },
                json={
                    'personalizations': [{'to': [{'email': recipient}]}],
                    'from': {'email': from_email},
                    'subject': subject,
                    'content': [{'type': 'text/plain', 'value': body}],
                },
                timeout=12,
            )
            if resp.status_code in (200, 202):
                return {'sent': True, 'provider': 'sendgrid', 'status': 'sent'}
            return {'sent': False, 'queued': True, 'status': 'queued', 'warning': f'SendGrid failed with HTTP {resp.status_code}'}
        except Exception as exc:
            return {'sent': False, 'queued': True, 'status': 'queued', 'warning': f'SendGrid exception: {exc}'}

    smtp_host = os.getenv('SMTP_HOST', '').strip()
    if smtp_host and from_email:
        try:
            msg = EmailMessage()
            msg['From'] = from_email
            msg['To'] = recipient
            msg['Subject'] = subject
            msg.set_content(body)

            port = int(os.getenv('SMTP_PORT', '587'))
            username = os.getenv('SMTP_USERNAME', '').strip()
            password = os.getenv('SMTP_PASSWORD', '').strip()
            use_tls = os.getenv('SMTP_USE_TLS', 'true').lower() != 'false'
            with smtplib.SMTP(smtp_host, port, timeout=12) as smtp:
                if use_tls:
                    smtp.starttls()
                if username and password:
                    smtp.login(username, password)
                smtp.send_message(msg)
            return {'sent': True, 'provider': 'smtp', 'status': 'sent'}
        except Exception as exc:
            return {'sent': False, 'queued': True, 'status': 'queued', 'warning': f'SMTP exception: {exc}'}

    return {
        'sent': False,
        'queued': True,
        'simulated': True,
        'status': 'simulated',
        'warning': 'Email provider missing',
    }


def _crm_key(payload: dict) -> str:
    if not isinstance(payload, dict):
        return ''
    return str(payload.get('key') or payload.get('Key') or payload.get('projectKey') or payload.get('ProjectKey') or '').strip()


@app.route('/lead', methods=['POST'])
def lead():
    """
    POST /lead
    Body: { "serial", "name", "email", "phone", "message", "property_id", "property_name" }

    Public response is intentionally clean. CRM/email diagnostics are logged for
    admins and returned only when a valid X-Admin-Key is supplied.
    """
    data          = request.get_json(silent=True) or {}
    serial        = data.get('serial', '').strip()
    name          = data.get('name', '').strip()
    email         = data.get('email', '').strip()
    phone         = data.get('phone', '').strip()
    message       = data.get('message', '').strip()
    property_id   = data.get('property_id', '').strip()
    property_name = data.get('property_name', '').strip()
    address       = data.get('address', '').strip()
    page_url      = data.get('page_url', '').strip()
    source_site   = data.get('source_website', '').strip() or request.headers.get('Origin', '')
    admin_debug   = _admin_debug_enabled(data)

    if not serial:
        return jsonify({'error': 'serial required'}), 400
    if not name or not email:
        return jsonify({'error': 'name and email required'}), 400

    client_ip = request.headers.get('X-Forwarded-For', request.remote_addr or '').split(',')[0].strip()
    rate_error = check_lead_rate(email, client_ip)
    if rate_error:
        return jsonify({'error': rate_error}), 429

    row = get_serial(serial)
    if not row or not row['active']:
        return jsonify({'error': 'Invalid or expired serial'}), 403
    if _is_expired(row['expires_at']):
        return jsonify({'error': 'Serial expired'}), 403

    log_lead_attempt(client_ip, email, serial)

    warnings = []
    now_iso = datetime.now(timezone.utc).isoformat()
    crm_result = {
        'contact': 'skipped',
        'contact_key': '',
        'project': 'skipped',
        'project_key': '',
        'lead_link': 'skipped',
        'history': 'skipped',
        'history_key': '',
        'history_links': 0,
        'warnings': warnings,
    }

    jwt_token = (row.get('jwt') or '').strip()
    contact_key = ''
    project_key = ''

    history_body = '\n'.join([
        f'Name: {name}',
        f'Email: {email}',
        f'Phone: {phone or "Not provided"}',
        f'Message: {message or "Not provided"}',
        f'Property name: {property_name or "Not provided"}',
        f'Address: {address or "Not provided"}',
        f'Listing ID: {property_id or "Not provided"}',
        f'Page URL: {page_url or "Not provided"}',
        f'Source website: {source_site or "Not provided"}',
        f'Timestamp: {now_iso}',
    ])

    if jwt_token:
        parts      = name.split(' ', 1)
        first_name = parts[0]
        last_name  = parts[1] if len(parts) > 1 else ''
        crm = REALNEX_CRM_API
        hdrs = {
            'Content-Type':        'application/json',
            'Authorization':       f'Bearer {jwt_token}',
            'Crm-ApplicationName': 'RealNexListingsPro',
        }

        try:
            sr = requests.get(
                f'{crm}/api/v1/CrmOData/Contacts',
                params={'$filter': f"email eq '{email}'", '$top': '1'},
                headers=hdrs, timeout=15,
            )
            if sr.status_code == 200:
                contacts = sr.json().get('value', [])
                if contacts:
                    contact_key = _crm_key(contacts[0])
                    crm_result['contact'] = 'found'
            else:
                warnings.append(f'Contact search failed HTTP {sr.status_code}')
        except Exception as exc:
            warnings.append(f'Contact search failed: {exc}')

        if not contact_key:
            try:
                cr = requests.post(
                    f'{crm}/api/v1/Crm/contact',
                    json={
                        'fullName': name,
                        'firstName': first_name,
                        'lastName': last_name,
                        'email': email,
                        'mobile': phone,
                        'prospect': True,
                    },
                    headers=hdrs, timeout=15,
                )
                if cr.status_code in (200, 201, 202):
                    contact_key = _crm_key(cr.json())
                    crm_result['contact'] = 'created' if contact_key else 'create_returned_no_key'
                else:
                    warnings.append(f'Contact create failed HTTP {cr.status_code}')
            except Exception as exc:
                warnings.append(f'Contact create failed: {exc}')
        crm_result['contact_key'] = contact_key

        filters = []
        if property_id:
            filters.append(f"contains(notes,'{property_id}')")
        if property_name:
            safe_name = property_name.replace("'", "''")
            filters.append(f"contains(projectName,'{safe_name}') or contains(notes,'{safe_name}')")
        if address:
            safe_address = address.replace("'", "''")
            filters.append(f"contains(notes,'{safe_address}')")

        for flt in filters:
            try:
                pr = requests.get(
                    f'{crm}/api/v1/CrmOData/Projects',
                    params={'$filter': flt, '$top': '1'},
                    headers=hdrs, timeout=15,
                )
                if pr.status_code == 200:
                    projects = pr.json().get('value', [])
                    if projects:
                        project_key = _crm_key(projects[0])
                        crm_result['project'] = 'found'
                        break
                else:
                    warnings.append(f'Project search failed HTTP {pr.status_code}')
            except Exception as exc:
                warnings.append(f'Project search failed: {exc}')

        if not project_key:
            project_notes = history_body + '\nCRM write result summary: Project created from website inquiry.'
            try:
                cp = requests.post(
                    f'{crm}/api/v1/Crm/project',
                    json={
                        'projectName': f'Website Inquiry - {property_name or address or email}',
                        'subject': f'Website Inquiry - {property_name or "Property Inquiry"}',
                        'notes': project_notes,
                        'dateOpened': now_iso,
                    },
                    headers=hdrs, timeout=15,
                )
                if cp.status_code in (200, 201, 202):
                    project_key = _crm_key(cp.json())
                    crm_result['project'] = 'created' if project_key else 'create_returned_no_key'
                else:
                    warnings.append(f'Project create failed HTTP {cp.status_code}')
            except Exception as exc:
                warnings.append(f'Project create failed: {exc}')
        crm_result['project_key'] = project_key

        if contact_key and project_key:
            try:
                lr = requests.post(
                    f'{crm}/api/v1/Crm/project/{quote(project_key, safe="")}/lead',
                    json={
                        'published': True,
                        'objectKey': contact_key,
                        'notes': 'Website inquiry lead.',
                    },
                    headers=hdrs, timeout=15,
                )
                if lr.status_code in (200, 201, 202):
                    crm_result['lead_link'] = 'created'
                else:
                    crm_result['lead_link'] = 'failed'
                    warnings.append(f'Project lead link failed HTTP {lr.status_code}')
            except Exception as exc:
                crm_result['lead_link'] = 'failed'
                warnings.append(f'Project lead link failed: {exc}')

        result_lines = [
            f'Contact: {crm_result["contact"]}',
            f'Project: {crm_result["project"]}',
            f'Lead link: {crm_result["lead_link"]}',
        ]
        notes_text = history_body + '\nCRM write result summary:\n' + '\n'.join(result_lines)
        try:
            hr = requests.post(
                f'{crm}/api/v1/Crm/history',
                json={
                    'subject':      f'Website Inquiry - {property_name or "Property Inquiry"}',
                    'notes':        notes_text,
                    'startDate':    now_iso,
                    'endDate':      now_iso,
                    'timeless':     False,
                    'eventTypeKey': 1,
                    'published':    True,
                    'projectKey':   project_key or None,
                },
                headers=hdrs, timeout=15,
            )
            if hr.status_code in (200, 201, 202):
                history_key = _crm_key(hr.json())
                crm_result['history'] = 'created' if history_key else 'create_returned_no_key'
                crm_result['history_key'] = history_key
            else:
                warnings.append(f'History create failed HTTP {hr.status_code}')
        except Exception as exc:
            warnings.append(f'History create failed: {exc}')

        if crm_result['history_key']:
            objects = []
            if contact_key:
                objects.append({'key': contact_key, 'type': 'Contact', 'description': name})
            if project_key:
                objects.append({'key': project_key, 'type': 'Project', 'description': property_name or 'Website Inquiry'})
            if objects:
                try:
                    ho = requests.post(
                        f'{crm}/api/v1/Crm/history/{quote(crm_result["history_key"], safe="")}/object',
                        json=objects,
                        headers=hdrs, timeout=10,
                    )
                    if ho.status_code in (200, 201, 202):
                        crm_result['history_links'] = len(objects)
                    else:
                        warnings.append(f'History object link failed HTTP {ho.status_code}')
                except Exception as exc:
                    warnings.append(f'History object link failed: {exc}')
    else:
        crm_result['status'] = 'skipped_no_jwt'
        warnings.append('CRM JWT missing; writeback skipped')

    if jwt_token and 'status' not in crm_result:
        crm_result['status'] = 'attempted'

    recipient = _pick_broker_email(data, row, warnings)
    email_result = _send_lead_email(
        recipient,
        f'New Listing Inquiry - {property_name or "Property Inquiry"}',
        '\n'.join([
            f'Property name: {property_name or "Not provided"}',
            f'Address: {address or "Not provided"}',
            f'Listing URL: {page_url or "Not provided"}',
            f'Lead name: {name}',
            f'Email: {email}',
            f'Phone: {phone or "Not provided"}',
            f'Message: {message or "Not provided"}',
            f'Timestamp: {now_iso}',
            f'Source: {source_site or "Not provided"}',
            '',
            'CRM result:',
            f'- Contact: {crm_result["contact"]}',
            f'- Project: {crm_result["project"]}',
            f'- Lead link: {crm_result["lead_link"]}',
            f'- History: {crm_result["history"]}',
            f'- Warnings: {", ".join(warnings) if warnings else "None"}',
        ]),
    )
    if email_result.get('warning'):
        warnings.append(email_result['warning'])

    crm_status = crm_result.get('status') or 'attempted'
    email_status = 'simulated' if email_result.get('simulated') else (email_result.get('status') or ('sent' if email_result.get('sent') else 'queued'))
    lead_status = 'queued'
    serial_status = 'valid'
    company_id_status = 'present' if row.get('company_id') else 'missing'

    diagnostic = {
        'serial_status': serial_status,
        'company_id': company_id_status,
        'lead_status': lead_status,
        'email_status': email_status,
        'crm_status': crm_status,
        'lead': {
            'serial': serial,
            'email': email,
            'property_id': property_id,
            'property_name': property_name,
            'page_url': page_url,
            'source_website': source_site,
        },
        'crm': crm_result,
        'email': {
            'recipient': recipient,
            'sent': email_result.get('sent', False),
            'queued': email_result.get('queued', False),
            'provider': email_result.get('provider', ''),
            'simulated': email_result.get('simulated', False),
        },
    }
    _lead_log(serial, 'lead_submitted', diagnostic)

    response = {'success': True, 'message': PUBLIC_LEAD_SUCCESS}
    if admin_debug:
        response['admin_diagnostics'] = diagnostic
    return jsonify(response)


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
        img_html = f'<img src="{img}" alt="{name}" loading="lazy">' if img else ''
        price_html = f'<p><strong>{price}</strong></p>' if price else ''
        cards += (
            f'<article class="rnlp-ssr-card">'
            f'{img_html}'
            f'<div class="rnlp-ssr-body">'
            f'<h3>{name}</h3><p>{addr}</p>'
            f'{price_html}'
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
        f'<script src="{PUBLIC_API_BASE.rstrip("/")}/widget.js" async></script>'
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
var proxy='""" + PUBLIC_API_BASE.rstrip('/') + r"""';
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
