import os
import json
import time
import sqlite3
import bcrypt
import cloudinary
import cloudinary.uploader
import cloudinary.api
from datetime import datetime, timedelta
from functools import wraps
from flask import (Flask, render_template, request, jsonify, session,
                   redirect, url_for, g)
from flask_socketio import SocketIO, emit, join_room, leave_room
from dotenv import load_dotenv
import threading

load_dotenv()

app = Flask(__name__)
app.secret_key = os.environ.get('FLASK_SECRET', 'soulsync-secret-key-2024')
app.config['PERMANENT_SESSION_LIFETIME'] = timedelta(minutes=30)
app.config['SESSION_COOKIE_SAMESITE'] = 'Lax'

socketio = SocketIO(app, cors_allowed_origins="*", async_mode='eventlet')

# Cloudinary config
cloudinary.config(
    cloud_name=os.environ.get('CLOUDINARY_CLOUD_NAME'),
    api_key=os.environ.get('CLOUDINARY_API_KEY'),
    api_secret=os.environ.get('CLOUDINARY_API_SECRET')
)

# Admin credentials (bcrypt hashed)
ADMIN_USERNAME = 'mypersonalspotify'
ADMIN_PASSWORD_HASH = bcrypt.hashpw(b'rajeshasdeveloper', bcrypt.gensalt())

# In-memory state
login_attempts = {}  # ip -> {count, lockout_until}
pair_state = {
    'me_connected': False,
    'her_connected': False,
    'status': 'disconnected',  # disconnected, waiting, connected
    'chat': [],
    'current_song': None,
    'is_playing': False,
    'position': 0,
}
connected_users = {}  # sid -> user_id
surprise_shown = {}   # song_id -> {me: bool, her: bool}

DB_PATH = os.environ.get('DB_PATH', '/data/database.db') if os.path.exists('/data') else 'database.db'

# ─────────────────────────── DATABASE ───────────────────────────

def get_db():
    if 'db' not in g:
        g.db = sqlite3.connect(DB_PATH)
        g.db.row_factory = sqlite3.Row
        g.db.execute("PRAGMA journal_mode=WAL")
    return g.db

@app.teardown_appcontext
def close_db(e=None):
    db = g.pop('db', None)
    if db:
        db.close()

def init_db():
    with app.app_context():
        db = sqlite3.connect(DB_PATH)
        db.row_factory = sqlite3.Row
        db.executescript("""
            CREATE TABLE IF NOT EXISTS songs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL UNIQUE,
                artist TEXT NOT NULL,
                audio_url TEXT NOT NULL,
                image_url TEXT NOT NULL,
                duration INTEGER DEFAULT 0,
                cloudinary_audio_id TEXT,
                cloudinary_image_id TEXT,
                sort_order INTEGER DEFAULT 0,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            );
            CREATE TABLE IF NOT EXISTS playlists (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL,
                cover_url TEXT DEFAULT '',
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            );
            CREATE TABLE IF NOT EXISTS playlist_songs (
                playlist_id INTEGER,
                song_id INTEGER,
                sort_order INTEGER DEFAULT 0,
                PRIMARY KEY (playlist_id, song_id),
                FOREIGN KEY (playlist_id) REFERENCES playlists(id) ON DELETE CASCADE,
                FOREIGN KEY (song_id) REFERENCES songs(id) ON DELETE CASCADE
            );
            CREATE TABLE IF NOT EXISTS likes (
                user_id TEXT NOT NULL,
                song_id INTEGER NOT NULL,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                PRIMARY KEY (user_id, song_id),
                FOREIGN KEY (song_id) REFERENCES songs(id) ON DELETE CASCADE
            );
            CREATE TABLE IF NOT EXISTS recently_played (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id TEXT NOT NULL,
                song_id INTEGER NOT NULL,
                played_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY (song_id) REFERENCES songs(id) ON DELETE CASCADE
            );
            CREATE TABLE IF NOT EXISTS search_history (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id TEXT NOT NULL,
                query TEXT NOT NULL,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            );
            CREATE TABLE IF NOT EXISTS upcoming_songs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL,
                search_count INTEGER DEFAULT 1,
                searched_by TEXT NOT NULL,
                last_searched_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                notified_me INTEGER DEFAULT 0,
                notified_her INTEGER DEFAULT 0
            );
            CREATE TABLE IF NOT EXISTS playback_state (
                user_id TEXT PRIMARY KEY,
                song_id INTEGER,
                position INTEGER DEFAULT 0,
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            );
            CREATE TABLE IF NOT EXISTS settings (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS song_positions (
                song_id INTEGER PRIMARY KEY,
                position INTEGER NOT NULL
            );
        """)
        # Default settings
        defaults = [
            ('theme_mode', 'auto'),
            ('force_theme', '0'),
            ('me_name', 'Me'),
            ('her_name', 'Her'),
            ('show_welcome_name', '1'),
        ]
        for k, v in defaults:
            db.execute("INSERT OR IGNORE INTO settings (key,value) VALUES (?,?)", (k, v))
        db.commit()
        db.close()

# ─────────────────────────── HELPERS ───────────────────────────

def require_admin(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        if not session.get('admin'):
            return jsonify({'error': 'Unauthorized'}), 401
        session.modified = True
        return f(*args, **kwargs)
    return decorated

def get_setting(key, default=''):
    db = get_db()
    row = db.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
    return row['value'] if row else default

def set_setting(key, value):
    db = get_db()
    db.execute("INSERT OR REPLACE INTO settings (key,value) VALUES (?,?)", (key, value))
    db.commit()

def song_to_dict(row, user_id='me'):
    db = get_db()
    liked = db.execute("SELECT 1 FROM likes WHERE user_id=? AND song_id=?",
                       (user_id, row['id'])).fetchone()
    now = datetime.utcnow()
    created = datetime.fromisoformat(str(row['created_at'])) if row['created_at'] else now
    is_new = (now - created).total_seconds() < 7 * 86400
    return {
        'id': row['id'],
        'name': row['name'],
        'artist': row['artist'],
        'audio_url': row['audio_url'],
        'image_url': row['image_url'],
        'duration': row['duration'],
        'sort_order': row['sort_order'],
        'created_at': str(row['created_at']),
        'is_new': is_new,
        'liked': bool(liked),
    }

def check_upcoming_match(song_name):
    """When a song is uploaded, check if it matches upcoming requests and notify."""
    db = get_db()
    # Fuzzy match: check if uploaded song name contains upcoming name or vice versa
    upcomings = db.execute("SELECT * FROM upcoming_songs").fetchall()
    for u in upcomings:
        uname = u['name'].lower().strip()
        sname = song_name.lower().strip()
        if uname in sname or sname in uname or uname == sname:
            # Get the song we just uploaded
            song = db.execute("SELECT * FROM songs WHERE LOWER(name)=LOWER(?)", (song_name,)).fetchone()
            if song:
                searched_by_list = json.loads(u['searched_by']) if u['searched_by'].startswith('[') else [u['searched_by']]
                notify_data = {
                    'song_id': song['id'],
                    'song_name': song['name'],
                    'image_url': song['image_url'],
                }
                if 'me' in searched_by_list:
                    db.execute("UPDATE upcoming_songs SET notified_me=0 WHERE id=?", (u['id'],))
                    socketio.emit('surprise_song', {**notify_data, 'user': 'me'}, room='me')
                if 'her' in searched_by_list:
                    db.execute("UPDATE upcoming_songs SET notified_her=0 WHERE id=?", (u['id'],))
                    socketio.emit('surprise_song', {**notify_data, 'user': 'her'}, room='her')
                db.commit()

# ─────────────────────────── AUTH ROUTES ───────────────────────────

@app.route('/admin/login', methods=['GET', 'POST'])
def admin_login():
    if request.method == 'GET':
        return render_template('admin_login.html')

    ip = request.remote_addr
    now = time.time()
    info = login_attempts.get(ip, {'count': 0, 'lockout_until': 0})

    if info['lockout_until'] > now:
        remaining = int(info['lockout_until'] - now)
        return jsonify({'error': f'Too many attempts. Try again in {remaining}s'}), 429

    data = request.json or {}
    username = data.get('username', '')
    password = data.get('password', '').encode()

    if username == ADMIN_USERNAME and bcrypt.checkpw(password, ADMIN_PASSWORD_HASH):
        session.permanent = True
        session['admin'] = True
        login_attempts.pop(ip, None)
        return jsonify({'success': True})
    else:
        info['count'] = info.get('count', 0) + 1
        if info['count'] >= 5:
            info['lockout_until'] = now + 900  # 15 min
            info['count'] = 0
        login_attempts[ip] = info
        return jsonify({'error': 'Invalid credentials', 'attempts': info['count']}), 401

@app.route('/admin/logout', methods=['POST'])
def admin_logout():
    session.pop('admin', None)
    return jsonify({'success': True})

@app.route('/admin/check')
def admin_check():
    return jsonify({'admin': bool(session.get('admin'))})

# ─────────────────────────── ADMIN PAGE ───────────────────────────

@app.route('/admin')
def admin_page():
    if not session.get('admin'):
        return redirect('/admin/login')
    return render_template('admin.html')

# ─────────────────────────── SONGS API ───────────────────────────

@app.route('/api/songs')
def get_songs():
    user_id = request.args.get('user', 'me')
    db = get_db()
    songs = db.execute("SELECT * FROM songs ORDER BY sort_order ASC, id ASC").fetchall()
    return jsonify([song_to_dict(s, user_id) for s in songs])

@app.route('/api/songs/upload', methods=['POST'])
@require_admin
def upload_song():
    db = get_db()
    name = request.form.get('name', '').strip()
    artist = request.form.get('artist', '').strip()

    if not name or not artist:
        return jsonify({'error': 'Name and artist required'}), 400

    existing = db.execute("SELECT id FROM songs WHERE LOWER(name)=LOWER(?)", (name,)).fetchone()
    if existing:
        return jsonify({'error': 'Song with this name already exists'}), 409

    audio_file = request.files.get('audio')
    image_file = request.files.get('image')

    if not audio_file:
        return jsonify({'error': 'Audio file required'}), 400

    try:
        # Upload audio
        audio_result = cloudinary.uploader.upload(
            audio_file,
            resource_type='video',
            folder='soulsync/audio',
            format='mp3'
        )
        audio_url = audio_result['secure_url']
        audio_id = audio_result['public_id']
        duration = int(audio_result.get('duration', 0))

        # Upload image
        if image_file:
            img_result = cloudinary.uploader.upload(
                image_file,
                folder='soulsync/images',
                transformation=[{'width': 500, 'height': 500, 'crop': 'fill'}]
            )
            image_url = img_result['secure_url']
            image_id = img_result['public_id']
        else:
            image_url = '/static/icons/default.png'
            image_id = ''

        max_order = db.execute("SELECT MAX(sort_order) as m FROM songs").fetchone()['m'] or 0
        cursor = db.execute(
            "INSERT INTO songs (name,artist,audio_url,image_url,duration,cloudinary_audio_id,cloudinary_image_id,sort_order) VALUES (?,?,?,?,?,?,?,?)",
            (name, artist, audio_url, image_url, duration, audio_id, image_id, max_order + 1)
        )
        db.commit()
        song_id = cursor.lastrowid
        song = db.execute("SELECT * FROM songs WHERE id=?", (song_id,)).fetchone()

        # Check upcoming
        check_upcoming_match(name)

        song_data = song_to_dict(song)
        socketio.emit('song_added', song_data)
        return jsonify({'success': True, 'song': song_data})
    except Exception as e:
        return jsonify({'error': str(e)}), 500

@app.route('/api/songs/<int:song_id>', methods=['PUT'])
@require_admin
def update_song(song_id):
    db = get_db()
    data = request.json or {}
    name = data.get('name', '').strip()
    artist = data.get('artist', '').strip()
    if not name or not artist:
        return jsonify({'error': 'Name and artist required'}), 400
    existing = db.execute("SELECT id FROM songs WHERE LOWER(name)=LOWER(?) AND id!=?", (name, song_id)).fetchone()
    if existing:
        return jsonify({'error': 'Song name already exists'}), 409
    db.execute("UPDATE songs SET name=?, artist=? WHERE id=?", (name, artist, song_id))
    db.commit()
    song = db.execute("SELECT * FROM songs WHERE id=?", (song_id,)).fetchone()
    song_data = song_to_dict(song)
    socketio.emit('song_updated', song_data)
    return jsonify({'success': True, 'song': song_data})

@app.route('/api/songs/delete', methods=['POST'])
@require_admin
def delete_songs():
    db = get_db()
    ids = request.json.get('ids', [])
    for sid in ids:
        song = db.execute("SELECT * FROM songs WHERE id=?", (sid,)).fetchone()
        if song:
            # Delete from cloudinary
            try:
                if song['cloudinary_audio_id']:
                    cloudinary.uploader.destroy(song['cloudinary_audio_id'], resource_type='video')
                if song['cloudinary_image_id']:
                    cloudinary.uploader.destroy(song['cloudinary_image_id'])
            except Exception:
                pass
            db.execute("DELETE FROM songs WHERE id=?", (sid,))
    db.commit()
    socketio.emit('songs_deleted', {'ids': ids})
    return jsonify({'success': True})

@app.route('/api/songs/reorder', methods=['POST'])
@require_admin
def reorder_songs():
    db = get_db()
    order = request.json.get('order', [])  # list of {id, sort_order}
    for item in order:
        db.execute("UPDATE songs SET sort_order=? WHERE id=?", (item['sort_order'], item['id']))
    db.commit()
    return jsonify({'success': True})

# ─────────────────────────── LIKES ───────────────────────────

@app.route('/api/likes/<user_id>', methods=['GET'])
def get_likes(user_id):
    db = get_db()
    rows = db.execute("""
        SELECT s.* FROM likes l JOIN songs s ON s.id=l.song_id
        WHERE l.user_id=? ORDER BY l.created_at DESC
    """, (user_id,)).fetchall()
    return jsonify([song_to_dict(r, user_id) for r in rows])

@app.route('/api/likes/<user_id>/<int:song_id>', methods=['POST', 'DELETE'])
def toggle_like(user_id, song_id):
    db = get_db()
    if request.method == 'POST':
        db.execute("INSERT OR IGNORE INTO likes (user_id,song_id) VALUES (?,?)", (user_id, song_id))
        liked = True
    else:
        db.execute("DELETE FROM likes WHERE user_id=? AND song_id=?", (user_id, song_id))
        liked = False
    db.commit()
    socketio.emit('like_updated', {'user_id': user_id, 'song_id': song_id, 'liked': liked})
    return jsonify({'success': True, 'liked': liked})

# ─────────────────────────── RECENTLY PLAYED ───────────────────────────

@app.route('/api/recently-played/<user_id>', methods=['GET'])
def get_recently_played(user_id):
    db = get_db()
    rows = db.execute("""
        SELECT s.*, MAX(r.played_at) as last_played
        FROM recently_played r JOIN songs s ON s.id=r.song_id
        WHERE r.user_id=?
        GROUP BY s.id
        ORDER BY last_played DESC
        LIMIT 20
    """, (user_id,)).fetchall()
    result = []
    for r in rows:
        d = song_to_dict(r, user_id)
        d['last_played'] = r['last_played']
        result.append(d)
    return jsonify(result)

@app.route('/api/recently-played/<user_id>/<int:song_id>', methods=['POST'])
def add_recently_played(user_id, song_id):
    db = get_db()
    db.execute("INSERT INTO recently_played (user_id,song_id) VALUES (?,?)", (user_id, song_id))
    db.commit()
    return jsonify({'success': True})

# ─────────────────────────── PLAYBACK STATE ───────────────────────────

@app.route('/api/playback-state/<user_id>', methods=['GET'])
def get_playback_state(user_id):
    db = get_db()
    row = db.execute("SELECT * FROM playback_state WHERE user_id=?", (user_id,)).fetchone()
    if row:
        return jsonify({'song_id': row['song_id'], 'position': row['position']})
    return jsonify({'song_id': None, 'position': 0})

@app.route('/api/playback-state/<user_id>', methods=['POST'])
def save_playback_state(user_id):
    db = get_db()
    data = request.json or {}
    song_id = data.get('song_id')
    position = data.get('position', 0)
    db.execute("INSERT OR REPLACE INTO playback_state (user_id,song_id,position,updated_at) VALUES (?,?,?,CURRENT_TIMESTAMP)",
               (user_id, song_id, position))
    db.commit()
    return jsonify({'success': True})

# ─────────────────────────── SEARCH ───────────────────────────

@app.route('/api/search')
def search_songs():
    q = request.args.get('q', '').strip()
    user_id = request.args.get('user', 'me')
    db = get_db()
    if not q:
        return jsonify([])
    songs = db.execute("""
        SELECT * FROM songs
        WHERE LOWER(name) LIKE LOWER(?) OR LOWER(artist) LIKE LOWER(?)
        ORDER BY sort_order ASC
    """, (f'%{q}%', f'%{q}%')).fetchall()
    results = [song_to_dict(s, user_id) for s in songs]
    # If no results, add to upcoming
    if not results and len(q) > 1:
        existing = db.execute("SELECT * FROM upcoming_songs WHERE LOWER(name)=LOWER(?)", (q,)).fetchone()
        if existing:
            searched_by = json.loads(existing['searched_by']) if existing['searched_by'].startswith('[') else [existing['searched_by']]
            if user_id not in searched_by:
                searched_by.append(user_id)
            db.execute("UPDATE upcoming_songs SET search_count=search_count+1, last_searched_at=CURRENT_TIMESTAMP, searched_by=? WHERE id=?",
                       (json.dumps(searched_by), existing['id']))
        else:
            db.execute("INSERT INTO upcoming_songs (name,searched_by) VALUES (?,?)",
                       (q, json.dumps([user_id])))
        db.commit()
    return jsonify(results)

@app.route('/api/search-history/<user_id>', methods=['GET'])
def get_search_history(user_id):
    db = get_db()
    rows = db.execute("""
        SELECT DISTINCT query, MAX(created_at) as created_at
        FROM search_history WHERE user_id=?
        GROUP BY query ORDER BY created_at DESC LIMIT 20
    """, (user_id,)).fetchall()
    return jsonify([{'query': r['query'], 'created_at': r['created_at']} for r in rows])

@app.route('/api/search-history/<user_id>', methods=['POST'])
def add_search_history(user_id):
    db = get_db()
    query = (request.json or {}).get('query', '').strip()
    if query:
        db.execute("INSERT INTO search_history (user_id,query) VALUES (?,?)", (user_id, query))
        db.commit()
    return jsonify({'success': True})

@app.route('/api/search-history/<user_id>', methods=['DELETE'])
def clear_search_history(user_id):
    db = get_db()
    query = request.args.get('query')
    if query:
        db.execute("DELETE FROM search_history WHERE user_id=? AND query=?", (user_id, query))
    else:
        db.execute("DELETE FROM search_history WHERE user_id=?", (user_id,))
    db.commit()
    return jsonify({'success': True})

# ─────────────────────────── UPCOMING SONGS (ADMIN) ───────────────────────────

@app.route('/api/upcoming-songs')
@require_admin
def get_upcoming_songs():
    db = get_db()
    rows = db.execute("SELECT * FROM upcoming_songs ORDER BY search_count DESC, last_searched_at DESC").fetchall()
    result = []
    for r in rows:
        searched_by = json.loads(r['searched_by']) if r['searched_by'].startswith('[') else [r['searched_by']]
        result.append({
            'id': r['id'],
            'name': r['name'],
            'search_count': r['search_count'],
            'searched_by': searched_by,
            'last_searched_at': r['last_searched_at'],
        })
    return jsonify(result)

@app.route('/api/upcoming-songs/<int:uid>', methods=['DELETE'])
@require_admin
def delete_upcoming(uid):
    db = get_db()
    db.execute("DELETE FROM upcoming_songs WHERE id=?", (uid,))
    db.commit()
    return jsonify({'success': True})

# ─────────────────────────── PLAYLISTS ───────────────────────────

@app.route('/api/playlists')
def get_playlists():
    user_id = request.args.get('user', 'me')
    db = get_db()
    playlists = db.execute("SELECT * FROM playlists ORDER BY created_at DESC").fetchall()
    result = []
    for p in playlists:
        songs = db.execute("""
            SELECT s.* FROM playlist_songs ps JOIN songs s ON s.id=ps.song_id
            WHERE ps.playlist_id=? ORDER BY ps.sort_order ASC
        """, (p['id'],)).fetchall()
        cover = songs[0]['image_url'] if songs else ''
        result.append({
            'id': p['id'],
            'name': p['name'],
            'cover_url': cover or p['cover_url'],
            'song_count': len(songs),
            'songs': [song_to_dict(s, user_id) for s in songs],
            'created_at': str(p['created_at']),
        })
    return jsonify(result)

@app.route('/api/playlists', methods=['POST'])
@require_admin
def create_playlist():
    db = get_db()
    name = (request.json or {}).get('name', '').strip()
    if not name:
        return jsonify({'error': 'Name required'}), 400
    cursor = db.execute("INSERT INTO playlists (name) VALUES (?)", (name,))
    db.commit()
    pid = cursor.lastrowid
    pl = {'id': pid, 'name': name, 'cover_url': '', 'song_count': 0, 'songs': []}
    socketio.emit('playlist_created', pl)
    return jsonify({'success': True, 'playlist': pl})

@app.route('/api/playlists/<int:pid>', methods=['DELETE'])
@require_admin
def delete_playlist(pid):
    db = get_db()
    db.execute("DELETE FROM playlists WHERE id=?", (pid,))
    db.commit()
    socketio.emit('playlist_deleted', {'id': pid})
    return jsonify({'success': True})

@app.route('/api/playlists/<int:pid>/songs', methods=['POST'])
def add_song_to_playlist(pid):
    db = get_db()
    song_id = (request.json or {}).get('song_id')
    max_order = db.execute("SELECT MAX(sort_order) as m FROM playlist_songs WHERE playlist_id=?", (pid,)).fetchone()['m'] or 0
    db.execute("INSERT OR IGNORE INTO playlist_songs (playlist_id,song_id,sort_order) VALUES (?,?,?)",
               (pid, song_id, max_order + 1))
    db.commit()
    socketio.emit('playlist_updated', {'id': pid})
    return jsonify({'success': True})

@app.route('/api/playlists/<int:pid>/songs/<int:sid>', methods=['DELETE'])
def remove_song_from_playlist(pid, sid):
    db = get_db()
    db.execute("DELETE FROM playlist_songs WHERE playlist_id=? AND song_id=?", (pid, sid))
    db.commit()
    socketio.emit('playlist_updated', {'id': pid})
    return jsonify({'success': True})

# ─────────────────────────── SETTINGS ───────────────────────────

@app.route('/api/settings', methods=['GET'])
def get_settings():
    db = get_db()
    rows = db.execute("SELECT * FROM settings").fetchall()
    return jsonify({r['key']: r['value'] for r in rows})

@app.route('/api/settings', methods=['POST'])
@require_admin
def update_settings():
    db = get_db()
    data = request.json or {}
    for k, v in data.items():
        db.execute("INSERT OR REPLACE INTO settings (key,value) VALUES (?,?)", (k, str(v)))
    db.commit()
    socketio.emit('settings_updated', data)
    return jsonify({'success': True})

# ─────────────────────────── MAIN PAGE ───────────────────────────

@app.route('/')
def index():
    return render_template('index.html')

# ─────────────────────────── SOCKET.IO ───────────────────────────

@socketio.on('connect')
def on_connect():
    pass

@socketio.on('join_user_room')
def on_join_user(data):
    user_id = data.get('user_id', 'me')
    connected_users[request.sid] = user_id
    join_room(user_id)
    emit('room_joined', {'room': user_id})

@socketio.on('disconnect')
def on_disconnect():
    user_id = connected_users.pop(request.sid, None)
    if user_id:
        leave_room(user_id)

# ─── PAIR SYSTEM ───

@socketio.on('pair_connect')
def on_pair_connect(data):
    user = data.get('user')
    if user == 'me':
        pair_state['me_connected'] = True
    elif user == 'her':
        pair_state['her_connected'] = True

    if pair_state['me_connected'] and pair_state['her_connected']:
        pair_state['status'] = 'connected'
        socketio.emit('pair_status', {'status': 'connected'})
    else:
        pair_state['status'] = 'waiting'
        # Notify the other user
        other = 'her' if user == 'me' else 'me'
        socketio.emit('pair_request', {'from': user}, room=other)
        socketio.emit('pair_status', {'status': 'waiting'}, room=user)

@socketio.on('pair_disconnect')
def on_pair_disconnect(data):
    user = data.get('user')
    if user == 'me':
        pair_state['me_connected'] = False
    elif user == 'her':
        pair_state['her_connected'] = False

    if not pair_state['me_connected'] and not pair_state['her_connected']:
        pair_state['status'] = 'disconnected'
        # Delete chat
        pair_state['chat'] = []
        socketio.emit('pair_status', {'status': 'disconnected'})
        socketio.emit('chat_cleared')
    else:
        pair_state['status'] = 'waiting'
        socketio.emit('pair_status', {'status': 'waiting'})

@socketio.on('pair_sync')
def on_pair_sync(data):
    """Sync playback to paired user"""
    socketio.emit('pair_playback', data)

# ─── CHAT ───

@socketio.on('chat_message')
def on_chat_message(data):
    msg = {
        'id': int(time.time() * 1000),
        'user': data.get('user'),
        'text': data.get('text', ''),
        'reply_to': data.get('reply_to'),
        'reactions': {},
        'timestamp': datetime.utcnow().isoformat(),
    }
    pair_state['chat'].append(msg)
    socketio.emit('chat_message', msg)

@socketio.on('chat_reaction')
def on_chat_reaction(data):
    msg_id = data.get('msg_id')
    emoji = data.get('emoji')
    user = data.get('user')
    for msg in pair_state['chat']:
        if msg['id'] == msg_id:
            if emoji not in msg['reactions']:
                msg['reactions'][emoji] = []
            if user in msg['reactions'][emoji]:
                msg['reactions'][emoji].remove(user)
            else:
                msg['reactions'][emoji].append(user)
            break
    socketio.emit('chat_reaction_update', {'msg_id': msg_id, 'reactions': next(
        (m['reactions'] for m in pair_state['chat'] if m['id'] == msg_id), {})})

@socketio.on('get_chat')
def on_get_chat(data):
    emit('chat_history', {'messages': pair_state['chat']})

@socketio.on('get_pair_status')
def on_get_pair_status(data):
    user = data.get('user')
    if pair_state['me_connected'] and pair_state['her_connected']:
        status = 'connected'
    elif (user == 'me' and pair_state['me_connected']) or (user == 'her' and pair_state['her_connected']):
        status = 'waiting'
    else:
        status = 'disconnected'
    emit('pair_status', {'status': status})

if __name__ == '__main__':
    init_db()
    socketio.run(app, debug=True)
else:
    init_db()
