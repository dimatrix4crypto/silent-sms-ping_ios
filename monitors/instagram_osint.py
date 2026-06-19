"""
Instagram OSINT-Modul — Standortbestimmung & Soziale Analyse.

Analysiert:
  1. Eigene Posts: Geotagged Standorte → Aufenthaltskarte
  2. Zeitliche Muster: Wann ist die Person aktiv → Zeitzone / Rhythmus
  3. Soziales Netzwerk: Wer taggt die Person, wen taggt sie → Umfeld
  4. Follower/Following Crossposts: In welchen Fotos taucht die Person auf
  5. Gesichtserkennung: DeepFace-Matching gegen Profilbild des Targets

Setup:
  pip install instaloader deepface opencv-python-headless numpy

Für private Profile oder mehr Daten:
  Setze: IG_USERNAME=dein_ig_user  IG_PASSWORD=dein_ig_passwort
"""

import json
import logging
import os
import sqlite3
import tempfile
import threading
import urllib.request
from collections import Counter
from datetime import datetime
from math import atan2, cos, radians, sin, sqrt

import cv2
import instaloader
import numpy as np

log = logging.getLogger(__name__)

DB_PATH = os.path.join(os.path.dirname(__file__), '..', 'tracking.db')

_CASCADE_PATH = cv2.data.haarcascades + 'haarcascade_frontalface_default.xml'
_face_cascade = cv2.CascadeClassifier(_CASCADE_PATH)


def _haversine_km(lat1, lon1, lat2, lon2) -> float:
    R = 6371.0
    φ1, φ2 = radians(lat1), radians(lat2)
    dφ = radians(lat2 - lat1)
    dλ = radians(lon2 - lon1)
    a = sin(dφ / 2) ** 2 + cos(φ1) * cos(φ2) * sin(dλ / 2) ** 2
    return R * 2 * atan2(sqrt(a), sqrt(1 - a))


def _download_image_to_tmp(url: str) -> str | None:
    try:
        req = urllib.request.Request(url, headers={'User-Agent': 'Mozilla/5.0'})
        with urllib.request.urlopen(req, timeout=10) as resp:
            data = resp.read()
        suffix = '.jpg'
        fd, path = tempfile.mkstemp(suffix=suffix)
        with os.fdopen(fd, 'wb') as f:
            f.write(data)
        return path
    except Exception as e:
        log.debug('Bild-Download fehlgeschlagen: %s', e)
        return None


def _count_faces_opencv(image_url: str) -> int:
    try:
        req = urllib.request.Request(image_url, headers={'User-Agent': 'Mozilla/5.0'})
        with urllib.request.urlopen(req, timeout=8) as resp:
            data = np.frombuffer(resp.read(), dtype=np.uint8)
        img = cv2.imdecode(data, cv2.IMREAD_GRAYSCALE)
        if img is None:
            return 0
        faces = _face_cascade.detectMultiScale(
            img, scaleFactor=1.1, minNeighbors=5, minSize=(30, 30)
        )
        return len(faces)
    except Exception:
        return 0


def _recognize_face(reference_path: str, candidate_url: str,
                    threshold: float = 0.6) -> dict:
    from deepface import DeepFace

    candidate_path = _download_image_to_tmp(candidate_url)
    if not candidate_path:
        return {'match': False, 'distance': None, 'faces_found': 0}

    try:
        result = DeepFace.verify(
            img1_path=reference_path,
            img2_path=candidate_path,
            model_name='ArcFace',
            detector_backend='opencv',
            distance_metric='cosine',
            enforce_detection=False,
            silent=True,
        )
        return {
            'match':       result.get('verified', False),
            'distance':    round(result.get('distance', 1.0), 4),
            'faces_found': 1,
        }
    except Exception as e:
        log.debug('DeepFace Fehler: %s', e)
        return {'match': False, 'distance': None, 'faces_found': 0}
    finally:
        try:
            os.unlink(candidate_path)
        except Exception:
            pass


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


def analyze_profile(username: str, max_posts: int = 40,
                    analyze_network: bool = True,
                    detect_faces: bool = True,
                    reference_image_path: str | None = None) -> dict:
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
        'face_matches': [],
        'activity_hours': [],
        'top_locations': [],
        'probable_location': None,
    }

    _ref_path = reference_image_path
    _ref_cleanup = False
    if detect_faces and _ref_path is None and profile.profile_pic_url:
        _ref_path = _download_image_to_tmp(profile.profile_pic_url)
        _ref_cleanup = True
        if _ref_path:
            log.info('Referenzgesicht aus Profilbild geladen: %s', _ref_path)

    if profile.is_private:
        result['warning'] = 'Privates Profil — nur öffentliche Metadaten verfügbar'
        return result

    hour_counts = Counter()
    all_hashtags = Counter()

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

        if post.location and post.location.lat:
            loc = {
                'name': post.location.name,
                'lat':  round(float(post.location.lat), 5),
                'lon':  round(float(post.location.lng), 5),
            }
            post_data['location'] = loc
            result['locations'].append({**loc, 'timestamp': post_data['timestamp'],
                                        'source': 'own_post'})

        for tagged in post.tagged_users:
            result['network']['tags_others'][tagged] = \
                result['network']['tags_others'].get(tagged, 0) + 1

        if detect_faces and post.url:
            if _ref_path:
                match_result = _recognize_face(_ref_path, post.url)
                post_data['faces']       = match_result['faces_found']
                post_data['face_match']  = match_result['match']
                post_data['face_dist']   = match_result['distance']
                result['faces_detected'] += match_result['faces_found']
                if match_result['match']:
                    result['face_matches'].append({
                        'shortcode': post.shortcode,
                        'timestamp': post_data['timestamp'],
                        'location':  post_data['location'],
                        'distance':  match_result['distance'],
                        'image_url': post.url,
                    })
            else:
                n = _count_faces_opencv(post.url)
                post_data['faces'] = n
                result['faces_detected'] += n

        hour_counts[post.date_utc.hour] += 1
        all_hashtags.update(post.caption_hashtags)
        result['posts'].append(post_data)

    result['activity_hours'] = [hour_counts.get(h, 0) for h in range(24)]
    result['top_hashtags']   = dict(all_hashtags.most_common(15))

    if analyze_network and not profile.is_private:
        _analyze_network(L, username, profile, result,
                         max_accounts=20, ref_path=_ref_path)

    _compute_location_stats(result)

    if _ref_cleanup and _ref_path:
        try:
            os.unlink(_ref_path)
        except Exception:
            pass

    return result


def _analyze_network(L, username, profile, result, max_accounts=20, ref_path=None):
    checked = 0

    def check_accounts(iterator, rel_type):
        nonlocal checked
        for acc in iterator:
            if checked >= max_accounts:
                break
            checked += 1
            try:
                for post in acc.get_posts():
                    tag_match  = username.lower() in [u.lower() for u in post.tagged_users]
                    face_match = False
                    face_dist  = None

                    if not tag_match and ref_path and post.url:
                        fr = _recognize_face(ref_path, post.url)
                        face_match = fr['match']
                        face_dist  = fr['distance']

                    if not (tag_match or face_match):
                        continue

                    entry = {
                        'from_user':   acc.username,
                        'relation':    rel_type,
                        'shortcode':   post.shortcode,
                        'timestamp':   post.date_utc.isoformat(),
                        'image_url':   post.url,
                        'location':    None,
                        'found_by':    'tag' if tag_match else 'face_recognition',
                        'face_dist':   face_dist,
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
                            'source':    f'found_by_{rel_type}',
                            'by':        acc.username,
                            'method':    entry['found_by'],
                        })
                    result['network']['tagged_by'][acc.username] = entry
                    break
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
    locs = [l for l in result['locations'] if l.get('lat')]
    if not locs:
        return

    name_counts = Counter(l['name'] for l in locs if l.get('name'))
    result['top_locations'] = [
        {'name': name, 'count': cnt}
        for name, cnt in name_counts.most_common(10)
    ]

    clusters = []
    for loc in locs:
        placed = False
        for cluster in clusters:
            if _haversine_km(loc['lat'], loc['lon'],
                             cluster['lat'], cluster['lon']) < 5:
                cluster['points'].append(loc)
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


def start_analysis(username: str, max_posts: int = 40,
                   analyze_network: bool = True,
                   detect_faces: bool = False,
                   reference_image_path: str | None = None) -> int:
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
                                     detect_faces=detect_faces,
                                     reference_image_path=reference_image_path)
            status = 'error' if 'error' in result else 'done'
        except Exception as e:
            result = {'error': str(e)}
            status = 'error'
        finally:
            if reference_image_path:
                try:
                    os.unlink(reference_image_path)
                except Exception:
                    pass
        with sqlite3.connect(DB_PATH) as conn:
            conn.execute(
                'UPDATE instagram_analyses SET status=?, result_json=? WHERE id=?',
                (status, json.dumps(result, default=str), analysis_id),
            )
        log.info('Instagram-Analyse %s abgeschlossen: %s', analysis_id, status)

    threading.Thread(target=run, daemon=True).start()
    return analysis_id
