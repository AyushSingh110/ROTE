# Minimal image for the synthetic sandbox. No secrets, no credentials, no external services.
FROM python:3.11-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY rote/ ./rote/

# plans are compiled at startup, so the first boot takes roughly 50-130 seconds and the port
# does not accept connections until it finishes
ENV PYTHONUNBUFFERED=1
EXPOSE 8000

# $PORT is honoured by most platforms; 8000 locally
CMD ["sh", "-c", "python -m uvicorn rote.web.app:app --host 0.0.0.0 --port ${PORT:-8000}"]
