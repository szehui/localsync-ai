# LocalSync AI — Music discovery and playlist orchestration for Navidrome

## Quick Start

```bash
docker-compose up --build
# Open http://localhost:4535
```

## Development

### Backend
```bash
cd backend
python -m venv venv
source venv/bin/activate
pip install -r requirements.txt
uvicorn app.main:app --reload --port 8000
```

### Frontend
```bash
cd frontend
npm install
npm run dev
```

## Configuration

On first launch, configure your Navidrome connection:
- URL: `http://192.168.4.205:4533`
- Username: your Navidrome username
- Password: your Navidrome password

### Listening history (ListenBrainz)

ListenBrainz is the primary source for listening history and track similarity.
Last.fm is optional and only used as a fallback when ListenBrainz returns nothing.

1. Enable scrobbling to ListenBrainz in Navidrome (user profile → External
   Services → paste your ListenBrainz user token).
2. In Automation Hub, enter your ListenBrainz **username** and **user token**.
3. Press **Check** to verify — it reports the account's listen count and warns
   when the account has no history yet.

Notes:
- The API is read-only; nothing is ever submitted to ListenBrainz.
- A fresh account has no collaborative-filtering data. Until history builds,
  playlists are built from Navidrome similarity and recently-added tracks
  ("bootstrap mode"). This is expected, not an error.
- Listen to music for a few weeks and recommendations begin to personalise.

## Project Structure

```
localsync-ai/
├── backend/          # FastAPI + Python
├── frontend/         # React + Vite + Tailwind
├── docker-compose.yml
└── Dockerfile        # Multi-stage build
```
