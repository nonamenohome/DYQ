# B站 UP 主视频自动下载 + 看板

轮询一个 B 站 UP 主，发现新投稿自动下载 1080p，元数据存 SQLite，本地 Flask 页面看板。
Windows 11 本机、自用单用户、常驻进程。

## 架构

```
Flask 主线程 ── 只读写 DB / 投递队列，不碰网络
poller 线程 ── 每 N 分钟调 bilibili-api 拉一页投稿，新 bvid INSERT OR IGNORE 入库(status=pending)
dlworker 线程 ── 单线程消费 queue.Queue，同时只下一个 (yt-dlp)，防风控 + 避免写库冲突
```

- 去重靠 `videos.bvid` 主键。
- 下载进度由 yt-dlp 的 `progress_hooks` 写入内存字典，`/api/progress` 直接读。
- 重启时把残留的 `downloading` 打回 `pending`，任务不会丢。

## 安装

```bash
pip install -r requirements.txt
```

**必须自装 ffmpeg**（1080p 的 `bv*+ba` 需要合并音视频，yt-dlp 依赖外部 ffmpeg）：

- 下载 <https://www.gyan.dev/ffmpeg/builds/> 的 `ffmpeg-release-essentials.zip`，解压。
- 把解压目录里的 `bin` 加进系统环境变量 `PATH`。
- 验证：新开一个终端跑 `ffmpeg -version` 有输出即可。

## 配置

```bash
copy config.example.json config.json
```

编辑 `config.json`：

```json
{
  "uid": 12345678,
  "cookie": {"sessdata": "", "bili_jct": "", "buvid3": "", "dedeuserid": ""},
  "quality": 1080,
  "poll_interval_minutes": 10,
  "download_dir": "downloads",
  "port": 5000
}
```

- `uid`：UP 主的数字 UID（从空间页 `space.bilibili.com/<这串数字>` 取）。
- `quality`：目标清晰度上限，1080 表示 `height<=1080`。
- `download_dir`：相对路径按本项目目录解析。
- `cookie`：见下。**不填也能跑，但大多拿不到 1080p**（游客一般只有 480p/720p）。

### 怎么拿 cookie

1. Chrome/Edge 登录 bilibili.com。
2. F12 → `Application`（应用）→ `Cookies` → `https://www.bilibili.com`。
3. 分别复制这几个 Cookie 的 **Value**，填进 `config.json`：
   - `SESSDATA` → `sessdata`
   - `bili_jct` → `bili_jct`
   - `buvid3` → `buvid3`
   - `DedeUserID` → `dedeuserid`
4. 程序启动时会据此生成一份 Netscape 格式的 `cookies.txt` 交给 yt-dlp，无需手动导出。

cookie 有时效，失效后下载会 403 / 登录报错 —— 重新抓一遍覆盖即可。

## 运行

```bash
python app.py
```

然后浏览器打开 <http://127.0.0.1:5000>。Ctrl-C 停止。

## 接口

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/` | 看板页面 |
| GET | `/api/status` | `last_check / next_check / poller_enabled / video_count / downloading` |
| GET | `/api/videos` | 参数 `q`(标题模糊) `status`(all\|pending\|downloading\|done\|failed) `order`(pubdate_desc\|pubdate_asc) |
| POST | `/api/download/<bvid>` | 手动入队 |
| POST | `/api/refresh` | 立即触发一次检查 |
| GET | `/api/progress` | 正在下载的进度 `percent/speed/eta` |

## 健壮性

- 网络失败、风控（返回码 `-352`）、cookie 失效都会在 `check_once` / 下载流程里被 catch，
  打印 `[poll] 出错（已忽略，下轮继续）` 后继续，**不会让轮询线程崩**。
- 单个视频下载失败标记 `failed`，不影响队列里其余任务；可在页面点「手动下载」重试。

## 自检（不连 B 站）

```bash
python -m py_compile db.py fetcher.py downloader.py app.py
python -c "import db; db.init(); print(db.stats())"
```

## 待验证

以下因缺少真实 uid/cookie 未做端到端验证，代码按已核实的签名编写，若报错优先看这里：

1. **`get_videos` 返回字段名**：已核实签名
   `get_videos(self, tid=0, pn=1, ps=30, keyword='', order=VideoOrder.PUBDATE) -> dict`，
   `VideoOrder` 成员确认为 `PUBDATE / FAVORITE / VIEW`（注意：**不是** 网上常传的 `CLICK/STOW`）。
   列表取 `page["list"]["vlist"]`。但 vlist 条目里的具体键名（`created` vs `pubdate`、`length` 的
   字符串格式、`pic` 是否带 `//` 前缀）来自社区文档，代码已做兼容（`created or pubdate`、
   `mm:ss` 解析、`//` 补 `https:`），**真实返回仍需跑一次确认**。
2. **WBI 签名 / -352 风控**：新版库内部处理 WBI，但空间接口对匿名请求风控较严。填了 cookie
   一般可过；若持续 `-352`，是风控而非代码问题。
3. **`quality=1080` 是否真能拿到**：取决于账号是否大会员 / 视频本身是否只有更高码率，
   非大会员通常 1080p 可用但 1080P60/4K 不行。格式串带了 `/b` 兜底。
4. **`poller_enabled`**：目前恒为 `true`（无暂停开关），接口字段先占位。

已安装依赖版本：`yt-dlp 2026.08.19`、`bilibili-api-python 17.x`、`Flask 2.3.3`（本机实测）。
