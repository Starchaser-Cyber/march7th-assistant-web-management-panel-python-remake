"""v1.20 文案中性化回归：模板/前端零 PHP 字样，/action 中性端点与 /index.php 别名等价。"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from fastapi.testclient import TestClient  # noqa: E402
from main import app                          # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
client = TestClient(app)


def test_template_free_of_php():
    tpl = (ROOT / "templates" / "panel.html.j2").read_text(encoding="utf-8")
    assert "php" not in tpl.lower()


def test_panel_js_neutral_endpoint():
    js = (ROOT / "static" / "panel.js").read_text(encoding="utf-8")
    assert "index.php" not in js
    assert js.count("fetch('action'") == 9
    assert "f.action = 'action';" in js


def test_action_post_routes_like_root():
    a = client.post("/action", data={"action": "login", "pass": "wrong-pw"})
    b = client.post("/", data={"action": "login", "pass": "wrong-pw"})
    assert a.status_code == b.status_code
    assert a.status_code != 404
    # 若 /action 未进 _PANEL_PATHS 会被回源转发，收到 mock 上游页面
    assert b"MOCK_PHP_PAGE" not in a.content


def test_action_get_key_callback_alias():
    # 关键回调分发到 Python（缺 key → 403）；未分发则会转发成 mock 上游 200
    r = client.get("/action?scheduler=1", follow_redirects=False)
    assert r.status_code == 403


def test_index_php_alias_retained():
    # 旧版兼容：/index.php 必须继续可用（老页面、老 crontab、回滚后的旧代码）
    r = client.get("/index.php?scheduler=1", follow_redirects=False)
    assert r.status_code == 403


def test_pagectx_cron_neutral():
    src = (ROOT / "services" / "pagectx.py").read_text(encoding="utf-8")
    assert "/index.php?" not in src
    assert src.count("{base}/action?") == 3


def test_flash_fallback_neutral():
    from services.flash import fallback_page
    assert "index.php" not in fallback_page(err="boom")


def test_updateops_err_copy_neutral():
    src = (ROOT / "services" / "updateops.py").read_text(encoding="utf-8")
    assert "写入 index.php 失败" not in src
    assert "写入面板主文件失败" in src
