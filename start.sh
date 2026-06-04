#!/bin/bash
set -e

# Start FastAPI in background
cd /app/backend
uvicorn app.main:app --host 127.0.0.1 --port 8000 &

# Start nginx in foreground
nginx -g "daemon off;"
