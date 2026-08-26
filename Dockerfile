# Synthetic sandbox image. No secrets are baked in: the Groq credential arrives at runtime as
# an environment variable (a Hugging Face Space Secret in the public deployment).
FROM python:3.11-slim

# Hugging Face runs the container as uid 1000, so the user exists before anything is copied
RUN useradd -m -u 1000 user
USER user
ENV HOME=/home/user \
    PATH=/home/user/.local/bin:$PATH \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1
WORKDIR $HOME/app

COPY --chown=user requirements.txt .
RUN pip install --no-cache-dir --upgrade pip && pip install --no-cache-dir -r requirements.txt

COPY --chown=user rote/ ./rote/

# 7860 is the port Hugging Face routes to for a Docker Space (app_port in the README front
# matter). $PORT is honoured first so the same image runs unchanged on other platforms.
ENV PORT=7860
EXPOSE 7860

# Plans are compiled during startup and the socket does not accept connections until that
# finishes, so the first boot is slow by design rather than broken.
CMD ["sh", "-c", "python -m uvicorn rote.web.app:app --host 0.0.0.0 --port ${PORT:-7860}"]
