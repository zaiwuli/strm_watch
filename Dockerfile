FROM python:3.9-slim

# 安装时区数据（群晖需要）
RUN apt-get update && apt-get install -y --no-install-recommends tzdata && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# 复制依赖文件并安装
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# 复制主程序
COPY strm_watch.py .

# 环境变量配置
ENV SOURCE_DIR=/source \
    TARGET_DIR=/target \
    OLD_KEYWORD=/CloudNAS/CloudDrive/115open/ \
    NEW_MOUNT_PREFIX=/115/ \
    MS_URL= \
    MS_API_KEY= \
    ENABLE_URL_ENCODE=True \
    TZ=Asia/Shanghai \
    PYTHONUNBUFFERED=1

# 创建日志目录
RUN mkdir -p /app && chmod 755 /app

# 使用 -u 标志让 Python 输出不缓冲，便于群晖日志查看
CMD ["python", "-u", "strm_watch.py"]