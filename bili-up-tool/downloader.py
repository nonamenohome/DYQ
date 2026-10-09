"""下载工作线程：用 yt-dlp 的 Python API 单个消费队列，同时只下一个。

- cookie：从 config 的 cookie 字段生成 Netscape 格式 cookies.txt 给 yt-dlp。
- 格式：bv*[height<=1080]+ba/b[height<=1080]，靠 ffmpeg 合并。
- 进度：progress_hooks 拿百分比 / 速度 / ETA，写内存字典供 /api/progress 读。
"""
import os
import re
import json
import time
import queue
import threading
from datetime import datetime

import yt_dlp

import db

# 非法字符清洗：\/:*?"<>| 以及控制字符
_ILLEGAL = re.compile(r'[\\/:*?"<>|\x00-\x1f]')
# 进度表 {bvid: {...}}，只在下载线程写、Flask 线程读（dict 单写多读，够自用）
PROGRESS = {}
_plock = threading.Lock()


def _find_ffmpeg():
    """找 ffmpeg 可执行文件：优先 PATH，其次几个常见安装位置。

    返回 exe 路径或 None；None 时交给 yt-dlp 自己找。
    """
    import shutil
    p = shutil.which("ffmpeg")
    if p:
        return p
    for c in (
        os.path.expanduser(r"~\bin\ffmpeg.exe"),
        r"C:\ffmpeg\bin\ffmpeg.exe",
    ):
        if os.path.exists(c):
            return c
    return None


def sanitize(name, maxlen=120):
    name = _ILLEGAL.sub("_", name).strip().rstrip(".")
    name = re.sub(r"\s+", " ", name)
    return (name[:maxlen] or "untitled")


def write_cookiefile(cfg, path):
    """把 config 里的 cookie 字段写成 Netscape cookies.txt。缺失则返回 False。"""
    ck = cfg.get("cookie", {}) or {}
    if not ck.get("sessdata"):
        return False
    expires = int(time.time()) + 86400 * 30
    lines = ["# Netscape HTTP Cookie File"]
    for name in ("sessdata", "bili_jct", "buvid3", "dedeuserid"):
        val = ck.get(name)
        if not val:
            continue
        # domain, include_subdomains, path, secure, expiry, name, value
        lines.append("\t".join([".bilibili.com", "TRUE", "/", "FALSE", str(expires), name, val]))
    with open(path, "w", encoding="utf-8") as f:
        f.write("\n".join(lines) + "\n")
    return True


def _hook_factory(bvid):
    def hook(d):
        with _plock:
            st = PROGRESS.setdefault(bvid, {})
            if d.get("status") == "downloading":
                st["percent"] = round(float(re.sub(r"[^\d.]", "", d.get("_percent_str") or "0") or 0), 1)
                st["speed"] = d.get("_speed_str", "").strip()
                st["eta"] = d.get("_eta_str", "").strip()
                st["status"] = "downloading"
            elif d.get("status") == "finished":
                st["status"] = "merging"  # 交给 ffmpeg 合并
                st["percent"] = 100.0
    return hook


def download_one(cfg, video, cookiefile):
    """下载单个视频。返回落盘路径。异常向上抛，由工作线程标记 failed。"""
    bvid, title = video["bvid"], video["title"]
    outdir = os.path.abspath(cfg.get("download_dir") or "downloads")
    os.makedirs(outdir, exist_ok=True)
    h = int(cfg.get("quality") or 1080)

    fmt = f"bv*[height<={h}]+ba/b[height<={h}]/b"
    outtmpl = os.path.join(outdir, f"{sanitize(bvid + ' - ' + title)}.%(ext)s")

    opts = {
        "format": fmt,
        "outtmpl": outtmpl,
        "merge_output_format": "mp4",
        "noplaylist": True,
        "quiet": True,
        "no_warnings": True,
        "progress_hooks": [_hook_factory(bvid)],
        "retries": 3,
    }
    ff = _find_ffmpeg()
    if ff:
        opts["ffmpeg_location"] = ff
    if cookiefile:
        opts["cookiefile"] = cookiefile

    with _plock:
        PROGRESS[bvid] = {"percent": 0.0, "speed": "", "eta": "", "status": "downloading"}

    with yt_dlp.YoutubeDL(opts) as ydl:
        info = ydl.extract_info(f"https://www.bilibili.com/video/{bvid}", download=True)
        path = ydl.prepare_filename(info)
        # 合并后扩展名可能变成 mp4
        if not os.path.exists(path):
            base, _ = os.path.splitext(path)
            for ext in ("mp4", "mkv", "webm", "flv"):
                if os.path.exists(base + "." + ext):
                    path = base + "." + ext
                    break
    return path


def worker(cfg, q, cookie_path, on_done=None):
    """常驻下载线程：一个接一个消费队列。cookie_path 即生成的 cookies.txt 路径。

    on_done(bvid): 每个任务结束后回调（含异常路径），
    给上层清 _queued 去重集合用 —— 否则失败的视频永远重试不了。
    """
    cookiefile = cookie_path if write_cookiefile(cfg, cookie_path) else None
    if not cookiefile:
        print("[dl] 未配置 sessdata，将以游客身份下载（可能拿不到 1080p）", flush=True)
    while True:
        bvid = q.get()
        if bvid is None:  # 退出信号
            break
        try:
            video = db.get_video(bvid)
            if not video:
                continue
            db.set_status(bvid, "downloading")
            try:
                path = download_one(cfg, video, cookiefile)
                db.set_status(bvid, "done", file_path=path,
                              downloaded_at=datetime.now().isoformat(timespec="seconds"))
                print(f"[dl] OK  {bvid} -> {path}", flush=True)
            except Exception as e:
                db.set_status(bvid, "failed")
                print(f"[dl] ERR {bvid}: {e}", flush=True)
        finally:
            with _plock:
                PROGRESS.pop(bvid, None)
            if on_done:
                on_done(bvid)
            q.task_done()


def progress_rows():
    """组装 /api/progress 的返回。"""
    rows = []
    with _plock:
        for bvid, st in PROGRESS.items():
            v = db.get_video(bvid) or {}
            rows.append({
                "bvid": bvid,
                "title": v.get("title", ""),
                "percent": st.get("percent", 0.0),
                "speed": st.get("speed", ""),
                "eta": st.get("eta", ""),
                "status": "downloading" if st.get("status") == "merging"
                          else st.get("status", "downloading"),
            })
    return rows
