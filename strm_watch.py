import os, time, json, http.client
from urllib.parse import urlparse, quote
from threading import Thread
import streamlit as st
from watchdog.observers import Observer
from watchdog.events import FileSystemEventHandler

# --- 基础 UI 配置 ---
st.set_page_config(page_title="StrmWatch Pro", layout="centered")
st.markdown("""
    <style>
    #MainMenu {visibility: hidden;}
    footer {visibility: hidden;}
    header {visibility: hidden;}
    .block-container {padding-top: 2rem;}
    </style>
""", unsafe_allow_html=True)

# 物理映射路径 (环境变量)
SRC = os.getenv("SOURCE_DIR", "/源文件夹")
TGT = os.getenv("TARGET_DIR", "/目标文件夹")

# --- 初始化全局配置状态 ---
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

# --- 核心功能函数 ---

def add_log(msg):
    t = time.strftime('%H:%M:%S')
    st.session_state.logs.insert(0, f"[{t}] {msg}")
    if len(st.session_state.logs) > 30: st.session_state.logs.pop()

def send_ms(title, content, force=False):
    cfg = st.session_state.runtime_config
    if not force and not cfg["notify_en"]: return
    if not cfg["ms_u"].startswith("http") or not cfg["ms_k"]: return
    try:
        p = urlparse(f"{cfg['ms_u'].rstrip('/')}/api/v1/message/openSend")
        payload = json.dumps({"title": title, "content": content})
        conn = (http.client.HTTPSConnection(p.netloc, timeout=5) if p.scheme == 'https' 
                else http.client.HTTPConnection(p.netloc, timeout=5))
        conn.request("POST", p.path, body=payload, headers={'Content-Type': 'application/json', 'apiKey': cfg["ms_k"]})
        conn.getresponse(); conn.close()
        return True
    except: return False

def convert_logic(src_path):
    """单文件转换逻辑"""
    cfg = st.session_state.runtime_config
    if not src_path.endswith(".strm") or not cfg["kw"]: return False
    try:
        rel = os.path.relpath(src_path, SRC)
        out = os.path.join(TGT, rel)
        os.makedirs(os.path.dirname(out), exist_ok=True)
        with open(src_path, 'r', encoding='utf-8') as f: content = f.read().strip()
        
        if cfg["kw"] in content:
            path_part = content.split(cfg["kw"])[-1].lstrip('/')
            final_path = (cfg["pre"].rstrip('/') + '/' + path_part).replace('\\', '/')
            if cfg["enc"]: final_path = quote(final_path, safe='/')
            
            # 封装 MS API 链接
            new_c = f"{cfg['ms_u'].rstrip('/')}/api/v1/cloudStorage/strm302?apiKey={cfg['ms_k']}&pickCode=&path={final_path}"
            with open(out, 'w', encoding='utf-8') as f: f.write(new_c)
            return True
    except: pass
    return False

# --- UI 界面布局 ---

st.title("🚀 StrmWatch Pro")

# 1. 路径推演器 (自动提取 + 手动微调)
with st.expander("🛠️ 路径规则推演器", expanded=True):
    st.caption("对比旧/新路径，自动推测转换规则。即便文件名不同，程序也会尝试匹配目录深度。")
    col_l, col_r = st.columns(2)
    old_raw = col_l.text_area("旧 STRM 内容 (示例)", placeholder="/CloudNAS/115/Movie_A.mkv", height=68)
    new_raw = col_r.text_area("目标 挂载内容 (示例)", placeholder="/115/Movie_A.mkv", height=68)
    
    if st.button("🔥 推演并应用转换规则", use_container_width=True):
        if old_raw and new_raw:
            o_s, n_s = old_raw.strip(), new_raw.strip()
            o_p, n_p = o_s.split('/'), n_s.split('/')
            if len(o_p) > 1 and len(n_p) > 1:
                idx = 1
                while idx < min(len(o_p), len(n_p)):
                    if o_p[-idx] != n_p[-idx]: break
                    idx += 1
                # 更新全局配置
                st.session_state.runtime_config["kw"] = "/".join(o_p[:-idx+1]) + "/"
                st.session_state.runtime_config["pre"] = "/".join(n_p[:-idx+1]) + "/"
                st.success("规则推演完成！参数已回填，可点击下方设置查看。")
            else:
                st.error("路径格式无效。")

# 2. 核心设置区
with st.expander("⚙️ 核心参数设置"):
    c = st.session_state.runtime_config
    c["ms_u"] = st.text_input("Media Saber 地址", value=c["ms_u"])
    c["ms_k"] = st.text_input("API Key", value=c["ms_k"], type="password")
    c["kw"] = st.text_input("旧关键字 (OLD_KEYWORD)", value=c["kw"])
    c["pre"] = st.text_input("新前缀 (NEW_PREFIX)", value=c["pre"])
    
    col_s1, col_s2 = st.columns(2)
    c["enc"] = col_s1.checkbox("路径 UrlEncode 编码", value=c["enc"])
    c["notify_en"] = col_s2.checkbox("开启实时通知推送", value=c["notify_en"])
    
    if st.button("🧪 发送测试通知"):
        if send_ms("StrmWatch 测试", "API 通知功能连接成功！", force=True):
            st.toast("通知测试成功")
        else: st.error("测试失败，请检查地址或 Key")

# 3. 任务控制中心
st.divider()
c_run1, c_run2, c_run3 = st.columns(3)

if c_run1.button("🏃 立即执行全量扫描", use_container_width=True, type="primary"):
    count, total = 0, 0
    with st.spinner("正在遍历媒体库并更正路径..."):
        for r, _, fs in os.walk(SRC):
            for f in fs:
                if f.endswith(".strm"):
                    total += 1
                    if convert_logic(os.path.join(r, f)): count += 1
    
    msg = f"任务报告：全量同步完成\n共检索：{total} 个文件\n更正：{count} 个文件"
    add_log(f"📊 全量同步完成：检索 {total}，更正 {count}")
    send_ms("全量同步报告", msg)
    st.success(f"全量任务完成！共转换 {count} 个文件。")

if c_run2.button("🧹 清空界面日志", use_container_width=True):
    st.session_state.logs = []
    st.rerun()

if c_run3.button("🔄 刷新日志", use_container_width=True):
    st.experimental_rerun()

st.code("\n".join(st.session_state.logs) if st.session_state.logs else "等待任务中...", language="text")

# --- 后台增量监听 ---

class WatchHandler(FileSystemEventHandler):
    def on_created(self, e):
        if not e.is_directory and convert_logic(e.src_path):
            add_log(f"🆕 自动入库: {os.path.basename(e.src_path)}")
            send_ms("🆕 入库通知", f"自动转换新文件: {os.path.basename(e.src_path)}")
    def on_modified(self, e):
        if not e.is_directory and convert_logic(e.src_path):
            add_log(f"🔄 自动更新: {os.path.basename(e.src_path)}")

if not st.session_state.obs_started and os.path.exists(SRC):
    obs = Observer()
    obs.schedule(WatchHandler(), SRC, recursive=True)
    obs.start()
    st.session_state.obs_started = True
    add_log("🚀 哨兵增量监听已就绪")