FROM python:3.9-slim
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    SOURCE_DIR=/源文件夹 \
    TARGET_DIR=/目标文件夹 \
    CONFIG_PATH=/config/settings.json

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt && mkdir -p /源文件夹 /目标文件夹 /config

COPY strm_watch.py .
EXPOSE 8501
HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8501', timeout=3).read(1)"
CMD ["python", "strm_watch.py"]
