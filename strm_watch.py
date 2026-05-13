import asyncio
import http.client
import json
import logging
import os
import time
from pathlib import Path
from threading import Lock, Thread
from typing import Dict, Generator, Optional
from urllib.parse import quote, urlparse

from nicegui import ui
from watchdog.events import FileSystemEventHandler
from watchdog.observers import Observer


CONFIG_FILE = Path(os.getenv("CONFIG_PATH", "/config/settings.json"))


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
            }
            with open(CONFIG_FILE, "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False, indent=4)
            return True
        except Exception as e:
            logger.error(f"Failed to save config: {e}")
            return False


config = GlobalConfig()


class AppStatus:
    is_running = False
    observer: Optional[Observer] = None
    lock = Lock()
    recent_events: Dict[str, float] = {}
    debounce_seconds = 1.0


class NiceGuiLogHandler(logging.Handler):
    def __init__(self, log_widget: ui.log):
        super().__init__()
        self.log_widget = log_widget

    def emit(self, record):
        try:
            msg = json.dumps(f"{self.format(record)}\n")
            ui.run_javascript(
                f'const log = document.getElementById("c{self.log_widget.id}"); '
                f"if (log) {{ log.value += {msg}; log.scrollTop = log.scrollHeight; }}"
            )
        except Exception:
            self.handleError(record)


logger = logging.getLogger("StrmWatch")
logger.setLevel(logging.INFO)
if not logger.handlers:
    stream_handler = logging.StreamHandler()
    stream_handler.setFormatter(logging.Formatter("%(asctime)s - %(message)s", datefmt="%H:%M:%S"))
    logger.addHandler(stream_handler)


def is_valid_ms_url(value: str) -> bool:
    parsed = urlparse(value)
    return parsed.scheme in {"http", "https"} and bool(parsed.netloc)


def _do_send_ms(title: str, content: str):
    conn = None
    try:
        if not is_valid_ms_url(config.ms_url):
            logger.error("Notification failed: invalid MS URL")
            return

        parsed = urlparse(f"{config.ms_url.rstrip('/')}/api/v1/message/openSend")
        payload = json.dumps({"title": title, "content": content})
        headers = {"Content-Type": "application/json", "apiKey": config.ms_key}
        conn_cls = http.client.HTTPSConnection if parsed.scheme == "https" else http.client.HTTPConnection
        conn = conn_cls(parsed.netloc, timeout=5)
        conn.request("POST", parsed.path, body=payload, headers=headers)
        response = conn.getresponse()

        if 200 <= response.status < 300:
            logger.info("Notification sent")
        else:
            logger.error(f"Notification failed: HTTP {response.status}")
    except Exception as e:
        logger.error(f"Notification failed: {e}")
    finally:
        if conn:
            conn.close()


def send_ms_notification(title: str, content: str, force: bool = False):
    if not force and not config.notify:
        return
    if not config.ms_url or not config.ms_key:
        return
    Thread(target=_do_send_ms, args=(title, content), daemon=True).start()


def process_file_logic(src_path: Path) -> bool:
    if src_path.suffix.lower() != ".strm" or not config.old_kw:
        return False
    if not config.ms_url or not config.ms_key:
        logger.warning("Skipped processing: MS URL or API Key is missing")
        return False

    try:
        rel_path = src_path.relative_to(Path(config.src).resolve())
        target_path = Path(config.tgt).resolve() / rel_path
        target_path.parent.mkdir(parents=True, exist_ok=True)

        with open(src_path, "r", encoding="utf-8") as f:
            content = f.read().strip()

        if config.old_kw not in content:
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
        return True
    except Exception as e:
        logger.error(f"Process error {src_path.name}: {e}")
        return False


def strm_generator() -> Generator[Path, None, None]:
    try:
        for path in Path(config.src).resolve().rglob("*"):
            if path.is_file() and path.suffix.lower() == ".strm":
                yield path
    except Exception as e:
        logger.error(f"Scan error: {e}")


async def run_full_scan():
    logger.info("[Task] Full scan started")
    count, total = 0, 0
    try:
        for path in strm_generator():
            total += 1
            if await asyncio.to_thread(process_file_logic, path):
                count += 1
            if total % 50 == 0:
                await asyncio.sleep(0.01)

        logger.info(f"[Task] Scan completed. Total: {total}, Updated: {count}")
        if count > 0:
            send_ms_notification("Full Scan Report", f"Updated: {count}")
    except Exception as e:
        logger.error(f"Scan exception: {e}")


def restart_observer():
    with AppStatus.lock:
        if AppStatus.observer:
            AppStatus.observer.stop()
            AppStatus.observer.join()
            AppStatus.observer = None

        if not os.path.exists(config.src):
            AppStatus.is_running = False
            logger.error(f"Observer failed: Source dir not found ({config.src})")
            return

        if not config.old_kw:
            AppStatus.is_running = False
            logger.warning("Observer suspended: Missing OLD_KEYWORD")
            return

        try:
            AppStatus.recent_events.clear()
            AppStatus.observer = Observer()
            AppStatus.observer.schedule(WatchHandler(), config.src, recursive=True)
            AppStatus.observer.start()
            AppStatus.is_running = True
            logger.info(f"Observer ready: {config.src} -> {config.tgt}")
        except Exception as e:
            AppStatus.observer = None
            AppStatus.is_running = False
            logger.error(f"Observer failed: {e}")


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
            return

        AppStatus.recent_events[event_key] = now
        if len(AppStatus.recent_events) > 1000:
            expired_before = now - 60
            AppStatus.recent_events = {
                key: value for key, value in AppStatus.recent_events.items() if value >= expired_before
            }

        if process_file_logic(src_path):
            logger.info(f"{action}: {os.path.basename(event.src_path)}")

    def on_created(self, event):
        self._handle(event, "Created")

    def on_modified(self, event):
        self._handle(event, "Updated")


def validate_config_for_save() -> Optional[str]:
    if not config.old_kw or not config.new_pre:
        return "Please fill out Keyword and Prefix"
    if not config.ms_url or not config.ms_key:
        return "Please fill out MS URL and API Key"
    if not is_valid_ms_url(config.ms_url):
        return "MS URL must start with http:// or https://"
    return None


@ui.page("/")
def main_page():
    ui.query("body").style("background-color: #f8fafc")

    with ui.header().classes("items-center justify-between bg-slate-900 text-white shadow-md"):
        ui.label("StrmWatch Pro").classes("text-xl font-bold ml-4")
        with ui.row().classes("items-center mr-4 gap-2"):
            ui.label().classes("text-2xl").bind_text_from(
                AppStatus, "is_running", backward=lambda running: "RUNNING" if running else "STOPPED"
            )

    with ui.column().classes("w-full max-w-5xl mx-auto p-4 gap-4"):
        with ui.card().classes("w-full p-6 shadow-sm border-none bg-white"):
            ui.label("System & Path Configuration").classes("text-lg font-bold text-slate-700 mb-4")

            with ui.row().classes("w-full gap-4 mb-4"):
                ui.input("Source Dir (Mapped via Docker)").bind_value(config, "src").classes("flex-1").props("disable")
                ui.input("Target Dir").bind_value(config, "tgt").classes("flex-1").props("disable")

            with ui.row().classes("w-full gap-4 mb-4"):
                ui.input("Old Keyword (OLD_KEYWORD)", validation={"Cannot be empty": lambda v: len(v) > 0}).bind_value(
                    config, "old_kw"
                ).classes("flex-1")
                ui.input("New Prefix (NEW_PREFIX)", validation={"Cannot be empty": lambda v: len(v) > 0}).bind_value(
                    config, "new_pre"
                ).classes("flex-1")

            with ui.row().classes("w-full gap-4 mb-4"):
                ui.input("MS URL").bind_value(config, "ms_url").classes("flex-1")
                ui.input("API Key", password=True).bind_value(config, "ms_key").classes("flex-1")

            with ui.row().classes("w-full gap-6 items-center"):
                ui.checkbox("UrlEncode").bind_value(config, "url_enc")
                ui.checkbox("Enable Notifications").bind_value(config, "notify")
                ui.space()

                def handle_save():
                    validation_error = validate_config_for_save()
                    if validation_error:
                        ui.notify(validation_error, type="warning")
                        return

                    if config.save():
                        ui.notify("Configuration saved and applied", type="positive")
                        logger.info("Configuration updated, restarting observer")
                        restart_observer()
                    else:
                        ui.notify("Save failed, check directory permissions", type="negative")

                def handle_test_notification():
                    if not config.ms_url or not config.ms_key:
                        ui.notify("Please fill out MS URL and API Key first", type="warning")
                        return
                    if not is_valid_ms_url(config.ms_url):
                        ui.notify("MS URL must start with http:// or https://", type="warning")
                        return
                    send_ms_notification("Test", "Connection normal", True)
                    ui.notify("Test notification queued", type="info")

                ui.button("Test Notification", on_click=handle_test_notification).classes("bg-amber-500").props(
                    "unelevated outline"
                )
                ui.button("Save & Apply", on_click=handle_save).classes("bg-green-600 text-white").props("unelevated")

        with ui.row().classes("w-full gap-4 mt-2"):
            async def handle_full_scan(e):
                validation_error = validate_config_for_save()
                if validation_error:
                    ui.notify(validation_error, type="warning")
                    return

                e.sender.props("loading")
                try:
                    await run_full_scan()
                finally:
                    e.sender.props(remove="loading")

            ui.button("Execute Full Scan", on_click=handle_full_scan).classes("flex-1 bg-indigo-600 h-12 text-lg").props(
                "unelevated"
            )

        log_box = ui.log(max_lines=300).classes(
            "w-full h-[400px] bg-slate-900 text-lime-400 font-mono text-xs p-4 rounded-xl shadow-inner mt-4"
        )

        if not any(isinstance(handler, NiceGuiLogHandler) for handler in logger.handlers):
            log_handler = NiceGuiLogHandler(log_box)
            log_handler.setFormatter(logging.Formatter("%H:%M:%S - %(message)s"))
            logger.addHandler(log_handler)


if __name__ in {"__main__", "__mp_main__"}:
    restart_observer()
    ui.run(title="StrmWatch Pro", host="0.0.0.0", port=8501, show=False, reload=False)
