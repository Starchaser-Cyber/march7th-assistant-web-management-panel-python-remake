# March7th Assistant Web Management Panel — Python Remake

这是 `march7th-assistant-web-management-panel` 的 Python/FastAPI 重制版。

它保留原面板的主要页面和使用习惯，并由 Python + FastAPI + Uvicorn 提供服务，不依赖 PHP-FPM。项目适合在已经运行 March7th Assistant Docker 容器的 Linux 服务器上部署。

## 主要能力

- Docker 实例状态、日志和任务管理
- 配置读取与安全写入
- 监控、历史记录和告警
- WebSocket 事件流
- 游戏画面预览与分辨率切换
- 登录限速、会话轮换和预览令牌校验
- systemd 常驻与自动重启

## 快速开始

```bash
git clone https://github.com/starchaser-cyber/march7th-assistant-web-management-panel-python-remake.git
cd march7th-assistant-web-management-panel-python-remake/panel_migr
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
M7A_PANEL_DIR=/srv/m7a-panel M7A_LISTEN_HOST=0.0.0.0 M7A_LISTEN_PORT=9999 .venv/bin/python main.py
```

生产环境请按 [`docs/DEPLOY_PYTHON.md`](docs/DEPLOY_PYTHON.md) 配置 systemd。部署者必须把 `M7A_PANEL_DIR` 改成自己的目录，不能直接照抄他人的生产路径。

## 测试

```bash
cd panel_migr
.venv/bin/python -m pytest -q
.venv/bin/python -m compileall -q .
```

开发阶段回归结果：140 passed，3 skipped，0 failed。

## 安全提醒

不要提交 `.panel_pass.php`、`.env`、会话文件、监控数据、备份文件或任何访问令牌。首次部署后请立即设置强密码，并通过防火墙限制管理端口的访问来源。

## 文档

- [Python 部署手册](docs/DEPLOY_PYTHON.md)
- [验收清单](docs/ACCEPTANCE.md)
- [测试报告](docs/TEST_REPORT.md)
- [PHP 到 Python 迁移说明](docs/MIGRATION_NOTES.md)
- [接口清单](docs/migration_api_inventory.md)

## 许可证

本项目使用 GPL-3.0，详见 [`LICENSE`](LICENSE)。
