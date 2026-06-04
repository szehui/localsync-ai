# ==========================================
# Stage 1: Build frontend
# ==========================================
FROM node:20-alpine AS frontend-build
WORKDIR /app/frontend
COPY frontend/package*.json ./
RUN npm ci
COPY frontend/ ./
RUN npm run build

# ==========================================
# Stage 2: Runtime (nginx + FastAPI via uvicorn)
# ==========================================
FROM python:3.11-slim
WORKDIR /app

# Install nginx
RUN apt-get update && apt-get install -y --no-install-recommends nginx && \
    rm -rf /var/lib/apt/lists/*

# Install Python dependencies
COPY backend/requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy backend code
COPY backend/ ./backend/

# Copy frontend build
COPY --from=frontend-build /app/frontend/dist /var/www/html

# Nginx config
COPY nginx.conf /etc/nginx/nginx.conf

# Create data directory for SQLite
RUN mkdir -p /app/data

# Start script
COPY start.sh .
RUN chmod +x start.sh

EXPOSE 80

CMD ["./start.sh"]
