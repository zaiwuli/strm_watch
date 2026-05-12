import os
import time
import json
import http.client
import logging
from datetime import datetime
from urllib.parse import urlparse, quote
from threading import Thread
import streamlit as st
from watchdog.observers import Observer
from watchdog.events import FileSystemEventHandler

# ================= 页面与日志配置 =================
st.set_page_config(page_title="STRM Watch 控制台", page_icon="🚀", layout="wide")

if 'logs' not in st.session_state:
    st.session_state.logs = []
if 'obs_started' not in st.session_state:
    st.session_state.obs_started = False

# ================= UI 侧边栏：参数配置 =================
with st.sidebar:
    st.header("⚙️ 参数配置")
    
    # 基础配置 (从环境变量获取初始值)
    ms_url = st.text_input("Media Saber 地址", value=os.getenv("MS_URL", "https://"))
    ms_key = st.text_input("Media Saber API Key", value=os.getenv("MS_API_KEY", ""), type="password")
    
    st.divider()
    
    old_kw = st.text_input("旧路径关键字", value=os.getenv("OLD_KEYWORD", ""))
    new_prefix = st.text_input("新路径前缀", value=os.getenv("NEW_MOUNT_PREFIX", "/"))
    
    encode_on = st.toggle("路径 UrlEncode 编码", value=True)
    
    st.divider()
    
    # 物理映射路径 (只读显示)
    st.caption("📂 物理映射路径 (只读)")
    src_dir = os.getenv("SOURCE_DIR", "/源文件夹")
    tgt_dir = os.getenv("TARGET_DIR", "/目标文件夹")
    st.text(f"源: {src_dir}")
    st.text(f"目: {tgt_dir}")

# ================= 逻辑处理函数 =================

def send_ms_notification(title, content):
    """发送 MS 通知 (带重试逻辑)"""
    if not ms_url.startswith("http") or not ms_key: return
    
    # 尝试 3 次发送
    for attempt in range(3):
        try:
            url = f"{ms_url.rstrip('/')}/api/v1/message/openSend"
            p = urlparse(url)
            payload = json.dumps({"title": title, "content": content})
            headers = {'Content-Type': 'application/json', 'apiKey': ms_key}
            conn = (http.client.HTTPSConnection(p.netloc, timeout=5) if p.scheme == 'https' 
                    else http.client.HTTPConnection(p.netloc, timeout=5))
            conn.request("POST", p.path, body=payload, headers=headers)
            conn.getresponse(); conn.close()
            return # 发送成功
        except:
            time.sleep(2)

def process_file(src_path):
    """核心替换逻辑"""
    if not src_path.endswith(".strm"): return False
    try:
        rel_path = os.path.relpath(src_path, src_dir)
        target_path = os.path.join(tgt_dir, rel_path)
        os.makedirs(os.path.dirname(target_path), exist_ok=True)
        
        with open(src_path, 'r', encoding='utf-8') as f:
            content = f.read().strip()
            
        if old_kw and old_kw in content:
            path_part = content.split(old_kw)[-1].lstrip('/')
            final_path = (new_prefix.rstrip('/') + '/' + path_part).replace('\\', '/')
            if encode_on: final_path = quote(final_path, safe='/')
            
            new_c = f"{ms_url.rstrip('/')}/api/v1/cloudStorage/strm302?apiKey={ms_key}&pickCode=&path={final_path}"
            
            with open(target_path, 'w', encoding='utf-8') as f:
                f.write(new_c)
            return True
    except: pass
    return False

def manual_replace_tool(raw_text):
    """手动转换工具逻辑"""
    if not raw_text: return None
    if old_kw and old_kw in raw_text:
        path_part = raw_text.split(old_kw)[-1].lstrip('/')
        final_path = (new_prefix.rstrip('/') + '/' + path_part).replace('\\', '/')
        if encode_on: final_path = quote(final_path, safe='/')
        return f"{ms_url.rstrip('/')}/api/v1/cloudStorage/strm302?apiKey={ms_key}&pickCode=&path={final_path}"
    return "⚠️ 未匹配到关键字"

# ================= 主界面 UI =================

st.title("🚀 STRM Watch 自动化哨兵")

# 1. 状态卡片
col1, col2, col3 = st.columns(3)
col1.metric("待处理源目录", os.path.basename(src_dir))
col2.metric("已处理目标目录", os.path.basename(tgt_dir))
col3.metric("通知状态", "✅ 已就绪" if ms_key else "❌ 未配置")

# 2. 手动工具箱 (Expander)
with st.expander("🛠️ 手动路径转换工具"):
    st.caption("粘贴 strm 文件内的原始内容，快速生成修复后的 API 链接")
    test_input = st.text_input("原始路径内容", placeholder="例如: /CloudNAS/CloudDrive/115open/电影.mkv")
    if test_input:
        res = manual_replace_tool(test_input)
        if "http" in str(res):
            st.success("转换成功！")
            st.code(res, language="text")
        else:
            st.warning(res)

# 3. 日志与操作区
st.divider()
st.subheader("📋 实时同步日志")
log_box = st.empty()

def update_log(msg):
    t = time.strftime("%H:%M:%S", time.localtime())
    st.session_state.logs.insert(0, f"[{t}] {msg}")
    if len(st.session_state.logs) > 50: st.session_state.logs.pop()
    log_box.code("\n".join(st.session_state.logs))

if st.button("🔄 执行全量扫描"):
    if not old_kw or not ms_key:
        st.error("请先完成参数配置")
    else:
        count = 0
        with st.spinner("处理中..."):
            for root, _, files in os.walk(src_dir):
                for f in files:
                    if process_file(os.path.join(root, f)): count += 1
        update_log(f"📊 全量扫描完成，同步文件: {count}")
        if count > 0: send_ms_notification("📊 同步报告", f"初始扫描已处理 {count} 个文件")

# ================= 后台监控线程 =================

class WatcherHandler(FileSystemEventHandler):
    def on_created(self, event):
        if not event.is_directory and process_file(event.src_path):
            update_log(f"🆕 入库: {os.path.basename(event.src_path)}")
            send_ms_notification("🆕 实时同步通知", f"入库：{os.path.basename(event.src_path)}")
    def on_modified(self, event):
        if not event.is_directory and process_file(event.src_path):
            update_log(f"🔄 更正: {os.path.basename(event.src_path)}")
            send_ms_notification("🔄 内容更正通知", f"更正：{os.path.basename(event.src_path)}")

if not st.session_state.obs_started:
    observer = Observer()
    observer.schedule(WatcherHandler(), src_dir, recursive=True)
    observer.start()
    st.session_state.obs_started = True
    update_log("🚀 哨兵系统已上线，实时监听中...")

# 实时显示历史日志
log_box.code("\n".join(st.session_state.logs))