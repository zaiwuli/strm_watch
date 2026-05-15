import asyncio
import html
import json
import logging
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from logging.handlers import RotatingFileHandler
from pathlib import Path
from threading import Event, Lock, Thread
from typing import Dict, Generator, Optional
from urllib.parse import parse_qs, quote, unquote, urlparse

from nicegui import app, ui
from watchdog.events import FileSystemEventHandler
from watchdog.observers import Observer


CONFIG_FILE = Path(os.getenv("CONFIG_PATH", "/config/settings.json"))
LOG_FILE = CONFIG_FILE.parent / "strm_watch.log"
WEBHOOK_TEMPLATE_FILE = CONFIG_FILE.parent / "webhook_template.json"


def normalize_tool_type(value: str) -> str:
    value = (value or "MS").strip().upper()
    return value if value in {"MS", "PSN"} else "MS"


class GlobalConfig:
    def __init__(self):
        self.src = os.getenv("SOURCE_DIR", "/源文件夹")
        self.tgt = os.getenv("TARGET_DIR", "/目标文件夹")
        self.old_kw = ""
        self.new_pre = "/"
        self.tool_type = normalize_tool_type(os.getenv("TOOL_TYPE", "MS"))
        self.ms_url = ""
        self.ms_key = ""
        self.url_enc = True
        self.webhook_url = os.getenv("WEBHOOK_URL", "")
        self.poll_interval = 60
        self.last_run_mode = "增量"
        self.last_monitor_mode = "监控"
        self.load()

    def load(self):
        if not CONFIG_FILE.exists() or not os.access(CONFIG_FILE, os.R_OK):
            return

        try:
            with open(CONFIG_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)
            self.old_kw = data.get("old_kw", self.old_kw)
            self.new_pre = data.get("new_pre", self.new_pre)
            self.ms_url = data.get("ms_url", self.ms_url)
            self.ms_key = data.get("ms_key", self.ms_key)
            self.url_enc = data.get("url_enc", self.url_enc)
            self.webhook_url = data.get("webhook_url", self.webhook_url)
            self.poll_interval = int(data.get("poll_interval", self.poll_interval))
            self.last_run_mode = data.get("last_run_mode", self.last_run_mode)
            self.last_monitor_mode = data.get("last_monitor_mode", self.last_monitor_mode)
        except Exception as e:
            print(f"Failed to load config: {e}")

    def save(self):
        try:
            CONFIG_FILE.parent.mkdir(parents=True, exist_ok=True)
            data = {
                "old_kw": self.old_kw,
                "new_pre": self.new_pre,
                "ms_url": self.ms_url,
                "ms_key": self.ms_key,
                "url_enc": self.url_enc,
                "webhook_url": self.webhook_url,
                "poll_interval": self.poll_interval,
                "last_run_mode": self.last_run_mode,
                "last_monitor_mode": self.last_monitor_mode,
            }
            with open(CONFIG_FILE, "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False, indent=4)
            return True
        except Exception as e:
            logger.error("配置保存失败：%s", e)
            return False


config = GlobalConfig()


class AppStatus:
    is_running = False
    run_mode = "未启动"
    monitor_mode = "未启动"
    observer: Optional[Observer] = None
    poll_thread: Optional[Thread] = None
    stop_event = Event()
    lock = Lock()
    recent_events: Dict[str, float] = {}
    debounce_seconds = 1.0
    incremental_converted: list[str] = []
    incremental_deleted: list[str] = []
    incremental_notify_timer: Optional[Thread] = None
    incremental_notify_delay = 30


def setup_logging():
    LOG_FILE.parent.mkdir(parents=True, exist_ok=True)
    logger_obj = logging.getLogger("StrmWatch")
    logger_obj.setLevel(logging.INFO)
    logger_obj.handlers.clear()
    formatter = logging.Formatter("%(asctime)s - %(levelname)s - %(message)s", datefmt="%Y-%m-%d %H:%M:%S")

    stream_handler = logging.StreamHandler(sys.stdout)
    stream_handler.setFormatter(formatter)
    logger_obj.addHandler(stream_handler)

    file_handler = RotatingFileHandler(LOG_FILE, maxBytes=2_000_000, backupCount=3, encoding="utf-8")
    file_handler.setFormatter(formatter)
    logger_obj.addHandler(file_handler)
    return logger_obj


logger = setup_logging()
logging.getLogger("nicegui").setLevel(logging.WARNING)


def read_log_tail(max_chars: int = 60000) -> str:
    try:
        if not LOG_FILE.exists():
            return "日志文件尚未创建。"
        with open(LOG_FILE, "r", encoding="utf-8", errors="replace") as f:
            content = f.read()
        return content[-max_chars:]
    except Exception as e:
        return f"读取日志失败：{e}"


def format_log_html(content: str) -> str:
    escaped = html.escape(content)
    lines = escaped.splitlines()
    newest_first = "\n".join(reversed(lines))
    return (
        '<pre style="margin:0; white-space:pre-wrap; word-break:break-word; '
        'font-family:ui-monospace,SFMono-Regular,Menlo,Consolas,monospace; '
        'font-size:12px; line-height:1.45;">'
        f"{newest_first}</pre>"
    )


@app.get("/api/logs")
def api_logs():
    return {
        "html": format_log_html(read_log_tail()),
        "is_running": AppStatus.is_running,
        "run_mode": AppStatus.run_mode,
        "monitor_mode": AppStatus.monitor_mode,
    }


def is_valid_ms_url(value: str) -> bool:
    parsed = urlparse(value)
    return parsed.scheme in {"http", "https"} and bool(parsed.netloc)


def mask_secret(value: str) -> str:
    if len(value) <= 8:
        return "***"
    return f"{value[:4]}...{value[-4:]}"


def split_path_parts(value: str) -> list[str]:
    return [part for part in value.replace("\\", "/").split("/") if part]


def common_suffix_length(left: list[str], right: list[str]) -> int:
    count = 0
    for left_part, right_part in zip(reversed(left), reversed(right)):
        if left_part != right_part:
            break
        count += 1
    return count


def infer_config_from_examples(new_strm_url: str, old_strm_path: str, tool_type: str = "MS") -> Dict[str, str]:
    parsed = urlparse(new_strm_url.strip())
    if not parsed.scheme or not parsed.netloc:
        raise ValueError("新 STRM 链接格式不正确")

    query = parse_qs(parsed.query)
    api_key = query.get("apiKey", [""])[0] if tool_type == "MS" else ""
    encoded_new_path = query.get("path", [""])[0]
    new_path = unquote(encoded_new_path).strip()
    old_path = old_strm_path.strip()

    if tool_type == "MS" and not api_key:
        raise ValueError("新 STRM 链接里没有 apiKey 参数")
    if not new_path:
        raise ValueError("新 STRM 链接里没有 path 参数")
    if not old_path:
        raise ValueError("请填写旧 STRM 原始路径")

    new_parts = split_path_parts(new_path)
    old_parts = split_path_parts(old_path)

    if len(new_parts) >= 2 and new_parts[1].lower() == "media" and "媒体库" in old_parts:
        media_index = old_parts.index("媒体库")
        return {
            "ms_url": f"{parsed.scheme}://{parsed.netloc}",
            "ms_key": api_key,
            "old_kw": "/" + "/".join(old_parts[: media_index + 1]),
            "new_pre": "/" + "/".join(new_parts[:2]),
            "decoded_path": new_path,
            "tool_type": tool_type,
            "mode": "媒体库映射",
        }

    suffix_len = common_suffix_length(new_parts, old_parts)
    if suffix_len >= 2:
        old_keyword = "/" + "/".join(old_parts[:-suffix_len])
        new_prefix = "/" + "/".join(new_parts[:-suffix_len])
        mode = "仅替换前缀"
    elif suffix_len == 0:
        old_keyword = "/" + "/".join(old_parts[:-1]) if len(old_parts) > 1 else old_path
        new_prefix = "/" + "/".join(new_parts[:-1]) if len(new_parts) > 1 else new_path
        mode = "按目录推断"
    else:
        old_keyword = "/" + "/".join(old_parts[:-suffix_len])
        new_prefix = "/" + "/".join(new_parts[:-suffix_len])
        mode = "按后缀推断"

    if not old_keyword or old_keyword == "/":
        raise ValueError("无法从旧路径推断旧路径关键字")
    if not new_prefix:
        new_prefix = "/"

    return {
        "ms_url": f"{parsed.scheme}://{parsed.netloc}",
        "ms_key": api_key,
        "old_kw": old_keyword,
        "new_pre": new_prefix,
        "decoded_path": new_path,
        "tool_type": tool_type,
        "mode": mode,
    }


def find_first_strm_content(root: str) -> tuple[Path, str]:
    root_path = Path(root)
    if not root_path.exists():
        raise ValueError(f"目录不存在：{root}")
    for path in sorted(root_path.rglob("*.strm")):
        if not path.is_file():
            continue
        if path.name.startswith("."):
            continue
        try:
            content = path.read_text(encoding="utf-8").strip()
        except UnicodeDecodeError:
            content = path.read_text(encoding="utf-8-sig", errors="replace").strip()
        if content:
            return path, content
    raise ValueError(f"目录中没有可识别的 STRM 文件：{root}")


def infer_config_from_mounted_dirs() -> Dict[str, str]:
    old_path, old_content = find_first_strm_content(config.src)
    new_path, new_content = find_first_strm_content(config.tgt)
    logger.info("自动识别示例：旧 STRM=%s，新 STRM=%s", old_path, new_path)
    return infer_config_from_examples(new_content, old_content, config.tool_type)


def default_webhook_template() -> dict:
    return {"method": "GET", "params": {"text": "{msg}"}}


def ensure_webhook_template():
    if WEBHOOK_TEMPLATE_FILE.exists():
        return
    try:
        WEBHOOK_TEMPLATE_FILE.parent.mkdir(parents=True, exist_ok=True)
        with open(WEBHOOK_TEMPLATE_FILE, "w", encoding="utf-8") as f:
            json.dump(default_webhook_template(), f, ensure_ascii=False, indent=2)
    except Exception as e:
        logger.error("创建 Webhook 模板失败：%s", e)


def load_webhook_template() -> dict:
    ensure_webhook_template()
    try:
        with open(WEBHOOK_TEMPLATE_FILE, "r", encoding="utf-8") as f:
            template = json.load(f)
        if not isinstance(template, dict):
            raise ValueError("模板根节点必须是 JSON 对象")
        return template
    except Exception as e:
        logger.error("读取 Webhook 模板失败，使用默认模板：%s", e)
        return default_webhook_template()


def _replace_webhook_placeholders(value, variables: dict[str, str]):
    if isinstance(value, dict):
        return {k: _replace_webhook_placeholders(v, variables) for k, v in value.items()}
    if isinstance(value, list):
        return [_replace_webhook_placeholders(v, variables) for v in value]
    if isinstance(value, str):
        for key, replacement in variables.items():
            value = value.replace("{" + key + "}", replacement)
        return value
    return value


def _do_send_webhook(title: str, content: str):
    webhook_url = config.webhook_url.strip()
    if not webhook_url:
        logger.info("通知跳过：未填写 Webhook URL")
        return

    now_text = time.strftime("%Y-%m-%d %H:%M:%S")
    message = f"【{title}】\n{content}"
    variables = {
        "title": title,
        "content": content,
        "msg": message,
        "tool": config.tool_type,
        "time": now_text,
    }
    last_err = ""

    for _ in range(3):
        try:
            if "@@TEXT@@" in webhook_url or "%40%40TEXT%40%40" in webhook_url:
                safe_msg = urllib.parse.quote(message)
                final_url = webhook_url.replace("%40%40TEXT%40%40", safe_msg).replace("@@TEXT@@", safe_msg)
                req = urllib.request.Request(final_url, method="GET")
            else:
                template = load_webhook_template()
                method = template.get("method", "GET").upper()
                headers = _replace_webhook_placeholders(template.get("headers", {}), variables)
                if not isinstance(headers, dict):
                    headers = {}

                target_url = _replace_webhook_placeholders(template.get("url", webhook_url), variables).strip()
                if not target_url:
                    target_url = webhook_url

                if method == "POST" and "json_body" in template:
                    body = _replace_webhook_placeholders(template["json_body"], variables)
                    data = json.dumps(body, ensure_ascii=False).encode("utf-8")
                    headers["Content-Type"] = "application/json"
                    req = urllib.request.Request(target_url, data=data, headers=headers, method="POST")
                else:
                    params_dict = _replace_webhook_placeholders(template.get("params", {}), variables)
                    params = urllib.parse.urlencode(params_dict) if isinstance(params_dict, dict) else ""
                    if method == "GET":
                        parsed = list(urllib.parse.urlparse(target_url))
                        if params:
                            parsed[4] = params if not parsed[4] else parsed[4] + "&" + params
                        req = urllib.request.Request(urllib.parse.urlunparse(parsed), method="GET", headers=headers)
                    else:
                        body = template.get("body", params_dict)
                        body = _replace_webhook_placeholders(body, variables)
                        if isinstance(body, (dict, list)):
                            data = urllib.parse.urlencode(body).encode("utf-8")
                        else:
                            data = str(body).encode("utf-8")
                        req = urllib.request.Request(
                            target_url,
                            data=data,
                            headers=headers,
                            method=method,
                        )

            with urllib.request.urlopen(req, timeout=30):
                logger.info("Webhook 通知发送成功：%s", title)
                return
        except urllib.error.HTTPError as e:
            last_err = f"HTTP {e.code}: {e.reason}"
            time.sleep(2)
        except Exception as e:
            last_err = str(e)
            time.sleep(2)

    logger.error("Webhook 通知发送失败：%s", last_err)


def send_webhook_notification(title: str, content: str, force: bool = False):
    if not config.webhook_url.strip():
        logger.info("通知跳过：未填写 Webhook URL")
        return
    logger.info("通知已加入发送队列：%s", title)
    Thread(target=_do_send_webhook, args=(title, content), daemon=True).start()


def target_path_for_source(src_path: Path) -> Path:
    rel_path = src_path.relative_to(Path(config.src).resolve())
    return Path(config.tgt).resolve() / rel_path


def format_file_tree_item(path_text: str) -> list[str]:
    parts = Path(path_text.replace("\\", "/")).parts
    if len(parts) >= 2:
        folder = str(Path(*parts[:-1])).replace("\\", "/")
        filename = parts[-1]
        return [f"--- 📁 {folder}", f"     └── {filename}"]
    return [f"--- 📄 {path_text}"]


def build_incremental_create_notification_content(converted: list[str]) -> str:
    lines = [
        "🔗 成功创建Strm文件",
    ]
    for name in converted[:10]:
        lines.extend(format_file_tree_item(name))
    if len(converted) > 10:
        lines.append(f"--- 其余 {len(converted) - 10} 个文件省略")
    return "\n".join(lines)


def build_incremental_delete_notification_content(deleted: list[str]) -> str:
    lines = [
        "🗑️ 已同步删除Strm文件",
    ]
    for name in deleted[:10]:
        lines.extend(format_file_tree_item(name))
    if len(deleted) > 10:
        lines.append(f"--- 其余 {len(deleted) - 10} 个文件省略")
    return "\n".join(lines)


def build_full_scan_notification_content(total: int, count: int) -> str:
    lines = [
        "📦 全量扫描完成",
        f"--- 📂 源目录：{config.src}",
        f"--- 📁 目标目录：{config.tgt}",
        f"--- ✅ 生成数量：{count}",
        f"--- 🔎 扫描数量：{total}",
    ]
    return "\n".join(lines)


def flush_incremental_notification():
    with AppStatus.lock:
        converted = AppStatus.incremental_converted[:]
        deleted = AppStatus.incremental_deleted[:]
        AppStatus.incremental_converted.clear()
        AppStatus.incremental_deleted.clear()
        AppStatus.incremental_notify_timer = None

    if not converted and not deleted:
        return

    logger.info("发送增量汇总通知：转换/更新=%s，删除=%s", len(converted), len(deleted))
    if converted:
        send_webhook_notification("STRM 增量创建通知", build_incremental_create_notification_content(converted))
    if deleted:
        send_webhook_notification("STRM 增量删除通知", build_incremental_delete_notification_content(deleted))


def delayed_incremental_notification():
    time.sleep(AppStatus.incremental_notify_delay)
    flush_incremental_notification()


def record_incremental_change(action: str, src_path: Path):
    try:
        display_name = str(src_path.relative_to(Path(config.src).resolve())).replace("\\", "/")
    except ValueError:
        display_name = src_path.name
    with AppStatus.lock:
        if action == "deleted":
            AppStatus.incremental_deleted.append(display_name)
        else:
            AppStatus.incremental_converted.append(display_name)
        if AppStatus.incremental_notify_timer is None or not AppStatus.incremental_notify_timer.is_alive():
            AppStatus.incremental_notify_timer = Thread(target=delayed_incremental_notification, daemon=True)
            AppStatus.incremental_notify_timer.start()
            logger.info("增量汇总通知计时开始：%s 秒后发送", AppStatus.incremental_notify_delay)


def process_file_logic(src_path: Path) -> bool:
    if src_path.suffix.lower() != ".strm" or not config.old_kw:
        return False
    if not config.ms_url:
        logger.warning("跳过处理：服务地址为空")
        return False
    if config.tool_type == "MS" and not config.ms_key:
        logger.warning("跳过处理：MS API Key 为空")
        return False

    try:
        logger.info("开始处理：%s", src_path)
        target_path = target_path_for_source(src_path)
        target_path.parent.mkdir(parents=True, exist_ok=True)

        with open(src_path, "r", encoding="utf-8") as f:
            content = f.read().strip()

        if config.old_kw not in content:
            logger.info("未匹配旧路径关键字，跳过：%s", src_path.name)
            return False

        path_part = content.split(config.old_kw, 1)[1].lstrip("/")
        final_media_path = (config.new_pre.rstrip("/") + "/" + path_part).replace("\\", "/")
        if config.url_enc:
            final_media_path = quote(final_media_path, safe="/")
        if config.tool_type == "PSN":
            final_media_path = final_media_path.lstrip("/")

        if config.tool_type == "PSN":
            new_content = f"{config.ms_url.rstrip('/')}/my-server-api/api/getVideo302UrlByPath?path={final_media_path}"
        else:
            api_key = quote(config.ms_key, safe="")
            new_content = (
                f"{config.ms_url.rstrip('/')}/api/v1/cloudStorage/strm302"
                f"?apiKey={api_key}&pickCode=&path={final_media_path}"
            )

        tmp_path = target_path.with_name(f".{target_path.name}.tmp")
        with open(tmp_path, "w", encoding="utf-8") as f:
            f.write(new_content)
        tmp_path.replace(target_path)
        logger.info("转换完成：%s -> %s", src_path.name, target_path)
        return True
    except Exception as e:
        logger.error("处理失败 %s：%s", src_path.name, e)
        return False


def delete_target_for_source(src_path: Path) -> bool:
    if src_path.suffix.lower() != ".strm":
        return False
    try:
        target_path = target_path_for_source(src_path)
        if target_path.exists():
            target_path.unlink()
            logger.info("删除同步完成：%s -> %s", src_path.name, target_path)
            return True
        logger.info("删除同步跳过：目标文件不存在 %s", target_path)
        return False
    except Exception as e:
        logger.error("删除同步失败 %s：%s", src_path.name, e)
        return False


def strm_generator() -> Generator[Path, None, None]:
    try:
        for path in Path(config.src).resolve().rglob("*"):
            if path.is_file() and path.suffix.lower() == ".strm":
                yield path
    except Exception as e:
        logger.error("扫描目录失败：%s", e)


async def run_full_scan():
    logger.info("全量任务开始：扫描并转换所有 STRM。源目录=%s，目标目录=%s", config.src, config.tgt)
    count, total = 0, 0
    try:
        for path in strm_generator():
            total += 1
            if await asyncio.to_thread(process_file_logic, path):
                count += 1
            if total % 50 == 0:
                await asyncio.sleep(0.01)

        logger.info("全量任务完成：扫描=%s，转换=%s", total, count)
        if count > 0:
            send_webhook_notification("STRM 全量扫描通知", build_full_scan_notification_content(total, count))
    except Exception as e:
        logger.error("全量任务异常：%s", e)


def validate_config() -> Optional[str]:
    if not config.old_kw or not config.new_pre:
        return "请填写旧路径关键字和新路径前缀"
    if not config.ms_url:
        return "请填写服务地址"
    if config.tool_type == "MS" and not config.ms_key:
        return "MS 模式请填写 API Key"
    if not is_valid_ms_url(config.ms_url):
        return "服务地址必须以 http:// 或 https:// 开头"
    if not os.path.exists(config.src):
        return f"源目录不存在：{config.src}"
    return None


def stop_runtime():
    logger.info("正在停止当前任务")
    observer = None
    poll_thread = None
    with AppStatus.lock:
        AppStatus.stop_event.set()
        if AppStatus.observer:
            observer = AppStatus.observer
            AppStatus.observer = None
        if AppStatus.poll_thread and AppStatus.poll_thread.is_alive():
            poll_thread = AppStatus.poll_thread
        AppStatus.poll_thread = None
        AppStatus.is_running = False
        AppStatus.run_mode = "未启动"
        AppStatus.monitor_mode = "未启动"

    if observer:
        try:
            logger.info("正在停止监控观察器")
            observer.stop()
            observer.join(timeout=2)
            if observer.is_alive():
                logger.warning("监控观察器停止超时，已放弃等待")
            else:
                logger.info("监控观察器已停止")
        except Exception as e:
            logger.error("停止监控观察器失败：%s", e)

    if poll_thread:
        logger.info("正在等待轮询线程退出")
        poll_thread.join(timeout=2)
        if poll_thread.is_alive():
            logger.warning("轮询线程停止超时，已放弃等待")
        else:
            logger.info("轮询线程已停止")

    with AppStatus.lock:
        logger.info("当前任务已停止")
    flush_incremental_notification()


def start_watchdog_incremental():
    logger.info("正在启动增量监控")
    observer = Observer()
    observer.schedule(WatchHandler(), config.src, recursive=True)
    try:
        observer.start()
    except Exception:
        logger.exception("监控启动失败，准备回退到轮询模式")
        try:
            observer.stop()
            observer.join(timeout=1)
        except Exception:
            pass
        start_polling_incremental(max(5, int(config.poll_interval or 60)))
        return
    with AppStatus.lock:
        AppStatus.stop_event.clear()
        AppStatus.recent_events.clear()
        AppStatus.observer = observer
        AppStatus.is_running = True
        AppStatus.run_mode = "增量"
        AppStatus.monitor_mode = "监控"
    logger.info("增量监听已启动：方式=监控，源目录=%s，目标目录=%s", config.src, config.tgt)


def file_signature(path: Path) -> tuple[float, int]:
    stat = path.stat()
    return stat.st_mtime, stat.st_size


def poll_loop(interval: int):
    logger.info("轮询线程已启动：间隔=%s 秒", interval)
    seen: Dict[str, tuple[tuple[float, int], Path]] = {}
    for path in strm_generator():
        try:
            seen[str(path.resolve())] = (file_signature(path), path.resolve())
        except OSError:
            continue
    logger.info("轮询初始快照完成：已记录 %s 个 STRM 文件", len(seen))

    while not AppStatus.stop_event.wait(interval):
        logger.info("轮询检查开始")
        current_keys = set()
        changed = 0
        deleted = 0
        for path in strm_generator():
            try:
                key = str(path.resolve())
                signature = file_signature(path)
            except OSError:
                continue
            current_keys.add(key)
            old_entry = seen.get(key)
            if old_entry is None or old_entry[0] != signature:
                seen[key] = (signature, path.resolve())
                changed += 1
                if process_file_logic(path):
                    record_incremental_change("converted", path)
        for missing_key in list(set(seen) - current_keys):
            missing_path = seen.pop(missing_key)[1]
            if delete_target_for_source(missing_path):
                deleted += 1
                record_incremental_change("deleted", missing_path)
        logger.info("轮询检查完成：新增/修改 %s 个，删除 %s 个", changed, deleted)
    logger.info("轮询线程已退出")


def start_polling_incremental(interval: int):
    with AppStatus.lock:
        AppStatus.stop_event.clear()
        AppStatus.is_running = True
        AppStatus.run_mode = "增量"
        AppStatus.monitor_mode = "轮询"
        AppStatus.poll_thread = Thread(target=poll_loop, args=(interval,), daemon=True)
        AppStatus.poll_thread.start()
        logger.info("增量监听已启动：方式=轮询，间隔=%s 秒", interval)


class WatchHandler(FileSystemEventHandler):
    def _handle(self, event, action: str):
        if event.is_directory:
            return

        src_path = Path(event.src_path)
        if src_path.suffix.lower() != ".strm":
            return

        now = time.monotonic()
        event_key = str(src_path.resolve())
        last_seen = AppStatus.recent_events.get(event_key, 0)
        if now - last_seen < AppStatus.debounce_seconds:
            logger.info("监听事件已防抖跳过：%s", src_path.name)
            return

        AppStatus.recent_events[event_key] = now
        if len(AppStatus.recent_events) > 1000:
            expired_before = now - 60
            AppStatus.recent_events = {
                key: value for key, value in AppStatus.recent_events.items() if value >= expired_before
            }

        logger.info("监听事件：%s %s", action, os.path.basename(event.src_path))
        if process_file_logic(src_path):
            record_incremental_change("converted", src_path)

    def on_created(self, event):
        self._handle(event, "新增")

    def on_modified(self, event):
        self._handle(event, "修改")

    def on_deleted(self, event):
        if event.is_directory:
            return
        src_path = Path(event.src_path)
        if src_path.suffix.lower() != ".strm":
            return
        logger.info("监听事件：删除 %s", os.path.basename(event.src_path))
        if delete_target_for_source(src_path):
            record_incremental_change("deleted", src_path)


@ui.page("/")
def main_page():
    ui.query("body").style("background-color: #f8fafc")
    run_options = {"mode": config.last_run_mode, "monitor": config.last_monitor_mode}
    with ui.header().classes("items-center justify-between bg-slate-900 text-white shadow-md"):
        ui.label("📡 StrmWatch").classes("text-xl font-bold ml-4")
        with ui.row().classes("items-center mr-4 gap-3"):
            ui.label("🟢 运行中" if AppStatus.is_running else "🔴 未启动").classes("text-base font-bold").props(
                'id="status-label"'
            )
            ui.label(f"模式：{AppStatus.run_mode}").classes("text-sm text-slate-200").props('id="mode-label"')

    with ui.column().classes("w-full max-w-6xl mx-auto p-4 gap-4"):
        with ui.card().classes("w-full p-5 shadow-sm border-none bg-white"):
            ui.label("⚙️ 配置").classes("text-lg font-bold text-slate-700 mb-4")

            with ui.row().classes("w-full gap-4 mb-4"):
                ui.input("源目录").bind_value(config, "src").classes("flex-1").props("disable")
                ui.input("目标目录").bind_value(config, "tgt").classes("flex-1").props("disable")

            with ui.expansion("小工具", icon="auto_awesome").classes("w-full mb-4"):
                def handle_infer_config():
                    try:
                        logger.info("用户点击识别并填充")
                        logger.info("开始从挂载目录识别 STRM 配置：工具=%s", config.tool_type)
                        inferred = infer_config_from_mounted_dirs()
                        config.ms_url = inferred["ms_url"]
                        config.ms_key = inferred["ms_key"]
                        config.old_kw = inferred["old_kw"]
                        config.new_pre = inferred["new_pre"]
                        ui.notify(f"已识别并填充配置：{inferred['mode']}", type="positive")
                        logger.info(
                            "已识别配置：工具=%s，模式=%s，服务=%s，API Key=%s，旧路径关键字=%s，新路径前缀=%s",
                            inferred["tool_type"],
                            inferred["mode"],
                            inferred["ms_url"],
                            mask_secret(inferred["ms_key"]) if inferred["ms_key"] else "无",
                            inferred["old_kw"],
                            inferred["new_pre"],
                        )
                    except ValueError as e:
                        logger.warning("识别失败：%s", e)
                        ui.notify(str(e), type="warning")
                    finally:
                        ui.run_javascript("window.refreshStrmWatchUi && window.refreshStrmWatchUi()")

                ui.button("✨ 识别并填充", on_click=handle_infer_config).classes("bg-cyan-600 text-white").props(
                    "unelevated"
                )

            with ui.row().classes("w-full gap-4 mb-4"):
                ui.input("旧路径关键字", validation={"不能为空": lambda v: len(v) > 0}).bind_value(
                    config, "old_kw"
                ).classes("flex-1")
                ui.input("新路径前缀", validation={"不能为空": lambda v: len(v) > 0}).bind_value(
                    config, "new_pre"
                ).classes("flex-1")

            with ui.row().classes("w-full gap-4 mb-4"):
                service_placeholder = "http://192.168.31.2:999" if config.tool_type == "PSN" else "https://ms.example.com:55123"
                ui.input("服务地址").bind_value(config, "ms_url").classes("flex-1").props(f'placeholder="{service_placeholder}"')
                if config.tool_type == "MS":
                    ui.input("API Key", password=True).bind_value(config, "ms_key").classes("flex-1").props(
                        'placeholder="仅 MS 需要"'
                    )

            with ui.column().classes("w-full gap-1 mb-4"):
                ui.input("Webhook URL（留空则不发送通知）").bind_value(config, "webhook_url").classes("w-full")
                ui.label(
                    f"填写 Webhook 后自动通知；模板：{WEBHOOK_TEMPLATE_FILE}，变量：{{msg}}、{{title}}、{{content}}、{{tool}}、{{time}}"
                ).classes("text-xs text-slate-500")

            with ui.row().classes("w-full gap-6 items-center"):
                ui.checkbox("URL 编码").bind_value(config, "url_enc")
                ui.space()

                def handle_save():
                    try:
                        logger.info("用户点击保存配置")
                        config.last_run_mode = run_options["mode"]
                        config.last_monitor_mode = run_options["monitor"]
                        if config.save():
                            ui.notify("配置已保存", type="positive")
                            logger.info("配置已保存：%s", CONFIG_FILE)
                        else:
                            logger.error("保存配置失败")
                            ui.notify("保存失败，请检查配置目录权限", type="negative")
                    finally:
                        ui.run_javascript("window.refreshStrmWatchUi && window.refreshStrmWatchUi()")

                def handle_test_notification():
                    try:
                        logger.info("用户点击测试通知")
                        logger.info("开始测试通知")
                        if not config.webhook_url.strip():
                            logger.warning("测试通知失败：Webhook URL 为空")
                            ui.notify("请先填写 Webhook URL", type="warning")
                            return
                        send_webhook_notification("测试通知", "Webhook 连接正常", True)
                        ui.notify("测试通知已发送到队列", type="info")
                    finally:
                        ui.run_javascript("window.refreshStrmWatchUi && window.refreshStrmWatchUi()")

                ui.button("🧪 测试通知", on_click=handle_test_notification).classes("bg-amber-500").props(
                    "unelevated outline"
                )
                ui.button("💾 保存配置", on_click=handle_save).classes("bg-green-600 text-white").props("unelevated")

        with ui.card().classes("w-full p-5 shadow-sm border-none bg-white"):
            ui.label("🧭 运行控制").classes("text-lg font-bold text-slate-700 mb-3")
            with ui.row().classes("w-full gap-3 items-end"):
                with ui.column().classes("gap-1"):
                    ui.label("执行模式").classes("text-xs text-slate-500")
                    ui.toggle(["全量", "增量"], value=config.last_run_mode).bind_value(run_options, "mode").classes(
                        "w-56"
                    ).props("unelevated spread")
                with ui.column().classes("gap-1"):
                    ui.label("增量方式").classes("text-xs text-slate-500")
                    ui.toggle(["监控", "轮询"], value=config.last_monitor_mode).bind_value(
                        run_options, "monitor"
                    ).classes("w-56").props("unelevated spread")
                ui.number("轮询间隔（秒）", min=5, max=86400, step=5).bind_value(config, "poll_interval").classes("w-40")
                ui.space()

                async def handle_start():
                    try:
                        logger.info("用户点击启动：模式=%s，增量方式=%s", run_options["mode"], run_options["monitor"])
                        config.last_run_mode = run_options["mode"]
                        config.last_monitor_mode = run_options["monitor"]
                        validation_error = validate_config()
                        if validation_error:
                            logger.warning("启动失败：%s", validation_error)
                            ui.notify(validation_error, type="warning")
                            return
                        if not config.save():
                            logger.error("启动失败：配置保存失败")
                            ui.notify("配置保存失败，未启动", type="negative")
                            return

                        stop_runtime()
                        if run_options["mode"] == "全量":
                            AppStatus.is_running = True
                            AppStatus.run_mode = "全量"
                            AppStatus.monitor_mode = "一次性扫描"
                            try:
                                await run_full_scan()
                            finally:
                                AppStatus.is_running = False
                                AppStatus.run_mode = "未启动"
                                AppStatus.monitor_mode = "未启动"
                                logger.info("全量任务结束，状态已恢复为未启动")
                        else:
                            interval = max(5, int(config.poll_interval or 60))
                            if run_options["monitor"] == "轮询":
                                start_polling_incremental(interval)
                            else:
                                start_watchdog_incremental()
                        ui.notify("启动命令已执行", type="positive")
                    except Exception as e:
                        AppStatus.is_running = False
                        AppStatus.run_mode = "未启动"
                        AppStatus.monitor_mode = "未启动"
                        logger.exception("启动失败：%s", e)
                        ui.notify(f"启动失败：{e}", type="negative")
                    finally:
                        ui.run_javascript("window.refreshStrmWatchUi && window.refreshStrmWatchUi()")

                def handle_stop():
                    try:
                        logger.info("用户点击停止")
                        stop_runtime()
                        ui.notify("已停止", type="info")
                    finally:
                        ui.run_javascript("window.refreshStrmWatchUi && window.refreshStrmWatchUi()")

                ui.button("▶ 启动", on_click=handle_start).classes("w-28 bg-green-600 text-white").props("unelevated")
                ui.button("停止", on_click=handle_stop).classes("w-28 bg-slate-600 text-white").props("unelevated")

        with ui.expansion("📜 运行日志", icon="article").classes("w-full bg-white p-3 rounded shadow-sm"):
            with ui.row().classes("w-full items-center justify-between"):
                ui.label("最新日志显示在最上方").classes("text-sm text-slate-600")
                ui.label(f"日志文件：{LOG_FILE}").classes("text-xs text-slate-500")
            log_area = ui.html(format_log_html(read_log_tail())).classes(
                "w-full h-[420px] overflow-auto bg-slate-950 text-lime-300 p-3 rounded mt-3"
            ).props('id="log-panel"')

            ui.add_body_html(
                """
                <script>
                window.refreshStrmWatchUi = async function() {
                  try {
                    const response = await fetch('/api/logs', { cache: 'no-store' });
                    if (!response.ok) return;
                    const data = await response.json();
                    const logPanel = document.getElementById('log-panel');
                    if (logPanel) logPanel.innerHTML = data.html;
                    const statusLabel = document.getElementById('status-label');
                    if (statusLabel) statusLabel.textContent = data.is_running ? '🟢 运行中' : '🔴 未启动';
                    const modeLabel = document.getElementById('mode-label');
                    if (modeLabel) {
                      let text = `模式：${data.run_mode}`;
                      if (data.monitor_mode && data.monitor_mode !== '未启动') text += ` / ${data.monitor_mode}`;
                      modeLabel.textContent = text;
                    }
                  } catch (error) {
                    console.debug('refreshStrmWatchUi failed', error);
                  }
                };
                window.refreshStrmWatchUi();
                setInterval(window.refreshStrmWatchUi, 2000);
                </script>
                """
            )


if __name__ in {"__main__", "__mp_main__"}:
    ensure_webhook_template()
    logger.info("启动完成")
    ui.run(
        title="StrmWatch",
        host="0.0.0.0",
        port=8501,
        show=False,
        reload=False,
        show_welcome_message=False,
        uvicorn_logging_level="error",
    )
