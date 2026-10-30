"""V1.24.5 修复回归测试：Q-02（实例容器名撞车静默覆盖）+ Q-03（未登录 POST 回 HTML）。

对应第三方审计报告：
- Q-02【P1】`h_instance_save` 新增实例时 `inst_id` 为空，判据退化成「容器名相同即
  视为同一条」，把**已有实例整条改写**（id 保留、name/dir 被新值覆盖），文案还写
  「已更新」，实例总数仍为 1 —— 用户以为新建了一个，原实例配置已被静默吞掉。
- Q-03【P2】未登录 POST 无条件返回 200 + HTML 登录页；前端 `fetch('action').then(
  r => r.json())` 解析 HTML 抛 SyntaxError → 落进 `.catch` → 弹「网络错误」，
  真实原因（登录过期）完全不可见。

全部离线：monkeypatch / tmp_path，不联网、不碰本机 docker。
"""
import sys
import uuid
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import config as cfg
from fastapi.testclient import TestClient

from main import app
import routers.write as W

client = TestClient(app)

TOKEN = "v45insttoken"

# ===== 测试实例：目录落在 tests/_tmp 下 =====
INST_DIR = Path(__file__).resolve().parent / "_tmp" / "v1245inst"
(INST_DIR / "logs").mkdir(parents=True, exist_ok=True)
(INST_DIR / "data").mkdir(parents=True, exist_ok=True)

# 审计报告里复现用的原始实例
EXISTING = {"id": "m7a", "name": "我的主实例", "container": "m7a",
            "dir": "/home/march7thassistant", "default": True}


# ================= 通用 helper =================

def new_session(authed=True, csrf=True):
    """在测试 session 目录造一个会话文件（与 test_m2_post / test_v1244 同风格）。"""
    sid = "v45" + uuid.uuid4().hex[:10]
    parts = []
    if authed:
        parts.append("m7a_panel_auth|b:1;")
    if csrf:
        parts.append(f'm7a_panel_csrf|s:{len(TOKEN)}:"{TOKEN}";')
    p = Path(cfg.SESSION_SAVE_PATH)
    p.mkdir(parents=True, exist_ok=True)
    (p / f"sess_{sid}").write_text("".join(parts), encoding="utf-8")
    return {"PHPSESSID": sid}


def _hop(r, cookies):
    """手动跟随 3xx（TestClient 自动跟随时会丢 per-request cookies）。"""
    if r.status_code in (301, 302, 303, 307, 308) and r.headers.get("location"):
        return client.get(r.headers["location"], cookies=cookies or {})
    return r


def post(action, data=None, cookies=None, follow=True, headers=None):
    payload = {"action": action, "csrf": TOKEN}
    payload.update(data or {})
    ck = cookies or new_session()
    r = client.post("/", data=payload, cookies=ck, follow_redirects=False,
                    headers=headers or {})
    return _hop(r, ck) if follow else r


def post_raw(action, data=None, cookies=None, headers=None):
    """不跟随、不自动加 CSRF（用于未登录/无 token 场景）。"""
    payload = {"action": action}
    payload.update(data or {})
    return client.post("/", data=payload, cookies=cookies or {},
                       follow_redirects=False, headers=headers or {})


def _stub_instances(monkeypatch, items):
    """把实例表换成内存版：记录写入、隔离真实 .instances.php。"""
    state = {"items": [dict(x) for x in items], "writes": []}

    def load():
        return [dict(x) for x in state["items"]]

    def write(new_items):
        state["writes"].append([dict(x) for x in new_items])
        state["items"] = [dict(x) for x in new_items]
        return True

    monkeypatch.setattr(W, "instances_load", load)
    monkeypatch.setattr(W, "instances_write", write)
    monkeypatch.setattr(W, "instance_switch_to", lambda s, sid, t: True)
    return state


# ================= Q-02：新增实例撞容器名 =================

def test_q02_new_instance_same_container_does_not_overwrite(monkeypatch):
    """审计复现：新增时容器名撞车 → 必须报错，且原实例配置一字不改、总数不变。"""
    state = _stub_instances(monkeypatch, [EXISTING])

    r = post("instance_save", {"inst_id": "", "inst_name": "我新建的",
                               "inst_container": "m7a",
                               "inst_dir": str(INST_DIR)})

    # 1) 不再谎报「已更新」
    assert "已更新" not in r.text
    # 2) 给出可操作的错误
    assert "已被实例" in r.text and "我的主实例" in r.text
    assert "请换一个容器名" in r.text
    # 3) 关键：没有写盘
    assert state["writes"] == [], "撞名时不得写实例表"
    # 4) 原实例完全未被改写
    assert state["items"] == [EXISTING]


def test_q02_new_instance_same_container_keeps_name_and_dir(monkeypatch):
    """逐字段校验：name / dir / default 都保留原值（旧实现会被新值覆盖）。"""
    state = _stub_instances(monkeypatch, [EXISTING])
    post("instance_save", {"inst_id": "", "inst_name": "我新建的",
                           "inst_container": "m7a", "inst_dir": "/tmp/newdir"})
    assert len(state["items"]) == 1
    item = state["items"][0]
    assert item["name"] == "我的主实例"
    assert item["dir"] == "/home/march7thassistant"
    assert item["default"] is True


def test_q02_new_instance_distinct_container_appends(monkeypatch):
    """不撞名的新增仍正常追加，总数 +1 并切换。"""
    state = _stub_instances(monkeypatch, [EXISTING])
    r = post("instance_save", {"inst_id": "", "inst_name": "副实例",
                               "inst_container": "second",
                               "inst_dir": str(INST_DIR)})
    assert "已添加并切换" in r.text
    assert len(state["items"]) == 2
    assert [x["container"] for x in state["items"]] == ["m7a", "second"]
    assert state["items"][1]["name"] == "副实例"


def test_q02_new_instance_into_empty_table(monkeypatch):
    """空表新增（test_m2_post.test_instance_save_new 的路径）不受影响。"""
    state = _stub_instances(monkeypatch, [])
    r = post("instance_save", {"inst_id": "", "inst_name": "首实例",
                               "inst_container": "first",
                               "inst_dir": str(INST_DIR)})
    assert "已添加并切换" in r.text
    assert len(state["items"]) == 1


def test_q02_edit_existing_by_id_still_updates(monkeypatch):
    """编辑（带 inst_id）仍按 id 命中并更新 —— 不能把编辑功能一起改坏。"""
    state = _stub_instances(monkeypatch, [EXISTING])
    r = post("instance_save", {"inst_id": "m7a", "inst_name": "改名后",
                               "inst_container": "m7a",
                               "inst_dir": "/home/march7thassistant"})
    assert "已更新" in r.text and "已添加" not in r.text
    assert len(state["items"]) == 1
    assert state["items"][0]["id"] == "m7a"
    assert state["items"][0]["name"] == "改名后"
    assert state["writes"], "编辑应写盘"


def test_q02_edit_to_container_taken_by_other_rejected(monkeypatch):
    """编辑时把容器名改成别的实例已占用的名字 → 报错不写盘。"""
    state = _stub_instances(monkeypatch, [
        EXISTING,
        {"id": "second", "name": "副实例", "container": "second",
         "dir": "/home/demo/second", "default": False},
    ])
    r = post("instance_save", {"inst_id": "m7a", "inst_name": "我的主实例",
                               "inst_container": "second",
                               "inst_dir": "/home/march7thassistant"})
    assert "已被实例" in r.text and "副实例" in r.text
    assert state["writes"] == []


def test_q02_edit_missing_id_appends_as_before(monkeypatch):
    """带 inst_id 但找不到该 id → 保持原有行为：容器名空闲则追加。"""
    state = _stub_instances(monkeypatch, [EXISTING])
    r = post("instance_save", {"inst_id": "ghost", "inst_name": "幽灵",
                               "inst_container": "ghostc",
                               "inst_dir": str(INST_DIR)})
    assert "已添加并切换" in r.text
    assert len(state["items"]) == 2


def test_q02_end_to_end_instances_file_untouched(monkeypatch, tmp_path):
    """端到端（真实 instances_write 到 tmp 文件）：撞名后文件内容不变。"""
    inst_file = tmp_path / ".instances.php"
    from phpfile import dump_php_return

    inst_file.write_text(dump_php_return([EXISTING]), encoding="utf-8")
    before = inst_file.read_text(encoding="utf-8")

    monkeypatch.setattr(W, "instances_load", lambda: [dict(EXISTING)])
    monkeypatch.setattr(W, "instances_write",
                        lambda items: inst_file.write_text(
                            dump_php_return(items), encoding="utf-8") or True)
    monkeypatch.setattr(W, "instance_switch_to", lambda s, sid, t: True)

    r = post("instance_save", {"inst_id": "", "inst_name": "我新建的",
                               "inst_container": "m7a",
                               "inst_dir": str(INST_DIR)})
    assert "已被实例" in r.text
    assert inst_file.read_text(encoding="utf-8") == before


# ================= Q-03：未登录 POST 响应类型 =================

def test_q03_fetch_gets_json_401(monkeypatch):
    """fetch/XHR（Accept: application/json）→ 401 + JSON，不再是 200 HTML。"""
    r = post_raw("restart", cookies=new_session(authed=False),
                 headers={"Accept": "application/json"})
    assert r.status_code == 401
    assert r.headers["content-type"].startswith("application/json")
    d = r.json()
    assert d["ok"] is False
    assert d["auth"] is False
    assert "登录已过期" in d["msg"]


def test_q03_xhr_header_gets_json_401():
    """X-Requested-With: XMLHttpRequest（jQuery 风格）同样返回 401 JSON。"""
    r = post_raw("restart", cookies=new_session(authed=False),
                 headers={"X-Requested-With": "XMLHttpRequest"})
    assert r.status_code == 401
    assert r.json()["auth"] is False


def test_q03_json_body_is_parseable_not_html():
    """回归：响应体必须是合法 JSON —— 旧实现是 HTML，r.json() 直接抛错。"""
    import json

    r = post_raw("restart", cookies=new_session(authed=False),
                 headers={"Accept": "application/json"})
    parsed = json.loads(r.text)          # 抛异常即回归
    assert isinstance(parsed, dict)
    assert "auth-card" not in r.text, "fetch 分支不得再返回登录页 HTML"


def test_q03_plain_form_still_gets_login_page():
    """普通表单提交（浏览器导航，无 JSON 头）→ 仍返回登录页 HTML。"""
    r = post_raw("restart", cookies=new_session(authed=False),
                 headers={"Accept": "text/html,application/xhtml+xml"})
    assert r.status_code == 200
    assert "auth-card" in r.text


def test_q03_default_client_headers_get_html():
    """TestClient 默认 Accept（*/*，无 XHR 头）→ 维持旧行为，HTML 登录页。"""
    r = post_raw("restart", cookies=new_session(authed=False))
    assert r.status_code == 200
    assert "auth-card" in r.text


def test_q03_authed_post_unaffected(monkeypatch):
    """已登录 POST 不经过未登录分支（不能因为改分流而影响正常提交）。"""
    monkeypatch.setattr(W, "ensure_running", lambda inst, timeout=15.0: {"started": False})
    r = post("restart", {}, cookies=new_session(authed=True))
    assert r.status_code == 200
    assert "auth-card" not in r.text


def test_q03_login_action_still_renders(monkeypatch):
    """action=login 走前置分支，不受未登录分流影响（PRG 303 → 跟随回登录页）。"""
    ck = new_session(authed=False)
    r = post_raw("login", {"password": ""}, cookies=ck,
                 headers={"Accept": "application/json"})
    assert r.status_code == 303, "login 分支不应被未登录 401 分流拦截"
    followed = _hop(r, ck)
    assert followed.status_code == 200
    assert "auth-card" in followed.text


def test_q03_get_page_unaffected():
    """页面渲染路径不经过 _forward_unauth_page：未登录 GET 仍 200 HTML。"""
    r = client.get("/", cookies={}, follow_redirects=False)
    assert r.status_code == 200
    assert "auth-card" in r.text


def test_q03_wants_json_helper():
    """_wants_json 判定的直接单元测试（大小写不敏感）。"""

    class _Req:
        def __init__(self, headers):
            self.headers = headers

    assert W._wants_json(_Req({"accept": "APPLICATION/JSON"})) is True
    assert W._wants_json(_Req({"x-requested-with": "XMLHttpRequest"})) is True
    assert W._wants_json(_Req({"accept": "text/html"})) is False
    assert W._wants_json(_Req({})) is False


# ================= Q-03 补充：真实浏览器判据（Sec-Fetch-*）=================
# 上面的 fetch 测试用显式 `Accept: application/json`，但**生产代码并不发这个头**：
# static/panel.js 的调用是 `fetch('action', {method:'POST', body: fd})`，浏览器为它
# 生成的是 `Accept: */*` + `Sec-Fetch-Mode: cors` + `Sec-Fetch-Dest: empty`；
# 而真正的表单提交是 `Sec-Fetch-Mode: navigate` + `Sec-Fetch-Dest: document`。
# 实测（headless Chromium 打请求头回显服务）确认了这两组取值。若只按 Accept/XHR
# 判定，线上所有 fetch 都会落回 HTML 分支 —— Q-03 等于没修。下面把这些真实
# 头组合钉成回归测试。

def test_q03_real_browser_fetch_headers_get_json_401():
    """真实 fetch() 的头（Accept:*/*、Sec-Fetch-Mode:cors、Sec-Fetch-Dest:empty）→ 401 JSON。"""
    r = post_raw("restart", cookies=new_session(authed=False), headers={
        "Accept": "*/*",
        "Sec-Fetch-Mode": "cors",
        "Sec-Fetch-Dest": "empty",
    })
    assert r.status_code == 401, "真实 fetch 必须命中 JSON 分支，否则前端仍报网络错误"
    assert r.headers["content-type"].startswith("application/json")
    assert r.json()["auth"] is False


def test_q03_real_browser_form_headers_get_html():
    """真实表单提交的头（Sec-Fetch-Mode:navigate、Sec-Fetch-Dest:document）→ HTML 登录页。"""
    r = post_raw("restart", cookies=new_session(authed=False), headers={
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Sec-Fetch-Mode": "navigate",
        "Sec-Fetch-Dest": "document",
    })
    assert r.status_code == 200
    assert "auth-card" in r.text


def test_q03_sec_fetch_dest_empty_alone_is_enough():
    """只有 Sec-Fetch-Dest: empty（某些浏览器不发 Mode）也判为异步请求。"""
    r = post_raw("restart", cookies=new_session(authed=False),
                 headers={"Accept": "*/*", "Sec-Fetch-Dest": "empty"})
    assert r.status_code == 401


def test_q03_sec_fetch_navigate_is_not_async():
    """只有 Sec-Fetch-Mode: navigate（无 Dest）→ 仍按表单处理，回 HTML。"""
    r = post_raw("restart", cookies=new_session(authed=False),
                 headers={"Accept": "*/*", "Sec-Fetch-Mode": "navigate"})
    assert r.status_code == 200
    assert "auth-card" in r.text


def test_q03_wants_json_sec_fetch_rules():
    """_wants_json 对 Sec-Fetch-* 的判定（大小写不敏感）。"""

    class _Req:
        def __init__(self, headers):
            self.headers = headers

    assert W._wants_json(_Req({"sec-fetch-mode": "cors"})) is True
    assert W._wants_json(_Req({"sec-fetch-mode": "NO-CORS"})) is True
    assert W._wants_json(_Req({"sec-fetch-dest": "empty"})) is True
    assert W._wants_json(_Req({"sec-fetch-mode": "navigate"})) is False
    assert W._wants_json(_Req({"sec-fetch-mode": "navigate",
                               "sec-fetch-dest": "document"})) is False
    # 空值不能误判
    assert W._wants_json(_Req({"sec-fetch-mode": "", "sec-fetch-dest": ""})) is False
