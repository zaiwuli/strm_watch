import os
import time
import json
import logging
import asyncio
import http.client
from datetime import datetime
from urllib.parse import urlparse, quote
from threading import Thread, Lock
from pathlib import Path
from typing import Optional, Generator

from watchdog.observers import Observer
from watchdog.events import FileSystemEventHandler
from nicegui import ui, app

# ================= 1. Configuration Management =================

CONFIG_FILE = Path(os.getenv("CONFIG_PATH", "/config/settings.json"))

class GlobalConfig:
    def __init__(self):
        self.src = os.getenv("SOURCE_DIR", "/source")
        self.tgt = os.getenv("TARGET_DIR", "/target")
        self.old_kw = ""
        self.new_pre = "/"
        self.ms_url = ""
        self.ms_key = ""
        self.url_enc = True
        self.notify = True
        self.load()

    def load(self):
        if CONFIG_FILE.exists() and os.access(CONFIG_FILE, os.R_OK):
            try:
                with open(CONFIG_FILE, 'r', encoding='utf-8') as f:
                    data = json.load(f)
                    self.old_kw = data.get('old_kw', self.old_kw)
                    self.new_pre = data.get('new_pre', self.new_pre)
                    self.ms_url = data.get('ms_url', self.ms_url)
                    self.ms_key = data.get('ms_key', self.ms_key)
                    self.url_enc = data.get('url_enc', self.url_enc)
                    self.notify = data.get('notify', self.notify)
            except Exception as e:
                print(f"Failed to load config: {e}")

    def save(self):
        try:
            CONFIG_FILE.parent.mkdir(parents=True, exist_ok=True)
            data = {
                'old_kw': self.old_kw, 'new_pre': self.new_pre,
                'ms_url': self.ms_url, 'ms_key': self.ms_key,
                'url_enc': self.url_enc, 'notify': self.notify
            }
            with open(CONFIG_FILE, 'w', encoding='utf-8') as f:
                json.dump(data, f, ensure_ascii=False, indent=4)
            return True
        except Exception as e:
            logging.getLogger("StrmWatch").error(f"Failed to save config: {e}")
            return False

config = GlobalConfig()

class AppStatus:
    is_running = False
    observer: Optional[Observer] = None
    lock = Lock()

# ================= 2. Logging System =================

class NiceGuiLogHandler(logging.Handler):
    def __init__(self, log_widget: ui.log):
        super().__init__()
        self.log_widget = log_widget

    def emit(self, record):
        try:
            msg = self.format(record)
            ui.run_javascript(f'const log = document.getElementById("c{self.log_widget.id}"); if(log) {{ log.value += "{msg}\\n"; log.scrollTop = log.scrollHeight; }}')
        except Exception:
            self.handleError(record)

logger = logging.getLogger("StrmWatch")
logger.setLevel(logging.INFO)
sh = logging.StreamHandler()
sh.setFormatter(logging.Formatter('%(asctime)s - %(message)s', datefmt='%H:%M:%S'))
logger.addHandler(sh)

# ================= 3. Core Logic =================

def _do_send_ms(title: str, content: str):
    try:
        p = urlparse(f"{config.ms_url.rstrip('/')}/api/v1/message/openSend")
        payload = json.dumps({"title": title, "content": content})
        headers = {'Content-Type': 'application/json', 'apiKey': config.ms_key}
        conn_cls = http.client.HTTPSConnection if p.scheme == 'https' else http.client.HTTPConnection
        conn = conn_cls(p.netloc, timeout=5)
        conn.request("POST", p.path, body=payload, headers=headers)
        conn.getresponse()
        conn.close()
    except Exception as e:
        logger.error(f"Notification failed: {e}")

def send_ms_notification(title: str, content: str, force: bool = False):
    if not force and not config.notify: return
    if not config.ms_url or not config.ms_key: return
    Thread(target=_do_send_ms, args=(title, content), daemon=True).start()

def process_file_logic(src_path: Path) -> bool:
    if not src_path.suffix == ".strm" or not config.old_kw: return False
    try:
        rel = src_path.relative_to(Path(config.src).resolve())
        target_path = Path(config.tgt).resolve() / rel
        
        target_path.parent.mkdir(parents=True, exist_ok=True)
        with open(src_path, 'r', encoding='utf-8') as f: content = f.read().strip()
            
        if config.old_kw in content:
            path_part = content.split(config.old_kw)[-1].lstrip('/')
            final_media_path = (config.new_pre.rstrip('/') + '/' + path_part).replace('\\', '/')
            if config.url_enc: final_media_path = quote(final_media_path, safe='/')
            
            new_content = f"{config.ms_url.rstrip('/')}/api/v1/cloudStorage/strm302?apiKey={config.ms_key}&pickCode=&path={final_media_path}"
            with open(target_path, 'w', encoding='utf-8') as f: f.write(new_content)
            return True
    except Exception as e:
        logger.error(f"Process error {src_path.name}: {e}")
    return False

# ================= 4. Task & Observer =================

def strm_generator() -> Generator[Path, None, None]:
    try:
        for p in Path(config.src).resolve().rglob("*.strm"): yield p
    except Exception as e:
        logger.error(f"Scan error: {e}")

async def run_full_scan():
    logger.info("📡 [Task] Full scan started...")
    count, total = 0, 0
    try:
        for p in await asyncio.to_thread(list, strm_generator()):
            total += 1
            if await asyncio.to_thread(process_file_logic, p): count += 1
            if total % 50 == 0: await asyncio.sleep(0.01)
        logger.info(f"✅ [Task] Scan completed! Total: {total}, Updated: {count}")
        if count > 0: send_ms_notification("📊 Full Scan Report", f"Updated: {count}")
    except Exception as e:
        logger.error(f"❌ Scan Exception: {e}")

def restart_observer():
    with AppStatus.lock:
        if AppStatus.observer:
            AppStatus.observer.stop()
            AppStatus.observer.join()
            AppStatus.observer = None
        
        if os.path.exists(config.src) and config.old_kw:
            AppStatus.observer = Observer()
            AppStatus.observer.schedule(WatchHandler(), config.src, recursive=True)
            AppStatus.observer.start()
            AppStatus.is_running = True
            logger.info(f"📡 Observer ready: {config.src} -> {config.tgt}")
        else:
            AppStatus.is_running = False
            if not os.path.exists(config.src):
                logger.error(f"❌ Observer failed: Source dir not found ({config.src})")
            else:
                logger.warning("⚠️ Observer suspended: Missing OLD_KEYWORD")

class WatchHandler(FileSystemEventHandler):
    def on_created(self, event):
        if not event.is_directory and process_file_logic(Path(event.src_path)):
            logger.info(f"🆕 Created: {os.path.basename(event.src_path)}")
    def on_modified(self, event):
        if not event.is_directory and process_file_logic(Path(event.src_path)):
            logger.info(f"🔄 Updated: {os.path.basename(event.src_path)}")

# ================= 5. NiceGUI Interface =================

@ui.page('/')
def main_page():
    ui.query('body').style('background-color: #f8fafc')
    
    with ui.header().classes('items-center justify-between bg-slate-900 text-white shadow-md'):
        ui.label('🚀 StrmWatch Pro').classes('text-xl font-bold ml-4')
        with ui.row().classes('items-center mr-4 gap-2'):
            ui.label('●').classes('text-2xl').bind_style_from(AppStatus, 'is_running', backward=lambda r: f'color: {"#4ade80" if r else "#f87171"}')
            ui.label().bind_text_from(AppStatus, 'is_running', backward=lambda r: 'RUNNING' if r else 'STOPPED').classes('font-mono font-bold')

    with ui.column().classes('w-full max-w-5xl mx-auto p-4 gap-4'):
        with ui.card().classes('w-full p-6 shadow-sm border-none bg-white'):
            ui.label('⚙️ System & Path Configuration').classes('text-lg font-bold text-slate-700 mb-4')
            
            with ui.row().classes('w-full gap-4 mb-4'):
                ui.input('Source Dir (Mapped via Docker)').bind_value(config, 'src').classes('flex-1').props('disable')
                ui.input('Target Dir').bind_value(config, 'tgt').classes('flex-1').props('disable')
                
            with ui.row().classes('w-full gap-4 mb-4'):
                kw_input = ui.input('Old Keyword (OLD_KEYWORD)', validation={'Cannot be empty': lambda v: len(v) > 0}).bind_value(config, 'old_kw').classes('flex-1')
                pre_input = ui.input('New Prefix (NEW_PREFIX)', validation={'Cannot be empty': lambda v: len(v) > 0}).bind_value(config, 'new_pre').classes('flex-1')
                
            with ui.row().classes('w-full gap-4 mb-4'):
                ui.input('MS URL').bind_value(config, 'ms_url').classes('flex-1')
                ui.input('API Key', password=True).bind_value(config, 'ms_key').classes('flex-1')
                
            with ui.row().classes('w-full gap-6 items-center'):
                ui.checkbox('UrlEncode').bind_value(config, 'url_enc')
                ui.checkbox('Enable Notifications').bind_value(config, 'notify')
                ui.space()
                
                def handle_save():
                    if not config.old_kw or not config.new_pre:
                        ui.notify('Please fill out Keyword and Prefix!', type='warning')
                        return
                    if config.save():
                        ui.notify('✅ Configuration saved and applied!', type='positive')
                        logger.info("🛠️ Configuration updated, restarting observer...")
                        restart_observer() 
                    else:
                        ui.notify('❌ Save failed, check directory permissions', type='negative')
                
                ui.button('🧪 Test Notification', on_click=lambda: send_ms_notification("Test", "Connection normal!", True)).classes('bg-amber-500').props('unelevated outline')
                ui.button('💾 Save & Apply', on_click=handle_save).classes('bg-green-600 text-white').props('unelevated')

        with ui.row().classes('w-full gap-4 mt-2'):
            async def handle_full_scan(e):
                e.sender.props('loading')
                try:
                    await run_full_scan()
                finally:
                    e.sender.props(remove='loading')
            
            ui.button('🏃 Execute Full Scan', on_click=handle_full_scan).classes('flex-1 bg-indigo-600 h-12 text-lg').props('unelevated')

        log_box = ui.log(max_lines=300).classes('w-full h-[400px] bg-slate-900 text-lime-400 font-mono text-xs p-4 rounded-xl shadow-inner mt-4')
        
        if not any(isinstance(h, NiceGuiLogHandler) for h in logger.handlers):
            lh = NiceGuiLogHandler(log_box)
            lh.setFormatter(logging.Formatter('%H:%M:%S - %(message)s'))
            logger.addHandler(lh)

if __name__ in {"__main__", "__mp_main__"}:
    restart_observer()
    ui.run(title="StrmWatch Pro", port=8501, show=False, reload=False)