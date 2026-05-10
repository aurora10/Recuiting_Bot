# syntax=docker/dockerfile:1
FROM python:3.12-slim

WORKDIR /app

# Install system dependencies (reportlab/Pillow native libs)
RUN apt-get update && \
    apt-get install -y --no-install-recommends \
        libjpeg62-turbo \
        libfreetype6 \
        tzdata \
    && rm -rf /var/lib/apt/lists/*

# Install Python dependencies
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy application code and fonts
COPY recruitment_userbot.py .
COPY DejaVuSans.ttf .
COPY DejaVuSans-Bold.ttf .

# Data directory for persistent volumes (session, db, media, dossiers)
# Defaults to /app/data in container; overridable via DATA_DIR env var
RUN mkdir -p /app/data/media /app/data/dossiers

# Bot runs in /app, persistent data stored in DATA_DIR
ENTRYPOINT ["python", "recruitment_userbot.py"]