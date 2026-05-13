import asyncio
import http.client
import html
import json
import logging
import os
import time
from logging.handlers import RotatingFileHandler
from pathlib import Path
from threading import Event, Lock, Thread
from typing import Dict, Generator, Optional
from urllib.parse import parse_qs, quote, unquote, urlparse

from nicegui import ui
from watchdog.events import FileSystemEventHandler
from watchdog.observers import Observer


CONFIG_FILE = Path(os.getenv("CONFIG_PATH", "/config/settings.json"))
LOG_FILE = CONFIG_FILE.parent / "strm_watch.log"


class GlobalConfig:
    def __init__(self):
        self.src = os.getenv("SOURCE_DIR", "/源文件夹")
        self.tgt = os.getenv("TARGET_DIR", "/目标文件夹")
        self.old_kw = ""
        self.new_pre = "/"
        self.ms_url = ""
        self.ms_key = ""
        self.url_enc = True
        self.notify = True
        self.poll_interval = 60
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
            self.notify = data.get("notify", self.notify)
            self.poll_interval = int(data.get("poll_interval", self.poll_interval))
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
                "notify": self.notify,
                "poll_interval": self.poll_interval,
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

    stream_handler = logging.StreamHandler()
    stream_handler.setFormatter(formatter)
    logger_obj.addHandler(stream_handler)

    file_handler = RotatingFileHandler(LOG_FILE, maxBytes=2_000_000, backupCount=3, encoding="utf-8")
    file_handler.setFormatter(formatter)
    logger_obj.addHandler(file_handler)
    return logger_obj


logger = setup_logging()


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


def infer_config_from_examples(new_strm_url: str, old_strm_path: str) -> Dict[str, str]:
    parsed = urlparse(new_strm_url.strip())
    if not parsed.scheme or not parsed.netloc:
        raise ValueError("新 STRM 链接格式不正确")

    query = parse_qs(parsed.query)
    api_key = query.get("apiKey", [""])[0]
    encoded_new_path = query.get("path", [""])[0]
    new_path = unquote(encoded_new_path).strip()
    old_path = old_strm_path.strip()

    if not api_key:
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
        "mode": mode,
    }


def _do_send_ms(title: str, content: str):
    conn = None
    try:
        if not is_valid_ms_url(config.ms_url):
            logger.error("通知失败：媒体服务地址格式不正确")
            return

        parsed = urlparse(f"{config.ms_url.rstrip('/')}/api/v1/message/openSend")
        payload = json.dumps({"title": title, "content": content})
        headers = {"Content-Type": "application/json", "apiKey": config.ms_key}
        conn_cls = http.client.HTTPSConnection if parsed.scheme == "https" else http.client.HTTPConnection
        conn = conn_cls(parsed.netloc, timeout=5)
        conn.request("POST", parsed.path, body=payload, headers=headers)
        response = conn.getresponse()

        if 200 <= response.status < 300:
            logger.info("通知发送成功：%s", title)
        else:
            logger.error("通知发送失败：HTTP %s", response.status)
    except Exception as e:
        logger.error("通知发送失败：%s", e)
    finally:
        if conn:
            conn.close()


def send_ms_notification(title: str, content: str, force: bool = False):
    if not force and not config.notify:
        logger.info("通知已跳过：通知开关未启用")
        return
    if not config.ms_url or not config.ms_key:
        logger.warning("通知已跳过：媒体服务地址或 API Key 为空")
        return
    logger.info("通知已加入发送队列：%s", title)
    Thread(target=_do_send_ms, args=(title, content), daemon=True).start()


def target_path_for_source(src_path: Path) -> Path:
    rel_path = src_path.relative_to(Path(config.src).resolve())
    return Path(config.tgt).resolve() / rel_path


def build_incremental_notification_content(converted: list[str], deleted: list[str]) -> str:
    lines = [
        "STRM 增量处理完成",
        f"转换/更新：{len(converted)}",
        f"删除同步：{len(deleted)}",
    ]
    if converted:
        lines.append("")
        lines.append("转换/更新文件：")
        lines.extend(f"- {name}" for name in converted[:10])
        if len(converted) > 10:
            lines.append(f"- 其余 {len(converted) - 10} 个文件省略")
    if deleted:
        lines.append("")
        lines.append("删除同步文件：")
        lines.extend(f"- {name}" for name in deleted[:10])
        if len(deleted) > 10:
            lines.append(f"- 其余 {len(deleted) - 10} 个文件省略")
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

    content = build_incremental_notification_content(converted, deleted)
    logger.info("发送增量汇总通知：转换/更新=%s，删除=%s", len(converted), len(deleted))
    send_ms_notification("STRM 增量处理通知", content)


def delayed_incremental_notification():
    time.sleep(AppStatus.incremental_notify_delay)
    flush_incremental_notification()


def record_incremental_change(action: str, src_path: Path):
    with AppStatus.lock:
        if action == "deleted":
            AppStatus.incremental_deleted.append(src_path.name)
        else:
            AppStatus.incremental_converted.append(src_path.name)
        if AppStatus.incremental_notify_timer is None or not AppStatus.incremental_notify_timer.is_alive():
            AppStatus.incremental_notify_timer = Thread(target=delayed_incremental_notification, daemon=True)
            AppStatus.incremental_notify_timer.start()
            logger.info("增量汇总通知计时开始：%s 秒后发送", AppStatus.incremental_notify_delay)


def process_file_logic(src_path: Path) -> bool:
    if src_path.suffix.lower() != ".strm" or not config.old_kw:
        return False
    if not config.ms_url or not config.ms_key:
        logger.warning("跳过处理：媒体服务地址或 API Key 为空")
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
            send_ms_notification("全量扫描报告", f"已转换：{count}")
    except Exception as e:
        logger.error("全量任务异常：%s", e)


def validate_config() -> Optional[str]:
    if not config.old_kw or not config.new_pre:
        return "请填写旧路径关键字和新路径前缀"
    if not config.ms_url or not config.ms_key:
        return "请填写媒体服务地址和 API Key"
    if not is_valid_ms_url(config.ms_url):
        return "媒体服务地址必须以 http:// 或 https:// 开头"
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
    example_new = {"value": ""}
    example_old = {"value": ""}
    run_options = {"mode": "增量", "monitor": "监控"}
    log_area = None
    status_label = None
    mode_label = None

    def refresh_log():
        if log_area is not None:
            log_area.set_content(format_log_html(read_log_tail()))
            log_area.update()

    def refresh_status():
        if status_label is not None:
            status_label.set_text("🟢 运行中" if AppStatus.is_running else "🔴 未启动")
            status_label.update()
        if mode_label is not None:
            mode_text = f"模式：{AppStatus.run_mode}"
            if AppStatus.monitor_mode not in {"", "未启动"}:
                mode_text += f" / {AppStatus.monitor_mode}"
            mode_label.set_text(mode_text)
            mode_label.update()

    def refresh_ui_state():
        refresh_status()
        refresh_log()

    with ui.header().classes("items-center justify-between bg-slate-900 text-white shadow-md"):
        ui.label("📡 STRM 监控转换工具").classes("text-xl font-bold ml-4")
        with ui.row().classes("items-center mr-4 gap-3"):
            status_label = ui.label("🟢 运行中" if AppStatus.is_running else "🔴 未启动").classes("text-base font-bold")
            mode_label = ui.label(f"模式：{AppStatus.run_mode}").classes("text-sm text-slate-200")

    with ui.column().classes("w-full max-w-6xl mx-auto p-4 gap-4"):
        with ui.card().classes("w-full p-5 shadow-sm border-none bg-white"):
            ui.label("🧭 启动控制").classes("text-lg font-bold text-slate-700 mb-3")
            with ui.row().classes("w-full gap-3 items-end"):
                with ui.column().classes("gap-1"):
                    ui.label("执行模式").classes("text-xs text-slate-500")
                    ui.toggle(["全量", "增量"], value="增量").bind_value(run_options, "mode").classes("w-56").props(
                        "unelevated spread"
                    )
                with ui.column().classes("gap-1"):
                    ui.label("增量方式").classes("text-xs text-slate-500")
                    ui.toggle(["监控", "轮询"], value="监控").bind_value(run_options, "monitor").classes("w-56").props(
                        "unelevated spread"
                    )
                ui.number("轮询间隔（秒）", min=5, max=86400, step=5).bind_value(config, "poll_interval").classes("w-40")
                ui.space()

                async def handle_start():
                    try:
                        logger.info("用户点击启动：模式=%s，增量方式=%s", run_options["mode"], run_options["monitor"])
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
                        refresh_ui_state()

                def handle_stop():
                    try:
                        logger.info("用户点击停止")
                        stop_runtime()
                        ui.notify("已停止", type="info")
                    finally:
                        refresh_ui_state()

                ui.button("▶ 启动", on_click=handle_start).classes("w-28 bg-green-600 text-white").props(
                    "unelevated"
                )
                ui.button("停止", on_click=handle_stop).classes("w-28 bg-slate-600 text-white").props("unelevated")
        with ui.card().classes("w-full p-5 shadow-sm border-none bg-white"):
            ui.label("⚙️ 系统与路径配置").classes("text-lg font-bold text-slate-700 mb-4")

            with ui.expansion("🧠 小工具", icon="auto_awesome").classes("w-full mb-4"):
                ui.input("新 STRM 示例链接").bind_value(example_new, "value").classes("w-full mb-3")
                ui.input("旧 STRM 原始路径").bind_value(example_old, "value").classes("w-full mb-3")

                def handle_infer_config():
                    try:
                        logger.info("用户点击识别并填充")
                        logger.info("开始识别示例 STRM 配置")
                        inferred = infer_config_from_examples(example_new["value"], example_old["value"])
                        config.ms_url = inferred["ms_url"]
                        config.ms_key = inferred["ms_key"]
                        config.old_kw = inferred["old_kw"]
                        config.new_pre = inferred["new_pre"]
                        ui.notify(f"已识别并填充配置：{inferred['mode']}", type="positive")
                        logger.info(
                            "已识别配置：模式=%s，服务=%s，API Key=%s，旧路径关键字=%s，新路径前缀=%s",
                            inferred["mode"],
                            inferred["ms_url"],
                            mask_secret(inferred["ms_key"]),
                            inferred["old_kw"],
                            inferred["new_pre"],
                        )
                    except ValueError as e:
                        logger.warning("识别失败：%s", e)
                        ui.notify(str(e), type="warning")
                    finally:
                        refresh_ui_state()

                ui.button("✨ 识别并填充", on_click=handle_infer_config).classes("bg-cyan-600 text-white").props(
                    "unelevated"
                )

            with ui.row().classes("w-full gap-4 mb-4"):
                ui.input("源目录（Docker 挂载）").bind_value(config, "src").classes("flex-1").props("disable")
                ui.input("目标目录（Docker 挂载）").bind_value(config, "tgt").classes("flex-1").props("disable")

            with ui.row().classes("w-full gap-4 mb-4"):
                ui.input("旧路径关键字", validation={"不能为空": lambda v: len(v) > 0}).bind_value(
                    config, "old_kw"
                ).classes("flex-1")
                ui.input("新路径前缀", validation={"不能为空": lambda v: len(v) > 0}).bind_value(
                    config, "new_pre"
                ).classes("flex-1")

            with ui.row().classes("w-full gap-4 mb-4"):
                ui.input("媒体服务地址").bind_value(config, "ms_url").classes("flex-1")
                ui.input("API Key", password=True).bind_value(config, "ms_key").classes("flex-1")

            with ui.row().classes("w-full gap-6 items-center"):
                ui.checkbox("URL 编码").bind_value(config, "url_enc")
                ui.checkbox("启用通知").bind_value(config, "notify")
                ui.space()

                def handle_save():
                    try:
                        logger.info("用户点击保存配置")
                        if config.save():
                            ui.notify("配置已保存", type="positive")
                            logger.info("配置已保存：%s", CONFIG_FILE)
                        else:
                            logger.error("保存配置失败")
                            ui.notify("保存失败，请检查配置目录权限", type="negative")
                    finally:
                        refresh_ui_state()

                def handle_test_notification():
                    try:
                        logger.info("用户点击测试通知")
                        logger.info("开始测试通知")
                        if not config.ms_url or not config.ms_key:
                            logger.warning("测试通知失败：媒体服务地址或 API Key 为空")
                            ui.notify("请先填写媒体服务地址和 API Key", type="warning")
                            return
                        if not is_valid_ms_url(config.ms_url):
                            logger.warning("测试通知失败：媒体服务地址格式不正确")
                            ui.notify("媒体服务地址必须以 http:// 或 https:// 开头", type="warning")
                            return
                        send_ms_notification("测试通知", "连接正常", True)
                        ui.notify("测试通知已发送到队列", type="info")
                    finally:
                        refresh_ui_state()

                ui.button("🧪 测试通知", on_click=handle_test_notification).classes("bg-amber-500").props(
                    "unelevated outline"
                )
                ui.button("💾 保存配置", on_click=handle_save).classes("bg-green-600 text-white").props("unelevated")

        with ui.expansion("📜 运行日志", icon="article").classes("w-full bg-white p-3 rounded shadow-sm"):
            with ui.row().classes("w-full items-center justify-between"):
                ui.label("最新日志显示在最上方").classes("text-sm text-slate-600")
                ui.label(f"日志文件：{LOG_FILE}").classes("text-xs text-slate-500")
            log_area = ui.html(format_log_html(read_log_tail())).classes(
                "w-full h-[420px] overflow-auto bg-slate-950 text-lime-300 p-3 rounded mt-3"
            )

            refresh_ui_state()
            ui.timer(2.0, refresh_ui_state)


if __name__ in {"__main__", "__mp_main__"}:
    logger.info("服务启动完成，等待用户选择全量/增量后点击启动")
    ui.run(title="StrmWatch Pro", host="0.0.0.0", port=8501, show=False, reload=False)
