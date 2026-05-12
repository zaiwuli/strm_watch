import os, time, json, http.client
from urllib.parse import urlparse, quote
from threading import Thread
import streamlit as st
from watchdog.observers import Observer
from watchdog.events import FileSystemEventHandler

# --- 基础设置 ---
st.set_page_config(page_title="StrmWatch", layout="centered")
st.markdown("<style>#MainMenu {visibility: hidden;} footer {visibility: hidden;} header {visibility: hidden;} .block-container {padding-top: 2rem;}</style>", unsafe_allow_html=True)

# 环境与状态
SRC = os.getenv("SOURCE_DIR", "/源文件夹")
TGT = os.getenv("TARGET_DIR", "/目标文件夹")
if 'logs' not in st.session_state: st.session_state.logs = []
if 'obs_started' not in st.session_state: st.session_state.obs_started = False

# --- 功能函数 ---
def send_ms(title, content):
    ms_url = st.session_state.get('ms_u', os.getenv("MS_URL", ""))
    ms_key = st.session_state.get('ms_k', os.getenv("MS_API_KEY", ""))
    if not ms_url.startswith("http") or not ms_key: return
    try:
        p = urlparse(f"{ms_url.rstrip('/')}/api/v1/message/openSend")
        conn = (http.client.HTTPSConnection(p.netloc, timeout=5) if p.scheme == 'https' else http.client.HTTPConnection(p.netloc, timeout=5))
        conn.request("POST", p.path, body=json.dumps({"title": title, "content": content}), headers={'Content-Type': 'application/json', 'apiKey': ms_key})
        conn.getresponse(); conn.close()
    except: pass

def process(path):
    kw = st.session_state.get('kw', os.getenv("OLD_KEYWORD", ""))
    pre = st.session_state.get('pre', os.getenv("NEW_MOUNT_PREFIX", "/"))
    ms_u = st.session_state.get('ms_u', os.getenv("MS_URL", ""))
    ms_k = st.session_state.get('ms_k', os.getenv("MS_API_KEY", ""))
    if not path.endswith(".strm") or not kw: return False
    try:
        rel = os.path.relpath(path, SRC)
        out = os.path.join(TGT, rel)
        os.makedirs(os.path.dirname(out), exist_ok=True)
        with open(path, 'r', encoding='utf-8') as f: content = f.read().strip()
        if kw in content:
            p_part = content.split(kw)[-1].lstrip('/')
            f_path = (pre.rstrip('/') + '/' + p_part).replace('\\', '/')
            if st.session_state.get('enc', True): f_path = quote(f_path, safe='/')
            final = f"{ms_u.rstrip('/')}/api/v1/cloudStorage/strm302?apiKey={ms_k}&pickCode=&path={f_path}"
            with open(out, 'w', encoding='utf-8') as f: f.write(final)
            return True
    except: pass
    return False

def add_log(msg):
    st.session_state.logs.insert(0, f"[{time.strftime('%H:%M:%S')}] {msg}")
    if len(st.session_state.logs) > 30: st.session_state.logs.pop()

# --- UI 布局 ---
st.title("🚀 StrmWatch")

with st.expander("⚙️ 设置参数"):
    st.session_state.ms_u = st.text_input("MS 地址", value=os.getenv("MS_URL", "https://"))
    st.session_state.ms_k = st.text_input("API Key", value=os.getenv("MS_API_KEY", ""), type="password")
    st.session_state.kw = st.text_input("旧关键字", value=os.getenv("OLD_KEYWORD", ""))
    st.session_state.pre = st.text_input("新前缀", value=os.getenv("NEW_MOUNT_PREFIX", "/"))
    st.session_state.enc = st.checkbox("UrlEncode", value=True)

test_in = st.text_input("🔍 粘贴路径快速转换")
if test_in and st.session_state.get('kw'):
    try:
        kw = st.session_state.kw
        if kw in test_in:
            p_part = test_in.split(kw)[-1].lstrip('/')
            f_path = (st.session_state.pre.rstrip('/') + '/' + p_part).replace('\\', '/')
            if st.session_state.enc: f_path = quote(f_path, safe='/')
            st.code(f"{st.session_state.ms_u.rstrip('/')}/api/v1/cloudStorage/strm302?apiKey={st.session_state.ms_k}&pickCode=&path={f_path}")
    except: st.error("转换失败")

c1, c2 = st.columns([1, 4])
if c1.button("全量扫描"):
    count = 0
    for r, _, fs in os.walk(SRC):
        for f in fs:
            if process(os.path.join(r, f)): count += 1
    add_log(f"📊 扫描完成: {count}")
    if count > 0: send_ms("📊 同步报告", f"扫描处理了 {count} 个文件")

st.code("\n".join(st.session_state.logs) if st.session_state.logs else "等待任务...")

# --- 监听器 ---
class H(FileSystemEventHandler):
    def on_created(self, e):
        if not e.is_directory and process(e.src_path):
            add_log(f"🆕 入库: {os.path.basename(e.src_path)}")
            send_ms("🆕 同步通知", f"入库: {os.path.basename(e.src_path)}")
    def on_modified(self, e):
        if not e.is_directory and process(e.src_path): add_log(f"🔄 更新: {os.path.basename(e.src_path)}")

if not st.session_state.obs_started and os.path.exists(SRC):
    obs = Observer(); obs.schedule(H(), SRC, recursive=True); obs.start()
    st.session_state.obs_started = True
    add_log("🚀 监控系统启动成功")