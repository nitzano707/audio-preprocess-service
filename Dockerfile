FROM python:3.11-slim

RUN apt-get update && apt-get install -y --no-install-recommends ffmpeg && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app.py .
RUN mkdir -p /app/uploads

ENV UPLOAD_DIR=/app/uploads
ENV BASE_URL=https://audio-preprocess-service.onrender.com
ENV MAX_MB=25
ENV AUTO_DELETE_AFTER_SEC=3600

EXPOSE 8000
CMD ["uvicorn", "app:app", "--host", "0.0.0.0", "--port", "8000"]
