"""调用 bilibili-api-python 拉取 UP 主投稿列表。

已核实的 API（bilibili-api-python 17.x，从包源码 inspect 得到）：
    from bilibili_api import user, Credential
    u = user.User(uid, credential=cred)
    page = await u.get_videos(tid=0, pn=1, ps=30, keyword="", order=user.VideoOrder.PUBDATE)
    # VideoOrder 只有 PUBDATE / FAVORITE / VIEW 三个成员
    # 返回原始接口 dict，列表在 page["list"]["vlist"]

字段映射（vlist 项 → 本工具字段）：
    bvid -> bvid, aid -> aid, title -> title
    created -> pubdate（秒级时间戳；有的接口页返回 pubdate，两者都兜）
    length -> duration（字符串 "mm:ss"，转成秒）
    pic -> cover（部分条目是 //i0.hdslb.com/... 需补 https:）
    author -> up_name
"""
import asyncio
import re
import time

from bilibili_api import user, Credential


def make_credential(cfg):
    ck = cfg.get("cookie", {}) or {}
    return Credential(
        sessdata=ck.get("sessdata") or None,
        bili_jct=ck.get("bili_jct") or None,
        buvid3=ck.get("buvid3") or None,
        dedeuserid=ck.get("dedeuserid") or None,
    )


def _len_to_sec(length):
    """"mm:ss" 或 "hh:mm:ss" 或已是数字 → 秒。"""
    if isinstance(length, (int, float)):
        return int(length)
    if not length:
        return 0
    parts = [int(p) for p in re.findall(r"\d+", str(length))][:3]
    sec = 0
    for p in parts:
        sec = sec * 60 + p
    return sec


def _norm_cover(pic):
    if not pic:
        return ""
    return "https:" + pic if pic.startswith("//") else pic


async def _fetch(uid, cred, pn):
    u = user.User(uid, credential=cred)
    page = await u.get_videos(pn=pn, ps=30, order=user.VideoOrder.PUBDATE)
    vlist = (page.get("list") or {}).get("vlist") or []
    total = (page.get("page") or {}).get("count") or 0
    return vlist, total


def fetch_latest(cfg):
    """拉取所有目标 UP 的全部投稿，返回本工具的字段列表。

    失败抛异常，由调用方记日志后继续。
    翻页之间 sleep 1.2s —— 实测连发会吃 B站 412 风控。
    """
    uids = cfg.get("uids") or ([cfg["uid"]] if cfg.get("uid") else [])
    if not uids:
        raise RuntimeError("未配置 uid / uids")
    cred = make_credential(cfg)
    out = []
    for i, uid in enumerate(uids):
        if i:
            time.sleep(1.5)  # UP 之间也留间隔，别一口气打
        try:
            vlist = _fetch_all(uid, cred)
        except Exception as e:
            msg = str(e)
            # 风控页/HTML 会带几百 KB 正文，截断后再抛出，否则日志不可读
            if "<html" in msg.lower() or len(msg) > 300:
                msg = msg[:200].replace("\n", " ") + " …（疑似 B站风控页，见 README 的 cookie 说明）"
            raise RuntimeError(f"拉取 uid={uid} 失败：{msg}") from e
        out.extend(_to_rows(vlist))
    return out


def _fetch_all(uid, cred):
    """翻页拉完某个 UP 的全部投稿。"""
    vlist = []
    page = 1
    while True:
        got, total = asyncio.run(_fetch(uid, cred, page))
        vlist.extend(got)
        if not got or len(vlist) >= total or page >= 50:
            return vlist
        page += 1
        time.sleep(1.2)


def _to_rows(vlist):
    out = []
    for v in vlist:
        bvid = v.get("bvid")
        if not bvid:
            continue
        out.append({
            "bvid": bvid,
            "aid": v.get("aid") or 0,
            "title": v.get("title") or "",
            "pubdate": int(v.get("created") or v.get("pubdate") or 0),
            "duration": _len_to_sec(v.get("length")),
            "cover": _norm_cover(v.get("pic")),
            "up_name": v.get("author") or "",
        })
    return out
