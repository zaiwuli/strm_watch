FROM python:3.9-slim
ENV PYTHONUNBUFFERED=1     PYTHONDONTWRITEBYTECODE=1     SOURCE_DIR=/source     TARGET_DIR=/target     CONFIG_PATH=/config/settings.json

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt &&     mkdir -p /source /target /config

COPY strm_watch.py .
EXPOSE 8501
CMD ["python", "strm_watch.py"]