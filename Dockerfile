FROM python:3.12-slim

WORKDIR /app

# System deps + Deno JS runtime
RUN apt-get update && apt-get install -y --no-install-recommends \
    ffmpeg curl ca-certificates git && \
    curl -fsSL https://deno.land/install.sh | DENO_INSTALL=/usr/local sh && \
    rm -rf /var/lib/apt/lists/*

ENV PATH="/usr/local/bin:${PATH}"

# CLIP + torch (CPU)
RUN pip install --no-cache-dir torch torchvision --index-url https://download.pytorch.org/whl/cpu
RUN pip install --no-cache-dir git+https://github.com/openai/CLIP.git

# App dependencies
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy app
COPY server.py .
COPY index.html .
COPY search_module.py .
RUN mkdir -p downloads

CMD uvicorn server:app --host 0.0.0.0 --port ${PORT:-8080}
