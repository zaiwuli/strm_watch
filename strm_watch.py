import os
import sys
import time
import json
import http.client
import logging
from datetime import datetime
from urllib.parse import urlparse, quote
from watchdog.observers import Observer
from watchdog.events import FileSystemEventHandler

# ================= 日志配置 =================
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s',
    handlers=[
        logging.StreamHandler(sys.stdout),
        logging.FileHandler('/app/strm_watch.log', encoding='utf-8')
    ]
)
logger = logging.getLogger(__name__)

# ================= 配置区域 =================
SOURCE_DIR = os.getenv("SOURCE_DIR", "/source")
TARGET_DIR = os.getenv("TARGET_DIR", "/target")
OLD_KEYWORD = os.getenv("OLD_KEYWORD", "/CloudNAS/CloudDrive/115open/")
NEW_MOUNT_PREFIX = os.getenv("NEW_MOUNT_PREFIX", "/115/")
MS_URL = os.getenv("MS_URL", "")
MS_API_KEY = os.getenv("MS_API_KEY", "")
ENABLE_URL_ENCODE = os.getenv("ENABLE_URL_ENCODE", "True").lower() == "true"
STR_API_PATH = "api/v1/cloudStorage/strm302"
RETRY_ATTEMPTS = 3
RETRY_DELAY = 2
# ===========================================

def validate_config():
    """验证必要的配置"""
    if not os.path.isdir(SOURCE_DIR):
        logger.error(f"源文件夹不存在或无权限访问: {SOURCE_DIR}")
        return False
    if not os.path.isdir(TARGET_DIR):
        try:
            os.makedirs(TARGET_DIR, exist_ok=True)
            logger.info(f"已创建目标文件夹: {TARGET_DIR}")
        except Exception as e:
            logger.error(f"无法创建目标文件夹 {TARGET_DIR}: {e}")
            return False
    logger.info(f"配置验证成功 - 源: {SOURCE_DIR}, 目标: {TARGET_DIR}")
    return True

def send_ms_notification(title, content):
    """发送 MediaServer 通知"""
    if not MS_URL or not MS_API_KEY:
        logger.debug("通知功能未启用 (MS_URL 或 MS_API_KEY 为空)")
        return
    
    for attempt in range(1, RETRY_ATTEMPTS + 1):
        try:
            url = f"{MS_URL.rstrip('/')}/api/v1/message/openSend"
            p = urlparse(url)
            payload = json.dumps({"title": title, "content": content})
            headers = {'Content-Type': 'application/json', 'apiKey': MS_API_KEY}
            
            conn = (http.client.HTTPSConnection(p.netloc, timeout=10) if p.scheme == 'https' 
                    else http.client.HTTPConnection(p.netloc, timeout=10))
            conn.request("POST", p.path, body=payload, headers=headers)
            resp = conn.getresponse()
            resp.read()
            conn.close()
            logger.info(f"通知已发送: {title}")
            return
        except Exception as e:
            logger.warning(f"通知发送失败 (尝试 {attempt}/{RETRY_ATTEMPTS}): {e}")
            if attempt < RETRY_ATTEMPTS:
                time.sleep(RETRY_DELAY)
            else:
                logger.error(f"通知最终失败: {title}")

def process_file(src_path):
    """处理单个 .strm 文件"""
    if not src_path.endswith(".strm"):
        return False
    
    file_name = os.path.basename(src_path)
    
    try:
        # 检查文件是否可读
        if not os.access(src_path, os.R_OK):
            logger.warning(f"无权限读取文件: {src_path}")
            return False
        
        # 计算相对路径
        rel_path = os.path.relpath(src_path, SOURCE_DIR)
        target_path = os.path.join(TARGET_DIR, rel_path)
        
        # 创建目标目录
        target_dir = os.path.dirname(target_path)
        os.makedirs(target_dir, exist_ok=True)
        
        # 读取源文件内容
        with open(src_path, 'r', encoding='utf-8') as f:
            content = f.read().strip()
        
        # 验证内容
        if not content:
            logger.warning(f"文件内容为空: {src_path}")
            return False
        
        # 检查是否包含待替换的关键词
        if OLD_KEYWORD not in content:
            logger.debug(f"文件不包含待替换关键词 '{OLD_KEYWORD}': {file_name}")
            return False
        
        # 提取路径部分
        path_part = content.split(OLD_KEYWORD)[-1].lstrip('/')
        
        # 构建新路径
        final_path = (NEW_MOUNT_PREFIX.rstrip('/') + '/' + path_part).replace('\\', '/')
        
        # URL 编码
        if ENABLE_URL_ENCODE:
            final_path = quote(final_path, safe='/')
        
        # 生成新内容
        new_content = f"{MS_URL.rstrip('/')}/{STR_API_PATH}?apiKey={MS_API_KEY}&pickCode=&path={final_path}"
        
        # 写入目标文件
        with open(target_path, 'w', encoding='utf-8') as f:
            f.write(new_content)
        
        logger.info(f"✓ 成功处理: {file_name}")
        return True
        
    except PermissionError as e:
        logger.error(f"权限错误 - {file_name}: {e}")
        return False
    except IOError as e:
        logger.error(f"I/O 错误 - {file_name}: {e}")
        return False
    except Exception as e:
        logger.error(f"未知错误处理 {file_name}: {e}", exc_info=True)
        return False

class WatcherHandler(FileSystemEventHandler):
    """文件系统事件处理器"""
    
    def on_created(self, event):
        if event.is_directory:
            return
        try:
            if process_file(event.src_path):
                send_ms_notification("🆕 实时同步通知", f"入库：{os.path.basename(event.src_path)}")
        except Exception as e:
            logger.error(f"on_created 处理异常: {e}", exc_info=True)
    
    def on_modified(self, event):
        if event.is_directory:
            return
        try:
            if process_file(event.src_path):
                send_ms_notification("🔄 内容更正通知", f"更正：{os.path.basename(event.src_path)}")
        except Exception as e:
            logger.error(f"on_modified 处理异常: {e}", exc_info=True)

if __name__ == "__main__":
    logger.info("=" * 60)
    logger.info("STRM Watch 监控服务启动")
    logger.info(f"启动时间: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    logger.info("=" * 60)
    
    # 验证配置
    if not validate_config():
        logger.critical("配置验证失败，服务退出")
        sys.exit(1)
    
    # 初始扫描
    logger.info("开始初始扫描源文件夹...")
    initial_count = 0
    error_count = 0
    
    try:
        for root, _, files in os.walk(SOURCE_DIR):
            for f in files:
                src_file = os.path.join(root, f)
                try:
                    if process_file(src_file):
                        initial_count += 1
                except Exception as e:
                    logger.error(f"初始扫描处理失败 {src_file}: {e}")
                    error_count += 1
    except Exception as e:
        logger.error(f"初始扫描异常: {e}", exc_info=True)
    
    logger.info(f"初始扫描完成 - 成功: {initial_count}, 失败: {error_count}")
    
    if initial_count > 0:
        send_ms_notification("📊 启动同步报告", f"初始扫描已处理 {initial_count} 个文件")
    
    # 启动监控
    logger.info("启动文件系统监控...")
    event_handler = WatcherHandler()
    observer = Observer()
    observer.schedule(event_handler, SOURCE_DIR, recursive=True)
    observer.start()
    
    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        logger.info("收到中断信号，正在关闭...")
        observer.stop()
    except Exception as e:
        logger.error(f"监控异常: {e}", exc_info=True)
        observer.stop()
    finally:
        observer.join()
        logger.info("监控服务已停止")
        logger.info("=" * 60)