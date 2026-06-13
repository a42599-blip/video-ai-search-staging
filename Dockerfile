FROM python:3.11-slim
WORKDIR /app
RUN pip install --no-cache-dir torch>=2.2.0 --index-url https://download.pytorch.org/whl/cpu
RUN pip install --no-cache-dir open-clip-torch>=2.29.0 fastapi>=0.111.0 uvicorn>=0.29.0 httpx>=0.27.0 yt-dlp python-multipart>=0.0.9 Pillow>=10.0.0
# 預先下載 CLIP 模型（建置時就載好，啟動不用等）
RUN python -c "import open_clip; open_clip.create_model_and_transforms('ViT-B-32', pretrained='openai')"
COPY server.py .
COPY index.html .
COPY search_module.py .
CMD uvicorn server:app --host 0.0.0.0 --port ${PORT:-8080}
