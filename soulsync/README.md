# 💫 SoulSync — Personal Music Streaming App

A Spotify-inspired personal music streaming app built for two people.

## Features
- 🎵 Full music player with queue, shuffle, repeat
- 💘 Pair listening system (real-time sync via WebSocket)
- 💬 Pair chat with emoji reactions and reply threading
- 🔍 Real-time search with "Upcoming Songs" requests
- ❤️ Separate likes for Me & Her
- 🕐 Recently played (per user)
- 📋 Playlists
- 🎉 Surprise popup when requested songs are uploaded
- 🌙 Dark/Light/Auto theme
- 🔒 Admin panel with bcrypt auth

## Admin Credentials
- Username: `mypersonalspotify`
- Password: `rajeshasdeveloper`
- URL: `/admin`

## Setup

### 1. Clone & Install
```bash
pip install -r requirements.txt
```

### 2. Environment Variables
Copy `.env.example` to `.env` and fill in:
```
CLOUDINARY_CLOUD_NAME=...
CLOUDINARY_API_KEY=...
CLOUDINARY_API_SECRET=...
FLASK_SECRET=some-random-string
```

### 3. Run Locally
```bash
python app.py
```

### 4. Deploy to Render.com
1. Push to GitHub
2. Create new Web Service on Render
3. Set environment variables in Render dashboard
4. Start command: `gunicorn --worker-class eventlet -w 1 app:app`
5. Add a Disk (1 GB) mounted at `/data` for SQLite persistence

### Cloudinary Setup (Free Tier)
1. Sign up at cloudinary.com
2. Get Cloud Name, API Key, API Secret from dashboard
3. Add to environment variables

## File Structure
```
app.py                  # Flask backend
requirements.txt
render.yaml             # Render deployment config
static/
  css/style.css         # All styles
  icons/default.png     # Default album art
templates/
  index.html            # Main app (user view)
  admin.html            # Admin panel
  admin_login.html      # Admin login
```

## Tech Stack
- **Backend**: Python Flask + Flask-SocketIO
- **Database**: SQLite (built-in, no external DB needed)
- **Storage**: Cloudinary (free tier) for MP3s and images
- **Frontend**: Vanilla HTML/CSS/JS
- **Real-time**: Socket.IO
- **Auth**: bcrypt password hashing
