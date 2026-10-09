"""一次性脚本：每个 UP 各下最新 10 个。下完自己退出，不碰轮询。

用法：python dl_batch.py
"""
import time
import app
import db
import downloader

PER_UP = 10

app.load_config()
db.init()

cookie_ok = downloader.write_cookiefile(app._cfg, app.DB_DIR_COOKIE)
cookiefile = app.DB_DIR_COOKIE if cookie_ok else None
print(f"[batch] cookiefile={cookie_ok}", flush=True)

c = db.get_conn()
ups = [r["up_name"] for r in
       c.execute("SELECT DISTINCT up_name FROM videos WHERE up_name IS NOT NULL")]

targets = []
for up in ups:
    rows = c.execute(
        "SELECT bvid, title, duration FROM videos "
        "WHERE up_name=? AND status='pending' ORDER BY pubdate DESC LIMIT ?",
        (up, PER_UP)).fetchall()
    print(f"[batch] {up}: 选中 {len(rows)} 个", flush=True)
    targets.extend([dict(r) for r in rows])

print(f"[batch] 共 {len(targets)} 个，开始串行下载", flush=True)

ok = fail = 0
for i, t in enumerate(targets, 1):
    bvid = t["bvid"]
    v = db.get_video(bvid)
    db.set_status(bvid, "downloading")
    try:
        path = downloader.download_one(app._cfg, v, cookiefile)
        db.set_status(bvid, "done", file_path=path,
                      downloaded_at=__import__("datetime").datetime.now().isoformat(timespec="seconds"))
        ok += 1
        print(f"[batch] {i}/{len(targets)} OK  {bvid}", flush=True)
    except Exception as e:
        db.set_status(bvid, "failed")
        fail += 1
        print(f"[batch] {i}/{len(targets)} ERR {bvid}: {str(e)[:120]}", flush=True)
    time.sleep(2)  # 间隔，防风控

print(f"[batch] 完成：成功 {ok}，失败 {fail}", flush=True)
print(f"[batch] 统计：{db.stats()}", flush=True)
