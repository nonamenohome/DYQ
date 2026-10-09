"""Flask 主程序 + 轮询线程 + 下载工作线程。

线程模型：
  - Flask 主线程：只读写 DB、投递队列，不碰网络。
  - poller 线程：每隔 poll_interval_minutes 拉一次投稿，发现新 bvid 入库并投递下载队列。
  - dlworker 线程：单个消费 queue.Queue，同时只下一个（防风控 + 避免写库冲突）。

启动：python app.py
"""
import os
import queue
import threading
import time
from datetime import datetime, timedelta

from flask import Flask, jsonify, request, render_template

import db
import fetcher
import downloader

BASE = os.path.dirname(os.path.abspath(__file__))
CONFIG_PATH = os.path.join(BASE, "config.json")

DEFAULT_CFG = {
    "uid": 0,
    "uids": [],
    "cookie": {"sessdata": "", "bili_jct": "", "buvid3": "", "dedeuserid": ""},
    "quality": 1080,
    "poll_interval_minutes": 10,
    "download_dir": "downloads",
    "port": 5000,
}

app = Flask(__name__)

_cfg = dict(DEFAULT_CFG)
_q = queue.Queue()
_queued = set()          # 已投递/在队的 bvid，避免重复入队
_state = {"last_check": None, "next_check": None}
_poller_enabled = True
_lock = threading.Lock()


def load_config():
    global _cfg
    if os.path.exists(CONFIG_PATH):
        import json
        try:
            with open(CONFIG_PATH, "r", encoding="utf-8") as f:
                user = json.load(f)
            _cfg = {**DEFAULT_CFG, **user}
        except Exception as e:
            print(f"[cfg] {CONFIG_PATH} 解析失败（{e}），改用默认配置", flush=True)
    else:
        print(f"[cfg] 未找到 {CONFIG_PATH}，使用默认配置（不会真正下载）", flush=True)
    return _cfg


def _iso(dt):
    return dt.isoformat(timespec="seconds") if dt else None


def _enqueue(bvid):
    """投递下载，去重。"""
    with _lock:
        if bvid in _queued:
            return False
        _queued.add(bvid)
    _q.put(bvid)
    return True


def _requeue(bvid):
    """下载线程收尾回调：清掉去重标记，让失败/待重试的视频还能再入队。"""
    with _lock:
        _queued.discard(bvid)


def check_once():
    """拉一次投稿并入队。任何异常都吃掉，绝不让轮询线程崩。"""
    now = datetime.now()
    with _lock:
        _state["last_check"] = _iso(now)
    try:
        if not (_cfg.get("uids") or _cfg.get("uid")):
            print("[poll] 未配置 uid/uids，跳过", flush=True)
        else:
            videos = fetcher.fetch_latest(_cfg)
            added = db.insert_videos(videos)
            new_bvids = [v["bvid"] for v in videos]
            db.set_meta("last_check", _iso(now))
            print(f"[poll] {now:%H:%M:%S} 拉到 {len(videos)} 条，新增 {added} 条", flush=True)
            # 新入库的排到队尾；已在库但仍 pending 的也补投一次（幂等，靠 _queued 去重）
            for bvid in db.pending_bvids():
                _enqueue(bvid)
    except Exception as e:
        print(f"[poll] 出错（已忽略，下轮继续）：{e}", flush=True)
    finally:
        interval = int(_cfg.get("poll_interval_minutes") or 10)
        with _lock:
            _state["next_check"] = _iso(datetime.now() + timedelta(minutes=interval))


def poller_loop():
    while True:
        with _lock:
            enabled = _poller_enabled
        if enabled:
            check_once()
        else:
            with _lock:
                _state["next_check"] = None
        interval = int(_cfg.get("poll_interval_minutes") or 10) * 60
        # 分片 sleep，方便将来做立即唤醒；自用足够
        for _ in range(interval):
            time.sleep(1)
            with _lock:
                if not _poller_enabled:
                    break


def dlworker_loop():
    downloader.worker(_cfg, _q, DB_DIR_COOKIE, on_done=_requeue)


# cookies.txt 放在项目目录
DB_DIR_COOKIE = os.path.join(BASE, "cookies.txt")


# ---------------- 路由 ----------------

@app.route("/")
def index():
    return render_template("index.html")


@app.route("/api/status")
def api_status():
    st = db.stats()
    with _lock:
        return jsonify({
            "last_check": _state["last_check"] or db.get_meta("last_check"),
            "next_check": _state["next_check"],
            "poller_enabled": _poller_enabled,
            "video_count": st["total"],
            "up_name": st.get("up_name"),
            "up_names": st.get("up_names"),
            "downloading": st["downloading"],
        })


@app.route("/api/videos")
def api_videos():
    q = request.args.get("q") or None
    status = request.args.get("status") or "all"
    order = request.args.get("order") or "pubdate_desc"
    return jsonify(db.list_videos(q=q, status=status, order=order))


@app.route("/api/download/<bvid>", methods=["POST"])
def api_download(bvid):
    v = db.get_video(bvid)
    if not v:
        return jsonify({"ok": False, "error": "bvid 不存在"}), 404
    if v["status"] == "done":
        return jsonify({"ok": False, "error": "已下载"})
    db.set_status(bvid, "pending")
    if not _enqueue(bvid):
        return jsonify({"ok": False, "error": "已在下载队列中"})
    return jsonify({"ok": True})


@app.route("/api/refresh", methods=["POST"])
def api_refresh():
    # 后台触发一次检查，接口本身立即返回
    threading.Thread(target=check_once, daemon=True).start()
    return jsonify({"ok": True})


@app.route("/api/progress")
def api_progress():
    return jsonify(downloader.progress_rows())


def main():
    load_config()
    db.init()
    db.reset_downloading()
    os.makedirs(os.path.abspath(_cfg.get("download_dir") or "downloads"), exist_ok=True)

    threading.Thread(target=poller_loop, daemon=True).start()
    threading.Thread(target=dlworker_loop, daemon=True).start()

    port = int(_cfg.get("port") or 5000)
    print(f"[web] http://127.0.0.1:{port}  (uid={_cfg.get('uid')}, 间隔={_cfg.get('poll_interval_minutes')}min)", flush=True)
    app.run(host="127.0.0.1", port=port, debug=False)


if __name__ == "__main__":
    main()
