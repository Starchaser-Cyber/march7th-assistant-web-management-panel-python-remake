# 故障排查实战记录（H.264 预览解锁全过程）

> 本文记录 v1.19 面板在真实生产环境启用 **H.264 极致档（⚡按钮）** 时踩到的三个问题：
> 现象 → 排查思路 → 根因 → 解决方法 → 如何自验。排查用到的方法（对比哈希、手工 WebSocket 握手等）
> 同样适用于你部署时遇到的任何「预览 403 / 无画面 / 令牌无效」问题。

---

## 问题一：点 H.264 提示「服务器未安装 ffmpeg，H.264 档不可用」

### 现象

预览正常（JPEG 档有画面），点 ⚡H.264 后预览区域显示：

```
服务器未安装 ffmpeg，H.264 档不可用
```

### 定位

H.264 档的原理是：面板把上游 JPEG 帧喂给 `ffmpeg` 编码成 H.264 fMP4 再推给浏览器（MSE 播放），
所以服务器必须有带 `libx264` 的 ffmpeg。面板**每次建立连接时实时检测** `shutil.which(ffmpeg)`，
不依赖启动时的快照。

```bash
which ffmpeg || echo "未安装"
ffmpeg -hide_banner -encoders 2>/dev/null | grep libx264
```

### 解决（Ubuntu / Debian）

```bash
sudo apt-get update && sudo apt-get install -y ffmpeg
which ffmpeg                 # 应输出 /usr/bin/ffmpeg
ffmpeg -hide_banner -encoders | grep libx264   # 应能看到 libx264
```

其他发行版：`dnf install ffmpeg` / `apk add ffmpeg`（需确保构建包含 libx264）。

**装完不需要重启面板**——下一次点 H.264 就会实时检测到。

### 自验（不经过面板，直接跑面板同款编码管线）

```bash
ffmpeg -hide_banner -loglevel error \
  -f lavfi -i testsrc=size=640x360:rate=15 \
  -c:v libx264 -preset veryfast -tune zerolatency -pix_fmt yuv420p -g 15 \
  -f mp4 -movflags frag_keyframe+empty_moov+default_base_moof \
  -t 1 -f null -
echo "libx264 fMP4 管线 OK"
```

---

## 问题二：装完 ffmpeg 后报「上游连接失败：server rejected WebSocket connection: HTTP 403」（必现）

### 现象

ffmpeg 装好后，点 ⚡H.264 **每次都报**：

```
上游连接失败：server rejected WebSocket connection: HTTP 403
```

这条错误来自面板：面板作为反向代理，用 `websockets` 库连容器里的 `preview_server`（9223 端口）
时被对方以 403 拒绝。

### 排查过程（逐层排除，可照抄的方法）

**第 1 层：容器服务活着吗？**

```bash
docker ps --filter name=m7a
curl -s http://127.0.0.1:9999/m7a-preview/health   # 返回 200 即正常
```

✅ 正常，`preview_server` 在跑，health 200。

**第 2 层：两边的密钥（secret）是同一份吗？**

`preview_server` 校验令牌用的是 `preview_secret` 文件。宿主与容器各读一份，只要有一个对不上就 403。
**只比对哈希，不打印内容**：

```bash
sha256sum /home/<小助手目录>/logs/preview_secret
docker exec m7a sha256sum /m7a/logs/preview_secret
```

✅ 两个哈希一致（`c2a46c81…`），排除密钥不一致。

**第 3 层：绕开面板，手工对容器发 WebSocket 握手**

用与面板完全相同的算法现场算一个当日令牌，直接对容器 `/ws` 握手：

```python
# 服务器上执行；secret 从 preview_secret 读，日期口径与 preview_server 一致
import hmac, hashlib, time, socket
sec = open("/home/<小助手目录>/logs/preview_secret").read().strip()
tok = hmac.new(sec.encode(), time.strftime("%Y%m%d").encode(),
              hashlib.sha256).hexdigest()[:32]
ip, port = "172.17.0.2", 9223          # docker inspect -f '{{range .NetworkSettings.Networks}}{{.IPAddress}}{{end}}' m7a
key = "dGhlIHNhbXBsZSBub25jZQ=="
lines = ["GET /ws?token=%s&res=720p&quality=80 HTTP/1.1" % tok,
         "Host: %s:%d" % (ip, port), "Upgrade: websocket",
         "Connection: Upgrade", "Sec-WebSocket-Key: " + key,
         "Sec-WebSocket-Version: 13", "", ""]
s = socket.create_connection((ip, port), timeout=5)
s.sendall("\r\n".join(lines).encode())
print(s.recv(100))   # 期望: HTTP/1.1 101 Switching Protocols
```

✅ 手工握手返回 **101**——说明密钥、令牌算法、容器校验全部正常，问题一定出在**面板发出的 URL 上**。

**第 4 层：对比「预期 URL」和「面板真实发出的 URL」**

面板 `routers/preview.py` 转发时会从浏览器 query 里挑 `token/res/quality` 三个参数拼给上游。
打印面板实际拼出的 `ws_url`，发现：

```
预期: ws://172.17.0.2:9223/ws?token=2d2f4381…&res=720p
实际: ws://172.17.0.2:9223/ws?2d2f4381…&720p          ← 键名丢了！
```

### 根因

拼接时只取了每个参数的**值**、丢掉了 `token=` / `res=` 这个**键名**：

```python
# 修复前：join 出来的是裸值 → "?2d2f…&720p"
qs = "&".join(
    websocket.url.query.split(k + "=", 1)[1].split("&")[0]
    for k in keep
)
```

上游 `parse_qs` 解析 `?2d2f…&720p` 找不到 `token` 字段 → `check_token` 失败 → **403 Forbidden**。

> 为什么之前没暴露？——服务器没装 ffmpeg 时，面板在更早一步（1013 未装 ffmpeg）就返回了，
> 这段转发代码从来没真正执行过；装完 ffmpeg 第一次跑通到这一步，bug 才现形。

### 解决

```python
# 修复后：保留 key=value 形式
qs = "&".join(
    k + "=" + websocket.url.query.split(k + "=", 1)[1].split("&")[0]
    for k in keep
)
```

改完 `sudo systemctl restart m7a-panel`，回归验证：按第 3 层的握手脚本用**面板真实代码路径**
拼出的 URL 再连一次，返回 101 即修复完成。

---

## 问题三（潜在坑）：令牌日期口径不一致，每天 00:00–08:00 会 403

### 现象与原理

预览令牌是「密钥 + 当天日期」做 HMAC、**按天轮换**的。面板与容器必须对「今天是哪天」达成一致：

| 端 | 取日期方式 | 结果 |
|----|-----------|------|
| 面板（修复前） | `time.strftime("%Y%m%d", time.gmtime())` → **UTC 日期** | 与容器差一天 |
| 容器 preview_server | `time.strftime("%Y%m%d')` → **本地日期** | 本地（CST） |

中国时区（UTC+8）下，每天 **00:00–07:59** 这段时间：UTC 还是「昨天」，本地已经「今天」，
两端算出的令牌不同 → 面板本地校验能过（面板自己跟自己一致），转发到容器就 403。
这类「白天好好的、凌晨突然 403」的问题极难肉眼发现。

### 解决

面板侧统一改为**本地日期**，与容器、与旧版 `date('Ymd')` 行为完全一致：

```python
# services/preview.py
def day_token(secret: str, day: str | None = None) -> str:
    d = day or time.strftime("%Y%m%d")        # 本地日期，与容器口径一致
    return hmac.new(secret.encode(), d.encode(), hashlib.sha256).hexdigest()[:32]
```

### 通用原则

跨进程 / 跨容器用「按日令牌」时，两端必须：**同一密钥文件 + 同一日期口径 + 机器时间大致对齐**。
排查时先 `date` 双端对照，再哈希比对密钥文件，最后才怀疑算法。

---

## 通用自查清单（预览 403 / 无画面 / 令牌无效）

按顺序检查，哪一步不符合预期就从哪一步入手：

1. `docker ps` 容器在跑；`curl http://127.0.0.1:<面板端口>/m7a-preview/health` 返回 200
2. `sha256sum` 宿主与容器的 `preview_secret` 哈希一致
3. 双端 `date` 时间一致、时区明确（避免 UTC/本地混用）
4. 用「问题二 · 第 3 层」的握手脚本手工连容器 `/ws`，应返回 101
5. H.264 档另需：`which ffmpeg` 且 `ffmpeg -encoders | grep libx264`
6. 面板日志看具体报错：`journalctl -u m7a-panel -n 100 --no-pager`
7. 经 Nginx 反代时确认 WebSocket 三个头（`proxy_http_version 1.1` / `Upgrade` / `Connection "upgrade"`）

---

*对应修复已包含在当前仓库代码中；问题一为环境问题按文中命令处理即可，问题二、问题三为面板代码缺陷、已修复。*
