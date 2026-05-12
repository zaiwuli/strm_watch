import os, time, json, http.client
from urllib.parse import urlparse, quote
import streamlit as st
from watchdog.observers import Observer
from watchdog.events import FileSystemEventHandler

# --- 基础设置 ---
st.set_page_config(page_title="StrmWatch", layout="centered")
st.markdown("<style>#MainMenu {visibility: hidden;} footer {visibility: hidden;} header {visibility: hidden;} .block-container {padding-top: 2rem;}</style>", unsafe_allow_html=True)

# 物理路径
SRC = os.getenv("SOURCE_DIR", "/源文件夹")
TGT = os.getenv("TARGET_DIR", "/目标文件夹")

# --- 全局状态初始化 ---
if 'runtime_config' not in st.session_state:
    st.session_state.runtime_config = {
        "ms_u": os.getenv("MS_URL", "https://"),
        "ms_k": os.getenv("MS_API_KEY", ""),
        "kw": os.getenv("OLD_KEYWORD", ""),
        "pre": os.getenv("NEW_MOUNT_PREFIX", "/"),
        "enc": os.getenv("ENABLE_URL_ENCODE", "True").lower() == "true",
        "notify_en": True
    }
if 'logs' not in st.session_state: st.session_state.logs = []
if 'obs_started' not in st.session_state: st.session_state.obs_started = False

def add_log(msg):
    t = time.strftime('%H:%M:%S')
    st.session_state.logs.insert(0, f"[{t}] {msg}")
    if len(st.session_state.logs) > 20: st.session_state.logs.pop()

# --- 核心逻辑 ---
def send_ms(title, content, force=False):
    cfg = st.session_state.runtime_config
    if not force and not cfg["notify_en"]: return
    if not cfg["ms_u"].startswith("http") or not cfg["ms_k"]: return
    try:
        p = urlparse(f"{cfg['ms_u'].rstrip('/')}/api/v1/message/openSend")
        conn = (http.client.HTTPSConnection(p.netloc, timeout=5) if p.scheme == 'https' else http.client.HTTPConnection(p.netloc, timeout=5))
        conn.request("POST", p.path, body=json.dumps({"title": title, "content": content}), headers={'Content-Type': 'application/json', 'apiKey': cfg["ms_k"]})
        conn.getresponse(); conn.close()
        return True
    except Exception as e:
        add_log(f"⚠️ 通知发送失败: {e}")
        return False

def process(path):
    cfg = st.session_state.runtime_config
    if not path.endswith(".strm") or not cfg["kw"]: return False
    try:
        rel = os.path.relpath(path, SRC)
        out = os.path.join(TGT, rel)
        os.makedirs(os.path.dirname(out), exist_ok=True)
        with open(path, 'r', encoding='utf-8') as f: content = f.read().strip()
        if cfg["kw"] in content:
            p_part = content.split(cfg["kw"])[-1].lstrip('/')
            f_path = (cfg["pre"].rstrip('/') + '/' + p_part).replace('\\', '/')
            if cfg["enc"]: f_path = quote(f_path, safe='/')
            final = f"{cfg['ms_u'].rstrip('/')}/api/v1/cloudStorage/strm302?apiKey={cfg['ms_k']}&pickCode=&path={f_path}"
            with open(out, 'w', encoding='utf-8') as f: f.write(final)
            return True
    except: pass
    return False

# --- UI 布局 ---
st.title("🚀 StrmWatch")

# 1. 快捷配置提取器
with st.expander("🛠️ 快捷配置提取器", expanded=True):
    st.caption("粘贴两个 strm 内容，自动计算关键字与前缀")
    col_a, col_b = st.columns(2)
    old_sample = col_a.text_area("旧 strm 内容 (示例)", placeholder="/CloudNAS/115/movie.mkv", height=60)
    new_sample = col_b.text_area("新 strm 内容 (示例)", placeholder="/115/movie.mkv", height=60)
    
    if st.button("🔥 提取并应用配置", use_container_width=True):
        if old_sample and new_sample:
            # 简单算法：寻找共同后缀，反推前缀
            # 这里我们假设文件名是一样的
            old_s, new_s = old_sample.strip(), new_sample.strip()
            common_suffix = os.path.commonprefix([old_s[::-1], new_s[::-1]])[::-1]
            if common_suffix and '/' in common_suffix:
                actual_suffix = common_suffix[common_suffix.find('/'):]
                st.session_state.runtime_config["kw"] = old_s.replace(actual_suffix, "") + "/"
                st.session_state.runtime_config["pre"] = new_s.replace(actual_suffix, "") + "/"
                st.success("配置已更新！请展开下方设置检查。")
            else:
                st.error("无法识别共同路径，请确保文件名一致。")

# 2. 参数设置
with st.expander("⚙️ 参数设置"):
    cfg = st.session_state.runtime_config
    cfg["ms_u"] = st.text_input("MS 地址", value=cfg["ms_u"])
    cfg["ms_k"] = st.text_input("API Key", value=cfg["ms_k"], type="password")
    cfg["kw"] = st.text_input("旧关键字 (OLD_KEYWORD)", value=cfg["kw"])
    cfg["pre"] = st.text_input("新前缀 (NEW_PREFIX)", value=cfg["pre"])
    
    c1, c2 = st.columns(2)
    cfg["enc"] = c1.checkbox("开启 UrlEncode", value=cfg["enc"])
    cfg["notify_en"] = c2.checkbox("开启通知推送", value=cfg["notify_en"])
    
    if st.button("🧪 发送测试通知"):
        if send_ms("StrmWatch 测试", "如果你收到这条消息，说明配置成功！", force=True):
            st.toast("测试通知已发出！")
        else:
            st.error("发送失败，请检查地址或 Key")

# 3. 任务执行
st.divider()
col_run1, col_run2 = st.columns([1, 1])
if col_run1.button("🔄 全量扫描同步", use_container_width=True):
    count = 0
    with st.spinner("同步中..."):
        for r, _, fs in os.walk(SRC):
            for f in fs:
                if process(os.path.join(r, f)): count += 1
    add_log(f"📊 全量扫描完成: {count}")

if col_run2.button("🧹 清空界面日志", use_container_width=True):
    st.session_state.logs = []
    st.rerun()

# 日志显示
st.code("\n".join(st.session_state.logs) if st.session_state.logs else "等待任务...", language="text")

# --- 监控监听器 ---
class H(FileSystemEventHandler):
    def on_created(self, e):
        if not e.is_directory and process(e.src_path):
            add_log(f"🆕 入库: {os.path.basename(e.src_path)}")
            send_ms("🆕 入库通知", f"文件: {os.path.basename(e.src_path)}")
    def on_modified(self, e):
        if not e.is_directory and process(e.src_path):
            add_log(f"🔄 更新: {os.path.basename(e.src_path)}")

if not st.session_state.obs_started:
    if os.path.exists(SRC):
        obs = Observer()
        obs.schedule(H(), SRC, recursive=True)
        obs.start()
        st.session_state.obs_started = True
        add_log("🚀 监控已启动")

# 自动刷新
time.sleep(5)
st.rerun()