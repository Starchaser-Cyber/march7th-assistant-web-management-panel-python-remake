# March7th 小助手网页管理面板 · Python重构版

浏览器远程管理 Docker 版三月七小助手（[March7thAssistant](https://github.com/Starchaser-Cyber/March7thAssistant)）的 Web 管理面板，**Python 3.12 + FastAPI 实现**。

采用渐进式迁移架构（strangler pattern）：前端资产完全复用、后端按接口逐个迁移，未处理的请求经网关转发回原站点，**任何一步都可用 nginx 一行配置回滚**，全程不断服。

## 特性

- ✅ 只读接口已切换到 Python：容器状态 / 资源监控 / 实时日志 / 运行状态 / 任务历史
- 🔒 登录态与 PHP 版共享（读同一 session 文件），一个 cookie 两边认
- 🔁 网关转发兜底：未迁移 / 未登录请求转发回原站点，`X-M7A-Fwd` 头防转发循环
- 🧪 pytest 单测覆盖请求分发、session 解析、监控采样（无需服务器环境）

## 架构

```
浏览器 ──> nginx
             ├─ 已迁移接口 (?ajax=status|log|running|monitor|history)
             │        └─→ 127.0.0.1:8787（本服务）
             └─ 其余（页面渲染 / 未迁移接口 / POST）
                      └─→ 原站点（一切照旧）

本服务内部：
  已登录 & 已迁移接口 → Python 处理
  其余 → gateway.py 转发回原站点（hop-by-hop 头剥离、强制 identity 编码）
```

## 目录结构

```
├── main.py            # FastAPI 入口：session 加载 + GET 分发 + catch-all 转发
├── gateway.py         # 绞杀者网关：转发回原站点（头处理 / 防循环 / 压缩策略）
├── phpsess.py         # PHP session 文件解析（登录态互通）
├── phpfile.py         # PHP 序列化文件读写兼容
├── config.py          # 公共配置（可用环境变量覆盖）
├── routers/
│   └── readonly.py    # 五个只读接口处理器
├── services/          # 领域逻辑：监控采样、历史判定、日志、多实例、shell
└── tests/             # pytest 单测
```

## 本地开发

```bash
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
.venv/bin/pytest tests/ -q          # 单测
.venv/bin/python main.py            # 起 127.0.0.1:8787
```

## 部署

1. 代码放置在面板站点目录下（如 `<站点目录>/panel_migr/`）
2. `python3 -m venv .venv && .venv/bin/pip install -r requirements.txt`
3. systemd 托管运行 `python main.py`（监听 `127.0.0.1:8787`，`Restart=always`）
4. nginx 把已迁移的 `?ajax=` 查询分流到 8787，其余保持原样 → `nginx -t && reload`
5. 回滚 = 删掉分流规则再 reload

**环境变量**：

| 变量 | 默认值 | 说明 |
|---|---|---|
| `M7A_PANEL_DIR` | 上级目录 | 面板根目录（data/、实例与配置文件所在处） |
| `M7A_PHP_UPSTREAM` | `http://127.0.0.1:9999` | 原站点地址（网关转发目标） |
| `M7A_LISTEN_HOST` / `M7A_LISTEN_PORT` | `127.0.0.1` / `8787` | 服务监听 |
| `M7A_SESSION_DIR` | `/tmp` | PHP session 目录（登录态互通） |
| `M7A_TZ` | 系统时区 | 与 PHP 时区对齐 |

## 迁移进度

| 阶段 | 内容 | 状态 |
|---|---|---|
| M1 | 只读 5 接口（status / log / running / monitor / history） | ✅ |
| M2 | 写操作全量切换（POST action + 服务器回调 + 导出） | ⏳ |
| M3 | 预览收编 + WebRTC 极致档 | ⏳ |
| M4 | 监控 / 日志 / 历史 / 告警事件化 | ⏳ |
| M5 | 会话加固 + 前端拆包 | ⏳ |
| M6 | 原站点退役、直连 | ⏳ |
