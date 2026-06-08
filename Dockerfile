FROM python:3.12-slim
WORKDIR /app
RUN pip install --no-cache-dir fastapi uvicorn httpx yt-dlp python-multipart Pillow
COPY server.py .
COPY index.html .
COPY search_module.py .
CMD uvicorn server:app --host 0.0.0.0 --port ${PORT:-8080}
