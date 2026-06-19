"""
Instagram OSINT-Modul — Standortbestimmung & Soziale Analyse.

Analysiert:
  1. Eigene Posts: Geotagged Standorte → Aufenthaltskarte
  2. Zeitliche Muster: Wann ist die Person aktiv → Zeitzone / Rhythmus
  3. Soziales Netzwerk: Wer taggt die Person, wen taggt sie → Umfeld
  4. Follower/Following Crossposts: In welchen Fotos taucht die Person auf
  5. Gesichtserkennung: OpenCV-Detektion in heruntergeladenen Bildern

Setup:
  pip install instaloader opencv-python-headless numpy requests

Für private Profile oder mehr Daten:
  Setze: IG_USERNAME=dein_ig_user  IG_PASSWORD=dein_ig_passwort
"""

import io
import json
import logging
import os
import sqlite3
import tempfile
import threading
import urllib.request
from collections import Counter
from datetime import datetime, timezone
from math import radians, sin, cos, sqrt, atan2

import cv2
import instaloader
import numpy as np

log = logging.getLogger(__name__)

DB_PATH = os.path.join(os.path.dirname(__file__), '..', 'tracking.db')

# Gesichts-Detektor (Haar Cascade — kein Modell-Download nötig)
_CASCADE_PATH = cv2.data.haarcascades + 'haarcascade_frontalface_default.xml'
_face_cascade = cv2.CascadeClassifier(_CASCADE_PATH)


# ---------------------------------------------------------------------------
# Hilfsfunktionen
# ---------------------------------------------------------------------------

def _haversine_km(lat1, lon1, lat2, lon2) -> float:
    """Luftlinienabstand in km zwischen zwei GPS-Punkten."""
    R = 6371.0
    φ1, φ2 = radians(lat1), radians(lat2)
    dφ = radians(lat2 - lat1)
    dλ = radians(lon2 - lon1)
    a = sin(dφ / 2) ** 2 + cos(φ1) * cos(φ2) * sin(dλ / 2) ** 2
    return R * 2 * atan2(sqrt(a), sqrt(1 - a))


def _weighted_center(points: list[dict]) -> dict | None:
    """Gewichteter Mittelpunkt (häufigere Orte zählen mehr)."""
    if not points:
        return None
    total = len(points)
    lat = sum(p['lat'] for p in points) / total
    lon = sum(p['lon'] for p in points) / total
    return {'lat': round(lat, 5), 'lon': round(lon, 5)}


def _detect_faces_in_url(image_url: str) -> int:
    """Lädt Bild von URL und gibt Anzahl erkannter Gesichter zurück."""
    try:
        req = urllib.request.Request(image_url, headers={'User-Agent': 'Mozilla/5.0'})
        with urllib.request.urlopen(req, timeout=8) as resp:
            data = np.frombuffer(resp.read(), dtype=np.uint8)
        img = cv2.imdecode(data, cv2.IMREAD_GRAYSCALE)
        if img is None:
            return 0
        faces = _face_cascade.detectMultiScale(img, scaleFactor=1.1, minNeighbors=5, minSize=(30, 30))
        return len(faces)
    except Exception:
        return 0


def _get_loader() -> instaloader.Instaloader:
    L = instaloader.Instaloader(
        download_pictures=False,
        download_videos=False,
        download_video_thumbnails=False,
        download_geotags=True,
        download_comments=False,
        save_metadata=False,
        quiet=True,
    )
    ig_user = os.environ.get('IG_USERNAME', '')
    ig_pass = os.environ.get('IG_PASSWORD', '')
    if ig_user and ig_pass:
        try:
            L.login(ig_user, ig_pass)
            log.info('Instagram: eingeloggt als %s', ig_user)
        except Exception as e:
            log.warning('Instagram Login fehlgeschlagen: %s', e)
    return L


# ---------------------------------------------------------------------------
# Kern-Analyse
# ---------------------------------------------------------------------------

def analyze_profile(username: str, max_posts: int = 40,
                    analyze_network: bool = True,
                    detect_faces: bool = True) -> dict:
    """
    Vollständige OSINT-Analyse eines Instagram-Profils.
    Gibt ein dict zurück das direkt als JSON in der DB gespeichert wird.
    """
    L = _get_loader()

    try:
        profile = instaloader.Profile.from_username(L.context, username)
    except instaloader.exceptions.ProfileNotExistsException:
        return {'error': f'Profil @{username} nicht gefunden'}
    except Exception as e:
        return {'error': str(e)}

    result = {
        'username':    username,
        'full_name':   profile.full_name,
        'bio':         profile.biography,
        'followers':   profile.followers,
        'following':   profile.followees,
        'post_count':  profile.mediacount,
        'is_private':  profile.is_private,
        'profile_pic': profile.profile_pic_url,
        'analyzed_at': datetime.utcnow().isoformat(),
        'posts':       [],
        'locations':   [],
        'network':     {'tagged_by': {}, 'tags_others': {}},
        'faces_detected': 0,
        'activity_hours': [],
        'top_locations': [],
        'probable_location': None,
    }

    if profile.is_private:
        result['warning'] = 'Privates Profil — nur öffentliche Metadaten verfügbar'
        return result

    hour_counts = Counter()
    all_hashtags = Counter()

    # --- Eigene Posts analysieren ---
    for post in profile.get_posts():
        if len(result['posts']) >= max_posts:
            break

        post_data = {
            'shortcode': post.shortcode,
            'timestamp': post.date_utc.isoformat(),
            'hour_utc':  post.date_utc.hour,
            'likes':     post.likes,
            'caption':   (post.caption or '')[:300],
            'hashtags':  list(post.caption_hashtags),
            'tagged_users': list(post.tagged_users),
            'image_url': post.url,
            'location':  None,
            'faces':     0,
        }

        # Standort aus Post-Geotag
        if post.location and post.location.lat:
            loc = {
                'name': post.location.name,
                'lat':  round(float(post.location.lat), 5),
                'lon':  round(float(post.location.lng), 5),
            }
            post_data['location'] = loc
            result['locations'].append({**loc, 'timestamp': post_data['timestamp'],
                                        'source': 'own_post'})

        # Netzwerk: wen taggt die Person
        for tagged in post.tagged_users:
            result['network']['tags_others'][tagged] = \
                result['network']['tags_others'].get(tagged, 0) + 1

        # Gesichtserkennung (optional, verlangsamt die Analyse)
        if detect_faces and post.url:
            n = _detect_faces_in_url(post.url)
            post_data['faces'] = n
            result['faces_detected'] += n

        hour_counts[post.date_utc.hour] += 1
        all_hashtags.update(post.caption_hashtags)
        result['posts'].append(post_data)

    result['activity_hours'] = [hour_counts.get(h, 0) for h in range(24)]
    result['top_hashtags']   = dict(all_hashtags.most_common(15))

    # --- Follower/Following: wer taggt die Zielperson ---
    if analyze_network and not profile.is_private:
        _analyze_network(L, username, profile, result, max_accounts=20)

    # --- Standortauswertung ---
    _compute_location_stats(result)

    return result


def _analyze_network(L, username, profile, result, max_accounts=20):
    """
    Prüft Follower und Following:
    - Lädt ihre neuesten Posts
    - Sucht nach Tags der Zielperson
    - Extrahiert Standorte dieser Posts (→ Aufenthaltsnachweis durch Dritte)
    """
    checked = 0

    def check_accounts(iterator, rel_type):
        nonlocal checked
        for acc in iterator:
            if checked >= max_accounts:
                break
            checked += 1
            try:
                for post in acc.get_posts():
                    if username.lower() in [u.lower() for u in post.tagged_users]:
                        entry = {
                            'from_user':  acc.username,
                            'relation':   rel_type,
                            'shortcode':  post.shortcode,
                            'timestamp':  post.date_utc.isoformat(),
                            'image_url':  post.url,
                            'location':   None,
                        }
                        if post.location and post.location.lat:
                            entry['location'] = {
                                'name': post.location.name,
                                'lat':  round(float(post.location.lat), 5),
                                'lon':  round(float(post.location.lng), 5),
                            }
                            result['locations'].append({
                                **entry['location'],
                                'timestamp': post.date_utc.isoformat(),
                                'source': f'tagged_by_{rel_type}',
                                'by': acc.username,
                            })
                        result['network']['tagged_by'][acc.username] = entry
                        break  # pro Account maximal 1 Fund
            except Exception:
                pass

    try:
        check_accounts(profile.get_followers(), 'follower')
    except Exception as e:
        log.debug('Follower-Zugriff: %s', e)

    try:
        check_accounts(profile.get_followees(), 'following')
    except Exception as e:
        log.debug('Following-Zugriff: %s', e)


def _compute_location_stats(result):
    """Berechnet Top-Standorte und wahrscheinlichsten Aufenthaltsort."""
    locs = [l for l in result['locations'] if l.get('lat')]
    if not locs:
        return

    # Häufigste Standortnamen
    name_counts = Counter(l['name'] for l in locs if l.get('name'))
    result['top_locations'] = [
        {'name': name, 'count': cnt}
        for name, cnt in name_counts.most_common(10)
    ]

    # Cluster: Punkte innerhalb 5km gruppieren
    clusters = []
    for loc in locs:
        placed = False
        for cluster in clusters:
            if _haversine_km(loc['lat'], loc['lon'],
                             cluster['lat'], cluster['lon']) < 5:
                cluster['points'].append(loc)
                # Clustermittelpunkt aktualisieren
                cluster['lat'] = sum(p['lat'] for p in cluster['points']) / len(cluster['points'])
                cluster['lon'] = sum(p['lon'] for p in cluster['points']) / len(cluster['points'])
                placed = True
                break
        if not placed:
            clusters.append({'lat': loc['lat'], 'lon': loc['lon'],
                             'points': [loc]})

    clusters.sort(key=lambda c: len(c['points']), reverse=True)

    if clusters:
        best = clusters[0]
        # Neuesten Post aus diesem Cluster finden
        newest = max(best['points'], key=lambda p: p.get('timestamp', ''))
        result['probable_location'] = {
            'lat':         round(best['lat'], 5),
            'lon':         round(best['lon'], 5),
            'count':       len(best['points']),
            'total_locs':  len(locs),
            'confidence':  round(len(best['points']) / len(locs) * 100),
            'last_seen':   newest.get('timestamp', ''),
            'near_places': list({p['name'] for p in best['points'] if p.get('name')})[:5],
        }

    result['all_clusters'] = [
        {
            'lat':    round(c['lat'], 5),
            'lon':    round(c['lon'], 5),
            'count':  len(c['points']),
            'places': list({p['name'] for p in c['points'] if p.get('name')})[:3],
        }
        for c in clusters[:10]
    ]


# ---------------------------------------------------------------------------
# DB-Integration (wird von Flask aufgerufen)
# ---------------------------------------------------------------------------

def start_analysis(username: str, max_posts: int = 40,
                   analyze_network: bool = True,
                   detect_faces: bool = False) -> int:
    """Startet Analyse im Hintergrund-Thread, gibt Analysis-ID zurück."""
    with sqlite3.connect(DB_PATH) as conn:
        cur = conn.execute(
            '''INSERT INTO instagram_analyses
               (username, started_at, status, result_json)
               VALUES (?, ?, 'running', '{}')''',
            (username, datetime.utcnow().isoformat()),
        )
        analysis_id = cur.lastrowid

    def run():
        try:
            result = analyze_profile(username, max_posts=max_posts,
                                     analyze_network=analyze_network,
                                     detect_faces=detect_faces)
            status = 'error' if 'error' in result else 'done'
        except Exception as e:
            result = {'error': str(e)}
            status = 'error'
        with sqlite3.connect(DB_PATH) as conn:
            conn.execute(
                'UPDATE instagram_analyses SET status=?, result_json=? WHERE id=?',
                (status, json.dumps(result, default=str), analysis_id),
            )
        log.info('Instagram-Analyse %s abgeschlossen: %s', analysis_id, status)

    threading.Thread(target=run, daemon=True).start()
    return analysis_id
