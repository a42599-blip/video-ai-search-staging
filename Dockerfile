FROM python:3.12-slim

WORKDIR /app

RUN apt-get update && apt-get install -y --no-install-recommends \
    ffmpeg curl ca-certificates git && rm -rf /var/lib/apt/lists/*

# Install CLIP + torch (CPU only, smaller)
RUN pip install --no-cache-dir torch torchvision --index-url https://download.pytorch.org/whl/cpu
RUN pip install --no-cache-dir git+https://github.com/openai/CLIP.git

# App dependencies  
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# App code
COPY server.py .
COPY index.html .
COPY search_module.py .
COPY crawlers/ ./crawlers/
RUN mkdir -p downloads

CMD uvicorn server:app --host 0.0.0.0 --port ${PORT:-8080}
