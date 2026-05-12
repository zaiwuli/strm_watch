FROM python:3.9-slim
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    LANG=C.UTF-8 \
    SOURCE_DIR=/源文件夹 \
    TARGET_DIR=/目标文件夹
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt && mkdir -p /源文件夹 /目标文件夹
COPY strm_watch.py .
EXPOSE 8501
CMD ["streamlit", "run", "strm_watch.py", "--server.port=8501", "--server.address=0.0.0.0", "--server.headless=true"]
