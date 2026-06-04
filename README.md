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

## Project Structure

```
localsync-ai/
├── backend/          # FastAPI + Python
├── frontend/         # React + Vite + Tailwind
├── docker-compose.yml
└── Dockerfile        # Multi-stage build
```
