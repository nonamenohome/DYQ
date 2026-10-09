"""SQLite 存储层。单文件自用工具，全局一个连接 + 锁即可。"""
import sqlite3
import os
import threading
from datetime import datetime

DB_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "bili.db")

_lock = threading.Lock()
_conn = None


def _now():
    return datetime.now().isoformat(timespec="seconds")


def get_conn():
    global _conn
    if _conn is None:
        # check_same_thread=False：轮询/下载/Flask 三个线程共用一个连接，全靠 _lock 串行化
        _conn = sqlite3.connect(DB_PATH, check_same_thread=False)
        _conn.row_factory = sqlite3.Row
    return _conn


def init():
    with _lock:
        c = get_conn()
        c.executescript(
            """
            CREATE TABLE IF NOT EXISTS videos (
              bvid TEXT PRIMARY KEY, aid INTEGER, title TEXT, pubdate INTEGER,
              duration INTEGER, cover TEXT, up_name TEXT,
              status TEXT DEFAULT 'pending', file_path TEXT, downloaded_at TEXT, created_at TEXT
            );
            CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT);
            """
        )
        c.commit()


def insert_videos(videos):
    """批量入库，bvid 去重（INSERT OR IGNORE）。返回新插入的条数。"""
    with _lock:
        c = get_conn()
        before = c.total_changes
        c.executemany(
            """INSERT OR IGNORE INTO videos
               (bvid, aid, title, pubdate, duration, cover, up_name, status, created_at)
               VALUES (:bvid, :aid, :title, :pubdate, :duration, :cover, :up_name, 'pending', :created_at)""",
            [{**v, "created_at": _now()} for v in videos],
        )
        c.commit()
        return c.total_changes - before


def list_videos(q=None, status="all", order="pubdate_desc"):
    sql = "SELECT * FROM videos WHERE 1=1"
    args = []
    if q:
        sql += " AND title LIKE ?"
        args.append(f"%{q}%")
    if status and status != "all":
        sql += " AND status = ?"
        args.append(status)
    sql += " ORDER BY pubdate " + ("DESC" if order != "pubdate_asc" else "ASC")
    with _lock:
        return [dict(r) for r in get_conn().execute(sql, args).fetchall()]


def get_video(bvid):
    with _lock:
        row = get_conn().execute("SELECT * FROM videos WHERE bvid = ?", (bvid,)).fetchone()
        return dict(row) if row else None


def set_status(bvid, status, file_path=None, downloaded_at=None):
    with _lock:
        c = get_conn()
        c.execute(
            "UPDATE videos SET status=?, file_path=COALESCE(?, file_path), "
            "downloaded_at=COALESCE(?, downloaded_at) WHERE bvid=?",
            (status, file_path, downloaded_at, bvid),
        )
        c.commit()


def pending_bvids():
    """所有待下载的 bvid（按发布时间从旧到新，先补老的）。"""
    with _lock:
        rows = get_conn().execute(
            "SELECT bvid FROM videos WHERE status='pending' ORDER BY pubdate ASC"
        ).fetchall()
        return [r["bvid"] for r in rows]


def stats():
    with _lock:
        c = get_conn()
        total = c.execute("SELECT COUNT(*) FROM videos").fetchone()[0]
        by = {r["status"]: r["n"] for r in c.execute(
            "SELECT status, COUNT(*) n FROM videos GROUP BY status").fetchall()}
        rows = c.execute("SELECT DISTINCT up_name FROM videos "
                         "WHERE up_name IS NOT NULL AND up_name != ''").fetchall()
        names = [r["up_name"] for r in rows]
        return {"total": total,
                "up_name": names[0] if names else None,
                "up_names": names,
                **{s: by.get(s, 0) for s in
                   ("pending", "downloading", "done", "failed")}}


def set_meta(key, value):
    with _lock:
        c = get_conn()
        c.execute("INSERT OR REPLACE INTO meta(key, value) VALUES (?, ?)", (key, value))
        c.commit()


def get_meta(key, default=None):
    with _lock:
        row = get_conn().execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
        return row["value"] if row else default


def reset_downloading():
    """启动时把上次残留的 downloading 打回 pending，避免任务丢掉。"""
    with _lock:
        c = get_conn()
        c.execute("UPDATE videos SET status='pending' WHERE status='downloading'")
        c.commit()


if __name__ == "__main__":
    init()
    print(stats())
