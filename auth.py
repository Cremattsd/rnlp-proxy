import os
import functools
from flask import request, jsonify


def require_admin_key(f):
    """Decorator that enforces X-Admin-Key header authentication."""
    @functools.wraps(f)
    def wrapper(*args, **kwargs):
        admin_key = os.getenv('ADMIN_KEY', '')
        if not admin_key:
            return jsonify({'error': 'Server not configured — ADMIN_KEY missing'}), 500
        provided = request.headers.get('X-Admin-Key', '')
        if not provided or provided != admin_key:
            return jsonify({'error': 'Unauthorized'}), 401
        return f(*args, **kwargs)
    return wrapper
