#!/usr/bin/env python3
"""
模型下载管理工具 - Flask 后端
提供双页签功能：
  - 页签一：搜索已汇总模型（favorite_list.json）
  - 页签二：从指定网站搜索模型并收藏
"""

import json
import os
import sys
import hashlib
import re
import subprocess
import threading
import time
import uuid
from datetime import datetime, timedelta
from pathlib import Path

# 将当前目录加入 sys.path，以便导入 get-model-lists
# PyInstaller: sys._MEIPASS = 只读资源目录, exe 所在目录 = 可写
_BUNDLE_DIR = Path(getattr(sys, '_MEIPASS', Path(__file__).parent.absolute()))
_USER_DIR = Path(sys.executable).parent.absolute() if getattr(sys, 'frozen', False) else _BUNDLE_DIR
sys.path.insert(0, str(_BUNDLE_DIR))

# HF 镜像 + SSL 修复
os.environ["HF_ENDPOINT"] = "https://hf-mirror.com"

from flask import Flask, request, jsonify, send_from_directory

# ---- 导入搜索模块（文件名含连字符，通过文件路径加载） ----
import importlib.util
_searcher_spec = importlib.util.spec_from_file_location(
    "model_searcher", _BUNDLE_DIR / "get-model-lists.py"
)
_model_searcher = importlib.util.module_from_spec(_searcher_spec)
_searcher_spec.loader.exec_module(_model_searcher)
search_repos = _model_searcher.search_repos

app = Flask(__name__, static_folder=str(_BUNDLE_DIR / "static"), static_url_path="/static")

@app.route("/api/clipboard", methods=["POST"])
def api_clipboard():
    """把文本复制到系统剪贴板"""
    import subprocess as _sp
    data = request.get_json(silent=True) or {}
    text = data.get("text", "")
    if text:
        _sp.run(['powershell', '-Command', f"Set-Clipboard -Value '{text}'"], capture_output=True)
    return jsonify({"ok": True})

@app.errorhandler(500)
def handle_500(e):
    return jsonify({"ok": False, "error": "服务器内部错误"}), 500

# ---- 路径配置 ----
MODEL_JSON_FILE = _USER_DIR / "favorite_list.json"
TEMP_DIR = _USER_DIR / "temp"
DOWNLOAD_SCRIPT = _BUNDLE_DIR / "download_models.ps1"
HF_MIRROR = "https://hf-mirror.com"
TEMP_DIR.mkdir(exist_ok=True)

# ---- 下载任务状态 ----
_downloads = {}
_download_lock = threading.Lock()

# ---- 下载队列 ----
# [{id, model, status: waiting|downloading|done|failed, progress:0-100, current_size, error, added_at}]
_queue = []
_queue_lock = threading.Lock()
_queue_worker_running = False


# ====================== 工具函数 ======================

def _read_model_json() -> list:
    """读取 favorite_list.json，返回模型列表"""
    if not MODEL_JSON_FILE.exists():
        return []
    with open(MODEL_JSON_FILE, "r", encoding="utf-8") as f:
        return json.load(f)


def _write_model_json(models: list):
    """写入 favorite_list.json"""
    with open(MODEL_JSON_FILE, "w", encoding="utf-8") as f:
        json.dump(models, f, indent=4, ensure_ascii=False)


def _search_cache_or_fetch(repos: list) -> (list, bool):
    """直接搜索，不使用缓存"""
    all_models = []
    for url in repos:
        url = url.strip()
        if not url:
            continue
        try:
            models = search_repos([url])
        except Exception as e:
            print(f"[ERROR] 搜索 {url} 失败: {e}")
            models = []
        all_models.extend(models)
    return all_models, False


# ====================== 路由 ======================

@app.route("/")
def index():
    return send_from_directory("static", "index.html")


# ---- 用户偏好（替代 localStorage） ----
_PREFS_FILE = _USER_DIR / "user_prefs.json"
_prefs_lock = threading.Lock()

def _read_prefs():
    if _PREFS_FILE.exists():
        try:
            with open(_PREFS_FILE, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            pass
    return {}

def _write_prefs(data: dict):
    with open(_PREFS_FILE, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False)


@app.route("/api/prefs/get")
def api_prefs_get():
    with _prefs_lock:
        return jsonify({"ok": True, "prefs": _read_prefs()})


@app.route("/api/prefs/save", methods=["POST"])
def api_prefs_save():
    data = request.get_json(silent=True) or {}
    key = data.get("key", "")
    val = data.get("value")
    with _prefs_lock:
        prefs = _read_prefs()
        prefs[key] = val
        _write_prefs(prefs)
    return jsonify({"ok": True})


# ---- 页签一：已汇总模型 ----

@app.route("/api/tab1/count")
def api_tab1_count():
    """获取模型总数（不加载数据）"""
    try:
        models = _read_model_json()
        return jsonify({"ok": True, "total": len(models)})
    except Exception as e:
        return jsonify({"ok": False, "total": 0}), 500


@app.route("/api/tab1/models")
def api_tab1_models():
    """获取 favorite_list.json 中所有模型，支持搜索过滤"""
    search = request.args.get("search", "").strip().lower()
    source = request.args.get("source", "").strip().lower()
    try:
        models = _read_model_json()
    except Exception as e:
        return jsonify({"ok": False, "error": f"读取模型列表失败: {e}"}), 500

    if source:
        models = [m for m in models if m.get("source", "").lower() == source]
    if search:
        models = [m for m in models if search in m.get("filename", "").lower()]

    # 只返回展示需要的字段 + 关键字段
    result = []
    for m in models:
        result.append({
            "source": m.get("source", ""),
            "repo": m.get("repo", ""),
            "filename": m.get("filename", ""),
            "url": m.get("url", ""),
            "path": m.get("path", ""),
            "sha256": m.get("sha256", ""),
            "size": m.get("size", ""),
            "favorited_at": m.get("favorited_at", ""),
        })

    return jsonify({"ok": True, "models": result, "total": len(result)})


# ---- 页签二：网站搜索 ----

@app.route("/api/tab2/search", methods=["POST"])
def api_tab2_search():
    """搜索指定 repos 链接下的模型文件"""
    data = request.get_json(silent=True) or {}
    repos = data.get("repos", [])
    force_refresh = data.get("force_refresh", False)

    if not repos:
        return jsonify({"ok": False, "error": "请至少输入一个仓库链接"}), 400

    # 过滤空行
    repos = [r.strip() for r in repos if r.strip()]

    try:
        models, all_cached = _search_cache_or_fetch(repos)
    except Exception as e:
        return jsonify({"ok": False, "error": f"搜索失败: {e}"}), 500

    # 只返回展示需要的字段
    result = []
    for m in models:
        result.append({
            "source": m.get("source", ""),
            "repo": m.get("repo", ""),
            "filename": m.get("filename", ""),
            "url": m.get("url", ""),
            "path": m.get("path", ""),
            "sha256": m.get("sha256", ""),
            "size": m.get("size", ""),
        })

    return jsonify({
        "ok": True,
        "models": result,
        "total": len(result),
        "from_cache": all_cached,
    })


# ---- 收藏模型 ----

@app.route("/api/favorite", methods=["POST"])
def api_favorite():
    """收藏模型到 favorite_list.json（去重）"""
    data = request.get_json(silent=True) or {}
    model = data.get("model")
    if not model:
        return jsonify({"ok": False, "error": "缺少模型数据"}), 400

    try:
        models = _read_model_json()
    except Exception as e:
        return jsonify({"ok": False, "error": f"读取模型列表失败: {e}"}), 500

    # 去重：相同 filename + sha256 + url 不重复添加
    new_key = (model.get("filename", ""), model.get("sha256", ""), model.get("url", ""))
    for existing in models:
        ek = (existing.get("filename", ""), existing.get("sha256", ""), existing.get("url", ""))
        if new_key == ek:
            return jsonify({"ok": True, "message": "模型已存在，无需重复收藏", "duplicate": True})

    model["favorited_at"] = datetime.now().strftime("%Y-%m-%d %H:%M")
    models.append(model)
    models.sort(key=lambda x: x.get("filename", ""))

    try:
        _write_model_json(models)
    except Exception as e:
        return jsonify({"ok": False, "error": f"写入模型列表失败: {e}"}), 500

    return jsonify({"ok": True, "message": f"已收藏: {model.get('filename')}", "duplicate": False})


@app.route("/api/favorite/batch", methods=["POST"])
def api_favorite_batch():
    """批量收藏模型到 favorite_list.json（去重）"""
    data = request.get_json(silent=True) or {}
    new_models = data.get("models", [])
    if not new_models:
        return jsonify({"ok": False, "error": "缺少模型数据"}), 400

    try:
        existing = _read_model_json()
    except Exception as e:
        return jsonify({"ok": False, "error": f"读取模型列表失败: {e}"}), 500

    existing_keys = {(m.get("filename", ""), m.get("sha256", ""), m.get("url", "")) for m in existing}
    added = 0
    now = datetime.now().strftime("%Y-%m-%d %H:%M")
    for m in new_models:
        key = (m.get("filename", ""), m.get("sha256", ""), m.get("url", ""))
        if key not in existing_keys:
            m["favorited_at"] = now
            existing.append(m)
            existing_keys.add(key)
            added += 1

    existing.sort(key=lambda x: x.get("filename", ""))
    try:
        _write_model_json(existing)
    except Exception as e:
        return jsonify({"ok": False, "error": f"写入失败: {e}"}), 500

    return jsonify({"ok": True, "added": added, "skipped": len(new_models) - added, "total": len(existing)})


@app.route("/api/unfavorite", methods=["DELETE"])
def api_unfavorite():
    """取消收藏"""
    data = request.get_json(silent=True) or {}
    filename = data.get("filename", "")
    sha256 = data.get("sha256", "")
    url = data.get("url", "")
    try:
        models = _read_model_json()
    except Exception as e:
        return jsonify({"ok": False, "error": f"读取失败: {e}"}), 500
    key = (filename, sha256, url)
    before = len(models)
    models = [m for m in models if (m.get("filename",""), m.get("sha256",""), m.get("url","")) != key]
    if len(models) == before:
        return jsonify({"ok": False, "error": "未找到"}), 404
    _write_model_json(models)
    return jsonify({"ok": True, "total": len(models)})


# ---- 下载队列 ----

def _download_model(item: dict):
    """下载单个模型"""
    qid = item["id"]
    model = item["model"]
    temp_file = TEMP_DIR / f"queue_{qid}.json"
    log_file = TEMP_DIR / f"queue_{qid}.log"
    with open(temp_file, "w", encoding="utf-8") as f:
        json.dump([model], f, indent=2, ensure_ascii=False)

    base_dir = model.get("baseDir", str(_USER_DIR))
    try:
        base_dir = os.path.abspath(base_dir)
    except Exception:
        base_dir = str(_USER_DIR)

    ps_cmd = [
        "powershell.exe", "-NoProfile", "-ExecutionPolicy", "Bypass",
        "-File", str(DOWNLOAD_SCRIPT), "-BaseDir", base_dir, "-ModelFile", str(temp_file),
    ]
    if HF_MIRROR:
        ps_cmd += ["-HfMirror", HF_MIRROR]

    try:
        with open(log_file, "w", encoding="utf-8", buffering=1) as lf:
            proc = subprocess.Popen(ps_cmd, stdout=lf, stderr=subprocess.STDOUT, cwd=str(_USER_DIR), creationflags=subprocess.CREATE_NO_WINDOW)
            item["_proc"] = proc

        read_pos = 0
        _phase = "prealloc"
        _last_progress = time.time()
        _last_pct = -1

        def _poll():
            nonlocal read_pos, _phase, _last_progress, _last_pct
            while proc.poll() is None:
                try:
                    if log_file.exists():
                        with open(log_file, "r", encoding="utf-8", errors="replace") as lf:
                            lf.seek(read_pos)
                            tail = lf.read()
                            read_pos = lf.tell()
                        if "[FileAlloc:" in tail:
                            _phase = "prealloc"; item["phase"] = "prealloc"
                        if "[Checksum:" in tail:
                            _phase = "verifying"; item["phase"] = "verifying"
                        m = re.findall(r"FileAlloc.*?\((\d+)%\)", tail)
                        if m and _phase == "prealloc":
                            item["progress"] = int(m[-1])
                        m = re.findall(r"Checksum.*?\((\d+)%\)", tail)
                        if m and _phase == "verifying":
                            item["progress"] = int(m[-1])
                        m = re.findall(r"(\d+)%\).*DL:([\d.]+[KMGT]?iB)(?:.*ETA:(\S+))?", tail)
                        if m and _phase in ("downloading", "prealloc"):
                            _phase = "downloading"; item["phase"] = "downloading"
                            pct = int(m[-1][0]); item["progress"] = pct
                            item["speed"] = m[-1][1]
                            item["eta"] = m[-1][2].rstrip("]") if m[-1][2] else ""
                            if pct != _last_pct:
                                _last_pct = pct; _last_progress = time.time()
                        m = re.findall(r"\[(\d+)/(\d+)\]\s*下载[：:]\s*(.+)", tail)
                        if m:
                            item["current_file"] = m[-1][2].strip()
                            item["total_files"] = int(m[-1][1])
                        if "文件完整，跳过下载" in tail:
                            item["phase"] = "skipped"
                except Exception:
                    pass
                if _phase in ("downloading", "prealloc") and time.time() - _last_progress > 30:
                    proc.kill()
                    item["status"] = "failed"
                    item["error"] = "下载卡死（30秒无进度）"
                    return
                time.sleep(1.5)

        pinger = threading.Thread(target=_poll, daemon=True)
        pinger.start()
        proc.wait()
        pinger.join(timeout=5)

        if proc.returncode == 0:
            item["status"] = "done"; item["phase"] = "done"; item["progress"] = 100
        else:
            item["status"] = "failed"; item["phase"] = "failed"
            item["error"] = f"退出码: {proc.returncode}"
    except Exception as e:
        item["status"] = "failed"; item["error"] = str(e)
    finally:
        if temp_file.exists():
            try: temp_file.unlink()
            except Exception: pass
        if item["status"] in ("done",) and log_file.exists():
            try: log_file.unlink()
            except Exception: pass


def _queue_worker():
    """后台线程：逐个处理下载队列"""
    global _queue_worker_running
    _queue_worker_running = True
    while True:
        with _queue_lock:
            item = next((q for q in _queue if q["status"] == "waiting"), None)
            if item:
                item["status"] = "downloading"
        if item is None:
            break
        _download_model(item)
    _queue_worker_running = False



def _ensure_queue_running():
    """确保队列处理线程在运行"""
    with _queue_lock:
        waiting = any(q["status"] == "waiting" for q in _queue)
    if waiting and not _queue_worker_running:
        threading.Thread(target=_queue_worker, daemon=True).start()


@app.route("/api/queue/add", methods=["POST"])
def api_queue_add():
    """将一个或多个模型加入下载队列"""
    data = request.get_json(silent=True) or {}
    models = data.get("models", [])
    if not models:
        return jsonify({"ok": False, "error": "缺少模型数据"}), 400

    added = 0
    skipped = 0
    with _queue_lock:
        existing_keys = {(q["model"].get("filename",""), q["model"].get("sha256","")) for q in _queue}
        for m in models:
            key = (m.get("filename",""), m.get("sha256",""))
            if key in existing_keys:
                skipped += 1
                continue
            existing_keys.add(key)
            if "baseDir" not in m:
                m["baseDir"] = str(_USER_DIR)
            _queue.append({
                "id": uuid.uuid4().hex[:10],
                "model": m,
                "status": "waiting",
                "progress": 0,
                "current_size": "",
                "error": "",
                "added_at": datetime.now().isoformat(),
            })
            added += 1

    msg = f"已加入 {added} 个模型"
    if skipped: msg += f"，跳过 {skipped} 个重复"
    return jsonify({"ok": True, "message": msg, "queue_len": len(_queue)})


@app.route("/api/queue/start", methods=["POST"])
def api_queue_start():
    """批量下载——将 waiting 模型传给 PS 一次性执行"""
    with _queue_lock:
        waiting = [q for q in _queue if q["status"] == "waiting"]
        if not waiting: return jsonify({"ok": False, "error": "没有待下载的模型"}), 400
        for q in waiting: q["status"] = "downloading"; q["progress"] = 0; q["error"] = ""

    models = [q["model"] for q in waiting]
    temp_file = TEMP_DIR / f"batch_{uuid.uuid4().hex[:8]}.json"
    with open(temp_file, "w", encoding="utf-8") as f:
        json.dump(models, f, indent=2, ensure_ascii=False)

    ps_cmd = [
        "powershell.exe", "-NoProfile", "-ExecutionPolicy", "Bypass",
        "-File", str(DOWNLOAD_SCRIPT), "-BaseDir", str(_USER_DIR),
        "-ModelFile", str(temp_file),
    ]
    if HF_MIRROR: ps_cmd += ["-HfMirror", HF_MIRROR]

    def _run():
        try:
            proc = subprocess.Popen(ps_cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                cwd=str(_USER_DIR), creationflags=subprocess.CREATE_NO_WINDOW)
            proc.wait()
            ok = proc.returncode == 0
        except Exception: ok = False
        with _queue_lock:
            for q in waiting: q["status"] = "done" if ok else "failed"; q["error"] = "" if ok else "下载异常"
        if temp_file.exists():
            try: temp_file.unlink()
            except Exception: pass

    threading.Thread(target=_run, daemon=True).start()
    return jsonify({"ok": True, "message": f"开始下载 {len(waiting)} 个模型"})


def _parse_size_bytes(s: str) -> int:
    """把 '1.14GB' / '500MiB' 等转为字节数"""
    m = re.match(r'([\d.]+)\s*(GB|GiB|MB|MiB|KB|KiB|B)?', str(s), re.I)
    if not m: return 0
    v, u = float(m.group(1)), (m.group(2) or 'B').upper()
    return int(v * {'B':1,'KB':1024,'MB':1024**2,'GB':1024**3,'GIB':1024**3,'MIB':1024**2,'KIB':1024}.get(u, 1))


@app.route("/api/queue/status")
def api_queue_status():
    """获取队列状态——下载中的模型通过文件大小计算进度"""
    items = []
    with _queue_lock:
        for q in _queue:
            m = q["model"]
            pct = 0; cur = ""
            if q["status"] == "downloading":
                target_bytes = _parse_size_bytes(m.get("size", ""))
                if target_bytes > 0:
                    fp = Path(m.get("baseDir", str(_USER_DIR))) / m.get("path", "")
                    aria2_fp = Path(str(fp) + ".aria2")
                    if fp.exists():
                        actual = fp.stat().st_size
                        if actual >= target_bytes and not aria2_fp.exists():
                            q["status"] = "done"; pct = 100
                        else:
                            pct = 100 if actual >= target_bytes else int(actual / target_bytes * 100)
                            cur = f"{actual/1024**3:.1f}GB" if actual > 1024**3 else f"{actual/1024**2:.0f}MB"
            elif q["status"] == "done": pct = 100
            items.append({
                "id": q["id"], "filename": m.get("filename",""), "size": m.get("size",""),
                "source": m.get("source",""), "path": m.get("path",""),
                "status": q["status"], "progress": pct, "current_size": cur,
                "error": q.get("error",""), "added_at": q.get("added_at",""),
            })
    return jsonify({"ok": True, "items": items})


@app.route("/api/queue/remove", methods=["DELETE"])
def api_queue_remove():
    """从队列中移除一项（只能移除 waiting 或 done/failed 的）"""
    qid = request.args.get("id", "")
    with _queue_lock:
        for i, q in enumerate(_queue):
            if q["id"] == qid and q["status"] != "downloading":
                _queue.pop(i)
                return jsonify({"ok": True})
    return jsonify({"ok": False, "error": "未找到或正在下载中，无法移除"}), 400


@app.route("/api/open-folder", methods=["POST"])
def api_open_folder():
    """在资源管理器中打开指定文件夹"""
    data = request.get_json(silent=True) or {}
    folder = data.get("path", "")
    if not folder:
        return jsonify({"ok": False, "error": "缺少路径"}), 400
    try:
        folder_path = _USER_DIR / folder
        # 取父目录（文件可能不存在但父目录存在）
        target = str(folder_path.parent)
        if not os.path.isdir(target):
            return jsonify({"ok": False, "error": "路径不存在"}), 404
        os.startfile(target)
        # 等窗口打开后强制提到前台（Explorer 标题是文件夹名，非完整路径）
        import time as _t, ctypes as _c
        _t.sleep(0.8)
        name = os.path.basename(target)
        h = _c.windll.user32.FindWindowW(None, name)
        if h: _c.windll.user32.SetForegroundWindow(h)
        return jsonify({"ok": True})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500


@app.route("/api/queue/cancel", methods=["POST"])
def api_queue_cancel():
    """取消正在下载的任务"""
    qid = request.args.get("id", "")
    with _queue_lock:
        for q in _queue:
            if q["id"] == qid and q["status"] == "downloading":
                proc = q.get("_proc")
                if proc:
                    try:
                        proc.kill()
                    except Exception:
                        pass
                # 删除下载未完成的文件 + .aria2 控制文件
                m = q.get("model", {})
                fp = Path(m.get("baseDir", str(_USER_DIR))) / m.get("path", "")
                for f in (fp, Path(str(fp) + ".aria2")):
                    if f.exists():
                        try: f.unlink()
                        except Exception: pass
                q["status"] = "failed"
                q["error"] = "已取消"
                return jsonify({"ok": True})
    return jsonify({"ok": False, "error": "未找到可取消的任务"}), 400


@app.route("/api/queue/clear", methods=["DELETE"])
def api_queue_clear():
    """清除队列中所有已完成/失败的项，保留等待和下载中的"""
    with _queue_lock:
        global _queue
        _queue = [q for q in _queue if q["status"] in ("waiting", "downloading")]
    return jsonify({"ok": True, "queue_len": len(_queue)})


# ---- 下载（批量，保留兼容） ----

def _download_worker(download_id: str, models: list, base_dir: str, hf_mirror: str = "", hf_token: str = ""):
    """后台下载线程 — PIPE + 线程读取，实时写入日志文件"""
    log_file = TEMP_DIR / f"download_{download_id}.log"
    task_info = _downloads.get(download_id)
    if not task_info:
        return

    def _log(msg: str):
        """写日志到文件和内存"""
        timestamp = datetime.now().strftime("%H:%M:%S")
        line = f"[{timestamp}] {msg}"
        print(line)  # 服务器控制台可见
        try:
            with open(log_file, "a", encoding="utf-8") as lf:
                lf.write(line + "\n")
        except Exception:
            pass
        with _download_lock:
            if download_id in _downloads:
                _downloads[download_id]["log"].append(line)

    try:
        with _download_lock:
            _downloads[download_id]["running"] = True

        # 为每个模型设置 baseDir
        for m in models:
            if "baseDir" not in m:
                m["baseDir"] = base_dir or str(_USER_DIR)

        # 按 baseDir 分组
        groups: dict = {}
        for m in models:
            bd = os.path.abspath(m.get("baseDir", str(_USER_DIR)))
            groups.setdefault(bd, []).append(m)

        total = len(models)
        completed = 0

        _log(f"开始下载 {total} 个模型（{len(groups)} 组）")

        for group_base_dir, group_models in groups.items():
            # 为每组创建临时 JSON 文件
            temp_file = TEMP_DIR / f"download_{download_id}_{hashlib.md5(group_base_dir.encode()).hexdigest()[:8]}.json"
            with open(temp_file, "w", encoding="utf-8") as f:
                json.dump(group_models, f, indent=2, ensure_ascii=False)

            _log(f"启动下载: {len(group_models)} 个模型 -> {group_base_dir}")

            # PowerShell 命令：输出重定向到日志文件
            ps_cmd = [
                "powershell.exe",
                "-NoProfile",
                "-ExecutionPolicy", "Bypass",
                "-File", str(DOWNLOAD_SCRIPT),
                "-BaseDir", group_base_dir,
                "-ModelFile", str(temp_file),
            ]
            if hf_mirror:
                ps_cmd += ["-HfMirror", hf_mirror]
            if hf_token:
                ps_cmd += ["-HfToken", hf_token]

            # 使用 Popen + 文件描述符直写，实时输出到日志文件
            try:
                # PIPE + 独立线程读取，避免 PS 缓冲导致日志延迟
                proc = subprocess.Popen(
                    ps_cmd,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.STDOUT,
                    text=True,
                    errors="replace",
                    cwd=str(_USER_DIR),
                )

                with _download_lock:
                    _downloads[download_id]["process"] = proc

                def _reader():
                    try:
                        with open(log_file, "a", encoding="utf-8", buffering=1) as lf:
                            for line in proc.stdout:
                                lf.write(line)
                                lf.flush()
                    except Exception:
                        pass

                reader = threading.Thread(target=_reader, daemon=True)
                reader.start()
                proc.wait()
                reader.join(timeout=10)

                if proc.returncode == 0:
                    completed += len(group_models)
                    _log(f"[OK] 分组下载完成 ({group_base_dir})")
                else:
                    _log(f"[FAIL] 分组下载失败，退出码: {proc.returncode} ({group_base_dir})")

            except FileNotFoundError:
                _log(f"[ERROR] 找不到 powershell.exe，请检查系统环境")
            except Exception as e:
                _log(f"[ERROR] 下载异常: {e} ({group_base_dir})")
            finally:
                # 清理临时文件
                if temp_file.exists():
                    try:
                        temp_file.unlink()
                    except Exception:
                        pass

        _log(f"[DONE] 全部下载完成: {completed}/{total}")

        with _download_lock:
            if download_id in _downloads:
                _downloads[download_id]["completed"] = completed
                _downloads[download_id]["total"] = total

    except Exception as e:
        _log(f"[FATAL] 下载线程异常: {e}")
        import traceback
        _log(traceback.format_exc())
    finally:
        # 清理临时日志文件
        try:
            if log_file.exists():
                log_file.unlink()
        except Exception:
            pass
        with _download_lock:
            if download_id in _downloads:
                _downloads[download_id]["running"] = False
                _downloads[download_id]["finished"] = True


@app.route("/api/download/start", methods=["POST"])
def api_download_start():
    """启动下载任务"""
    data = request.get_json(silent=True) or {}
    models = data.get("models", [])
    base_dir = data.get("baseDir", str(_USER_DIR))
    hf_mirror = data.get("hfMirror", "") or HF_MIRROR
    hf_token = data.get("hfToken", "")

    if not models:
        return jsonify({"ok": False, "error": "请选择要下载的模型"}), 400

    # 检查是否有正在运行的任务
    with _download_lock:
        for did, info in _downloads.items():
            if info.get("running"):
                return jsonify({"ok": False, "error": "已有下载任务正在运行，请等待完成"}), 409

    download_id = uuid.uuid4().hex[:12]

    with _download_lock:
        _downloads[download_id] = {
            "running": False,
            "finished": False,
            "process": None,
            "started": datetime.now().isoformat(),
            "log": [],
            "completed": 0,
            "total": len(models),
        }

    thread = threading.Thread(
        target=_download_worker,
        args=(download_id, models, base_dir, hf_mirror, hf_token),
        daemon=True,
    )
    thread.start()

    # 等待线程开始
    time.sleep(0.5)

    return jsonify({"ok": True, "download_id": download_id, "total": len(models)})


@app.route("/api/download/status")
def api_download_status():
    """查询下载任务状态，优先从文件日志读取最新内容"""
    download_id = request.args.get("id", "")

    with _download_lock:
        if download_id and download_id in _downloads:
            info = dict(_downloads[download_id])
            info["download_id"] = download_id
        elif not download_id and _downloads:
            latest_id = list(_downloads.keys())[-1]
            info = dict(_downloads[latest_id])
            info["download_id"] = latest_id
        else:
            return jsonify({"ok": True, "running": False, "finished": False, "log": []})

    # 从文件日志读取最新内容（比内存更可靠）
    log_file = TEMP_DIR / f"download_{info.get('download_id', download_id)}.log"
    file_logs = []
    if log_file.exists():
        try:
            with open(log_file, "r", encoding="utf-8") as lf:
                file_logs = [line.rstrip() for line in lf.readlines() if line.strip()]
        except Exception:
            pass

    # 合并日志：文件为主（更完整），内存为补充
    mem_logs = info.get("log", [])
    combined = file_logs if len(file_logs) >= len(mem_logs) else mem_logs

    return jsonify({
        "ok": True,
        "running": info.get("running", False),
        "finished": info.get("finished", False),
        "completed": info.get("completed", 0),
        "total": info.get("total", 0),
        "started": info.get("started", ""),
        "log": combined[-80:],  # 最近 80 行
    })


# ====================== 启动 ======================

if __name__ == "__main__":
    print("=" * 50)
    print("  模型下载管理工具")
    print(f"  模型数据库: {MODEL_JSON_FILE}")
    print(f"  临时目录:   {TEMP_DIR}")
    print("=" * 50)
    print()
    print("  启动服务器: http://127.0.0.1:5000")
    print("  按 Ctrl+C 停止")
    print()

    app.run(host="127.0.0.1", port=5000, debug=True)
