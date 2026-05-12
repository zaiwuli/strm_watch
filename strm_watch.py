import os, time, json, http.client
from urllib.parse import urlparse, quote
import streamlit as st
from watchdog.observers import Observer
from watchdog.events import FileSystemEventHandler

# --- 基础设置 ---
st.set_page_config(page_title="StrmWatch Pro", layout="centered")
st.markdown("<style>#MainMenu {visibility: hidden;} footer {visibility: hidden;} header {visibility: hidden;} .block-container {padding-top: 2rem;}</style>", unsafe_allow_html=True)

SRC = os.getenv("SOURCE_DIR", "/源文件夹")
TGT = os.getenv("TARGET_DIR", "/目标文件夹")

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

# --- 逻辑处理 ---
def send_ms(title, content, force=False):
    cfg = st.session_state.runtime_config
    if not force and not cfg["notify_en"]: return
    if not cfg["ms_u"].startswith("http") or not cfg["ms_k"]: return
    try:
        url = f"{cfg['ms_u'].rstrip('/')}/api/v1/message/openSend"
        p = urlparse(url)
        conn = (http.client.HTTPSConnection(p.netloc, timeout=5) if p.scheme == 'https' else http.client.HTTPConnection(p.netloc, timeout=5))
        conn.request("POST", p.path, body=json.dumps({"title": title, "content": content}), headers={'Content-Type': 'application/json', 'apiKey': cfg["ms_k"]})
        conn.getresponse(); conn.close()
        return True
    except: return False

def convert_logic(src_path):
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
            new_c = f"{cfg['ms_u'].rstrip('/')}/api/v1/cloudStorage/strm302?apiKey={cfg['ms_k']}&pickCode=&path={final_path}"
            with open(out, 'w', encoding='utf-8') as f: f.write(new_c)
            return True
    except: pass
    return False

def add_log(msg):
    st.session_state.logs.insert(0, f"[{time.strftime('%H:%M:%S')}] {msg}")
    if len(st.session_state.logs) > 30: st.session_state.logs.pop()

# --- UI 渲染 ---
st.title("🚀 StrmWatch Pro")

# 1. 路径推演器 (支持自动提取 + 手动微调)
with st.expander("🛠️ 路径推演与配置工具", expanded=True):
    st.caption("对比旧/新路径内容，自动推测转换规则。即便文件名不同，程序也会尝试匹配目录。")
    col_l, col_r = st.columns(2)
    old_raw = col_l.text_area("旧 STRM 内容", placeholder="/CloudNAS/115/Movie_A.mkv", height=68)
    new_raw = col_r.text_area("目标 挂载内容", placeholder="/115/Movie_B.mkv", height=68)
    
    if st.button("🔥 推演并应用规则", use_container_width=True):
        if old_raw and new_raw:
            o_s, n_s = old_raw.strip(), new_raw.strip()
            # 改进算法：基于目录深度的推演
            o_parts, n_parts = o_s.split('/'), n_s.split('/')
            if len(o_parts) > 1 and len(n_parts) > 1:
                # 尝试从末尾向前匹配，直到找到不一致的地方
                idx = 1
                while idx < min(len(o_parts), len(n_parts)):
                    if o_parts[-idx] != n_parts[-idx]: break
                    idx += 1
                # 提取前缀
                st.session_state.runtime_config["kw"] = "/".join(o_parts[:-idx+1]) + "/"
                st.session_state.runtime_config["pre"] = "/".join(n_parts[:-idx+1]) + "/"
                st.success("规则推演完成！如不准确可在下方手动修改。")
            else:
                st.error("路径格式不规范，请确保包含完整的斜杠路径。")

# 2. 核心设置 (始终可手动输入)
with st.expander("⚙️ 核心参数设置"):
    c = st.session_state.runtime_config
    c["ms_u"] = st.text_input("Media Saber 地址", value=c["ms_u"])
    c["ms_k"] = st.text_input("API Key", value=c["ms_k"], type="password")
    c["kw"] = st.text_input("旧关键字 (OLD_KEYWORD)", value=c["kw"], help="手动输入需要被替换掉的开头部分")
    c["pre"] = st.text_input("新前缀 (NEW_PREFIX)", value=c["pre"], help="手动输入替换后的开头部分")
    
    col_s1, col_s2 = st.columns(2)
    c["enc"] = col_s1.checkbox("路径 UrlEncode", value=c["enc"])
    c["notify_en"] = col_set2 = col_s2.checkbox("开启通知推送", value=c["notify_en"])
    
    if st.button("🧪 发送测试通知"):
        if send_ms("StrmWatch 测试", "API 通知配置连接成功！", force=True):
            st.toast("通知已发出")
        else: st.error("发送失败")

# 3. 任务区
st.divider()
c_run1, c_run2 = st.columns(2)

if c_run1.button("🏃 立即执行全量扫描", use_container_width=True):
    count, total = 0, 0
    for r, _, fs in os.walk(SRC):
        for f in fs:
            if f.endswith(".strm"):
                total += 1
                if convert_logic(os.path.join(r, f)): count += 1
    
    msg = f"目录：{os.path.basename(SRC)}\n总计：{total} 个\n成功同步：{count} 个"
    add_log(f"📊 全量同步完成，更正 {count} 个文件")
    send_ms("全量同步报告", msg)

if c_run2.button("🧹 清空界面日志", use_container_width=True):
    st.session_state.logs = []
    st.rerun()

st.code("\n".join(st.session_state.logs) if st.session_state.logs else "等待任务中...", language="text")

# --- 监听器 ---
class WatchHandler(FileSystemEventHandler):
    def on_created(self, e):
        if not e.is_directory and convert_logic(e.src_path):
            add_log(f"🆕 自动入库: {os.path.basename(e.src_path)}")
            send_ms("🆕 入库通知", f"检测到新文件并已更正: {os.path.basename(e.src_path)}")
    def on_modified(self, e):
        if not e.is_directory and convert_logic(e.src_path):
            add_log(f"🔄 自动更新: {os.path.basename(e.src_path)}")

if not st.session_state.obs_started and os.path.exists(SRC):
    obs = Observer(); obs.schedule(WatchHandler(), SRC, recursive=True); obs.start()
    st.session_state.obs_started = True
    add_log("🚀 哨兵监听已就绪")

time.sleep(5); st.rerun()