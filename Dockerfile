FROM python:3.11-slim as builder
WORKDIR /build
COPY requirements.txt .
RUN pip install --no-cache-dir --prefix=/install -r requirements.txt

FROM python:3.11-slim
WORKDIR /app
RUN apt-get update && apt-get install -y --no-install-recommends ffmpeg && rm -rf /var/lib/apt/lists/*
COPY --from=builder /install /usr/local
COPY . .
RUN mkdir -p data/uploads data/characters data/images data/frames data/audio data/videos
RUN useradd -m -r appuser && chown -R appuser:appuser /app
USER appuser
# Containers bind to 0.0.0.0 for port publishing, so require the app's
# authentication gate unless an operator explicitly overrides it.
ENV AUTH_REQUIRED=true
HEALTHCHECK --interval=30s --timeout=10s --start-period=5s --retries=3 \
    CMD python -c "import requests,sys; r=requests.get('http://localhost:8000/', timeout=5); sys.exit(0 if r.status_code == 200 else 1)" || exit 1
EXPOSE 8000
# Serve via ui_patch:app so the pro UI (fonts + animations + mobile) is injected.
# Falls back to plain app:app if you prefer the bare UI.
CMD ["uvicorn", "ui_patch:app", "--host", "0.0.0.0", "--port", "8000", "--workers", "2"]
