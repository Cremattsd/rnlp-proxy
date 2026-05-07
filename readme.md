# rnlp-proxy

Flask proxy server for **RealNex Listings Pro** WordPress plugin.

Validates plugin serial numbers and proxies listing search requests to the
RealNex Search API — keeping `company_id` hidden from the WordPress site.

---

## Endpoints

### `POST /validate`
Check if a serial number is valid.

**Request**
```json
{ "serial": "XXXX-XXXX-XXXX-XXXX" }
```

**Response**
```json
{
  "valid": true,
  "company_id": "63935",
  "plan": "pro",
  "expires_at": "2027-01-01T00:00:00+00:00"
}
```

---

### `GET /health`
Lightweight production health check.

```json
{
  "success": true,
  "service": "realnex-marketplace-proxy",
  "status": "ok",
  "timestamp": "2026-05-06T00:00:00+00:00"
}
```

---

### `GET /version`
Expose deployed service version and environment.

```json
{
  "success": true,
  "service": "realnex-marketplace-proxy",
  "version": "3.5.0",
  "environment": "production"
}
```

---

### `POST /listings`
Proxy a listing search to RealNex. Requires a valid serial.
The server injects `company_id` automatically — never trust client-supplied `CompanyIDs`.

**Request**
```json
{
  "serial": "XXXX-XXXX-XXXX-XXXX",
  "filters": {
    "startIndex": 0,
    "NoOfRecords": 10000,
    "SortBy": "updated",
    "SortHow": "asc",
    "AgentIDs": ["199676"],
    "PropertyTypes": ["OFC", "IND"],
    "SearchType": ""
  }
}
```

**Response:** proxied JSON from `searchv2.realnex.com/api/v2/SearchListing1`

---

### `POST /register` *(Admin — requires `X-Admin-Key` header)*
Issue or update a serial number.

**Request**
```json
{
  "serial": "XXXX-XXXX-XXXX-XXXX",
  "company_id": "63935",
  "email": "client@example.com",
  "plan": "pro",
  "expires_at": "2027-01-01T00:00:00"
}
```

---

### `POST /revoke` *(Admin — requires `X-Admin-Key` header)*
Deactivate a serial.

```json
{ "serial": "XXXX-XXXX-XXXX-XXXX" }
```

---

### `GET /serials` *(Admin — requires `X-Admin-Key` header)*
List all registered serials with metadata.

---

## Environment Variables

| Variable       | Description                              | Default       |
|----------------|------------------------------------------|---------------|
| `ADMIN_KEY`    | Secret key for admin endpoints           | *(required)*  |
| `DATABASE_URL` | Path to SQLite database file             | `serials.db`  |

Copy `.env.example` → `.env` and fill in values.

---

## Local Setup

```bash
python -m venv venv
source venv/bin/activate       # Windows: venv\Scripts\activate
pip install -r requirements.txt
cp .env.example .env           # edit ADMIN_KEY
python app.py
```

---

## Deploy to Render

1. Push this repo to GitHub
2. Create a new **Web Service** on [render.com](https://render.com)
3. Connect the repo — Render will use `render.yaml` automatically
4. Set `ADMIN_KEY` in the Render environment dashboard

---

## Issuing a Serial

```bash
curl -X POST https://api.initial3development.com/register \
  -H "Content-Type: application/json" \
  -H "X-Admin-Key: your-secret-key" \
  -d '{
    "serial": "RNLP-ABCD-1234-WXYZ",
    "company_id": "63935",
    "email": "client@example.com",
    "plan": "pro",
    "expires_at": "2027-01-01T00:00:00"
  }'
```
