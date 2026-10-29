"""V1.24.4 修复回归测试（2026-10-29 第三方审计 D_handlers/B_auth/A_update）。

覆盖：
- P0-1 配置页两次保存把 config.yaml 写成非法 YAML（textarea 多行值）
- P1-14 `await` 同步函数 render_fallback → 所有非面板路径 500
- P1-15 已登录 GET 在事件循环里同步跑 alert_tick（改走 to_thread）
- P1-22 面板生成的 3 条 cron 用 `?key=` 而后端只认 `X-M7A-Key` 头
- 顺手：config_read_raw 上限、config_backup 同秒不覆盖、atomic 写、非 ASCII key 不 500
"""
import re
import sys
import uuid
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import config as cfg
from fastapi.testclient import TestClient

from main import app
from services import configops as C
from services.instances import instances_write

client = TestClient(app)

_TMP = Path(__file__).resolve().parent / "_tmp" / "v1244"


def _mk_inst(name: str) -> dict:
    d = _TMP / name
    (d / "logs").mkdir(parents=True, exist_ok=True)
    (d / "config.yaml").write_text(
        "locales:\n  zh_CN: 简体中文\n"
        "power_enable: true\n"
        "log_retention_days: 30\n"
        "notify_webhook_headers: \"Content-Type: application/json\"\n"
        "notify_webhook_body: '{\"text\": \"hi\"}'\n"
        "notify_custom_image: \"ghcr.io/x/y:latest\"\n"
        "notify_custom_data: \"a=b\"\n",
        "utf-8",
    )
    return {"id": name, "name": name, "container": name,
            "dir": str(d), "default": True}


def _session(authed=True) -> dict:
    sid = "v44" + uuid.uuid4().hex[:10]
    parts = ["m7a_panel_auth|b:1;"] if authed else []
    p = Path(cfg.SESSION_SAVE_PATH)
    p.mkdir(parents=True, exist_ok=True)
    (p / f"sess_{sid}").write_text("".join(parts), encoding="utf-8")
    return {"PHPSESSID": sid}


# ================= P0-1：多行值不得写坏 config.yaml =================

MULTILINE_CASES = [
    "line1\nline2",
    "line1\nline2\nline3",
    "带中文的多行\n第二行\n第三行",
    "含\"引号\"和\\反斜杠\n换行",
    "tab\there\nnext",
    "末尾有换行\n",
    "# 井号开头\n还有 # 行内井号",
]


@pytest.mark.parametrize("raw", MULTILINE_CASES)
def test_p01_yaml_format_val_single_line_and_valid(raw):
    """yaml_format_val 的 str/select 分支必须产出单行合法 YAML 标量。"""
    dq = C.yaml_format_val(raw, "str")
    assert "\n" not in dq and "\r" not in dq, f"输出仍含换行: {dq!r}"
    got = yaml.safe_load("k: " + dq + "\n")
    assert got == {"k": raw}, (dq, got)


@pytest.mark.parametrize("raw", MULTILINE_CASES)
def test_p01_two_round_save_stays_valid_yaml(raw, tmp_path):
    """完整回归：写入多行值 → 读回 → 再写入，两轮后 yaml.safe_load 仍成功。"""
    inst = _mk_inst("p01_" + uuid.uuid4().hex[:6])
    key = "notify_webhook_body"

    # 第一轮：模拟 write.py::_form_updates（textarea → yaml_format_val）
    upd1 = {key: C.yaml_format_val(raw, "textarea")}
    r1 = C.config_save_form(inst, upd1)
    assert r1["ok"] is True, r1
    text1 = Path(inst["dir"], "config.yaml").read_text("utf-8")
    d1 = yaml.safe_load(text1)          # 第一轮后必须合法
    assert d1[key] == raw, d1[key]

    # 读回（面板表单初值）
    back = C.yaml_read_simple(inst)
    assert back[key] == raw, f"读回丢内容: {back[key]!r}"

    # 第二轮：把读回值再保存一次
    upd2 = {key: C.yaml_format_val(back[key], "textarea")}
    r2 = C.config_save_form(inst, upd2)
    assert r2["ok"] is True, r2
    text2 = Path(inst["dir"], "config.yaml").read_text("utf-8")
    d2 = yaml.safe_load(text2)          # 两轮后仍合法
    assert d2[key] == raw, d2[key]
    # 其它键不能被顺手改坏
    assert d2["power_enable"] is True
    assert d2["locales"]["zh_CN"] == "简体中文"


def test_p01_yaml_read_simple_unescapes_not_stripslashes():
    """`\\n` 必须还原成换行；PHP stripslashes 的错解（`\\n`→`n`）要杜绝。"""
    assert C._yaml_unescape("a\\nb") == "a\nb"
    assert C._yaml_unescape("a\\tb") == "a\tb"
    assert C._yaml_unescape("a\\\\b") == "a\\b"
    assert C._yaml_unescape('a\\"b') == 'a"b'
    assert C._yaml_unescape("plain") == "plain"
    # 旧实现会把 \n 两字符变成 n，这里显式钉住回归
    assert C._stripslashes("a\\nb") != "a\nb"


def test_p01_read_simple_reads_multiline_quoted_value(tmp_path):
    """yaml_read_simple 能读回双引号包裹的转义多行值（原实现只剩 `\"{`）。"""
    inst = _mk_inst("p01_read_" + uuid.uuid4().hex[:6])
    raw = '{"msg": "第一行\\n第二行"}'
    C.config_save_form(inst, {"notify_webhook_body": C.yaml_format_val(raw, "textarea")})
    back = C.yaml_read_simple(inst)
    assert back["notify_webhook_body"] == raw


def test_p01_repairs_already_broken_file():
    """已被旧版写坏（值跨行）的文件，再次保存该键应恢复成合法 YAML。"""
    inst = _mk_inst("p01_broken_" + uuid.uuid4().hex[:6])
    p = Path(inst["dir"], "config.yaml")
    p.write_text(
        "locales:\n  zh_CN: 简体中文\n"
        "notify_webhook_body: \"{\"msg\": \"hi\",\n"
        "  \"k\": 1}\"\n"
        "power_enable: true\n",
        "utf-8",
    )
    r = C.config_save_form(inst, {"notify_webhook_body": C.yaml_format_val("ok\nnew", "textarea")})
    assert r["ok"] is True
    d = yaml.safe_load(p.read_text("utf-8"))
    assert d["notify_webhook_body"] == "ok\nnew"
    assert d["power_enable"] is True          # 后续键不能被吞掉


def test_p01_hash_in_value_survives_comment_split():
    """值里带 `#` 时不能被当成行尾注释截断。"""
    inst = _mk_inst("p01_hash_" + uuid.uuid4().hex[:6])
    raw = "color: #ff0000\nnext: #abcdef"
    C.config_save_form(inst, {"notify_webhook_body": C.yaml_format_val(raw, "textarea")})
    d = yaml.safe_load(Path(inst["dir"], "config.yaml").read_text("utf-8"))
    assert d["notify_webhook_body"] == raw
    # 真注释仍要保留
    p = Path(inst["dir"], "config.yaml")
    p.write_text('notify_custom_data: "a=b"  # 保留我\npower_enable: true\n', "utf-8")
    C.config_save_form(inst, {"notify_custom_data": C.yaml_format_val("c=d", "str")})
    text = p.read_text("utf-8")
    assert "# 保留我" in text
    assert yaml.safe_load(text)["notify_custom_data"] == "c=d"


def test_p01_dq_wrap_accepts_prebuilt_quoted_value():
    """write.py 对 password 字段自己拼了引号，落盘仍需合法。"""
    assert C._dq_wrap('"already"') == '"already"'
    assert C._dq_wrap('"raw\nnewline"') == '"raw\\nnewline"'
    assert yaml.safe_load('k: ' + C._dq_wrap('a"b'))["k"] == 'a"b'


def test_p01_http_two_round_save_end_to_end():
    """端到端复现审计现场：表单 POST 保存两次，config.yaml 仍是合法 YAML。"""
    inst = _mk_inst("p01_http_" + uuid.uuid4().hex[:6])
    instances_write([inst])
    cookies = _session()
    page = client.get("/", cookies=cookies, follow_redirects=False)
    m = re.search(r'name="csrf" value="([^"]+)"', page.text)
    assert m, "面板首页应有 csrf"
    csrf = m.group(1)
    multiline = '{"text": "line1"}\n第二行\n"third"'
    for _ in range(2):
        r = client.post(
            "/action",
            data={"action": "save_config_form", "csrf": csrf,
                  "cfg_notify_webhook_body": multiline,
                  "cfg_power_enable": "on"},
            cookies=cookies, follow_redirects=False,
        )
        assert r.status_code not in (500, 502), r.status_code
        text = Path(inst["dir"], "config.yaml").read_text("utf-8")
        doc = yaml.safe_load(text)          # 每一轮后都必须能解析
        assert doc["notify_webhook_body"] == multiline, doc["notify_webhook_body"]
        assert doc["power_enable"] is True


# ================= 顺手：config.yaml 读写上限 / 原子写 / 备份 =================

def test_p01_bool_int_fields_stay_unquoted():
    """回归：config_save_form 不能把 bool/int 字段写成字符串（`true`→"true"）。"""
    inst = _mk_inst("p01_types_" + uuid.uuid4().hex[:6])
    r = C.config_save_form(inst, {
        "power_enable": "true",
        "log_retention_days": "7",
        "notify_custom_data": "a=b",
    })
    assert r["ok"] is True, r
    d = yaml.safe_load(Path(inst["dir"], "config.yaml").read_text("utf-8"))
    assert d["power_enable"] is True, d["power_enable"]
    assert d["log_retention_days"] == 7, d["log_retention_days"]
    assert d["notify_custom_data"] == "a=b"


def test_p01_multiline_still_quoted_after_type_aware():
    """回归：按类型分流后，textarea 多行值仍必须加引号且合法。"""
    inst = _mk_inst("p01_types2_" + uuid.uuid4().hex[:6])
    raw = "第一行\n第二行"
    C.config_save_form(inst, {"notify_webhook_body": C.yaml_format_val(raw, "textarea")})
    d = yaml.safe_load(Path(inst["dir"], "config.yaml").read_text("utf-8"))
    assert d["notify_webhook_body"] == raw


def test_config_read_raw_limit_raised_to_1mb():
    inst = _mk_inst("big_" + uuid.uuid4().hex[:6])
    filler = "# " + ("x" * 80) + "\n"
    body = "locales:\n  zh_CN: 简体中文\npower_enable: true\n" + filler * 2000
    assert len(body.encode("utf-8")) > 65536
    Path(inst["dir"], "config.yaml").write_text(body, "utf-8")
    raw = C.config_read_raw(inst)
    assert raw is not None
    assert len(raw.encode("utf-8")) >= len(body.encode("utf-8")) - 8
    assert raw.rstrip().endswith("x" * 80)


def test_config_atomic_write_keeps_permissions():
    """原子写（mkstemp 0600）不能把 config.yaml 权限从 0644 降成 0600。"""
    import os

    inst = _mk_inst("perm_" + uuid.uuid4().hex[:6])
    f = Path(inst["dir"], "config.yaml")
    os.chmod(f, 0o644)
    C.config_save_form(inst, {"power_enable": "true"})
    assert (f.stat().st_mode & 0o777) == 0o644
    C.config_save_text(inst, "locales:\n  zh_CN: y\npower_enable: true\n")
    assert (f.stat().st_mode & 0o777) == 0o644


def test_config_save_text_rejects_oversize():
    inst = _mk_inst("oversize_" + uuid.uuid4().hex[:6])
    huge = "locales:\n  zh_CN: 简体中文\n" + ("# " + "y" * 100 + "\n") * 20000
    r = C.config_save_text(inst, huge)
    assert r["ok"] is False and "过大" in r["msg"]


def test_config_backup_same_second_no_overwrite():
    inst = _mk_inst("bak_" + uuid.uuid4().hex[:6])
    a = C.config_backup(inst)
    b = C.config_backup(inst)
    assert a and b and a != b, (a, b)
    assert Path(a).is_file() and Path(b).is_file()


def test_config_save_form_writes_atomically(tmp_path):
    """写 config.yaml 必须走 atomic_write_text（同目录临时文件 + os.replace）。"""
    src = (ROOT / "services" / "configops.py").read_text("utf-8")
    assert "_atomic_write_text" in src
    assert "from phpfile import atomic_write_text" in src
    # 不再有直接 Path(...).write_text 落 config.yaml
    assert "p.write_text(" not in src
    assert "Path(instance_config(inst)).write_text(" not in src


def test_instances_save_invalidates_php_cache():
    """panel_config_save / instances_write 写完要失效 phpfile 缓存（否则读到旧值）。"""
    src = (ROOT / "services" / "instances.py").read_text("utf-8")
    assert src.count("invalidate_php_cache(") >= 2


# ================= P1-14：await 同步函数 → 非面板路径 500 =================

@pytest.mark.parametrize("path", [
    "/favicon.ico", "/robots.txt", "/manifest.json", "/some/deep/link",
])
def test_p14_non_panel_get_not_500(path):
    r = client.get(path, cookies=_session(), follow_redirects=False)
    assert r.status_code != 500, f"{path} 仍 500"


def test_p14_non_panel_post_not_500():
    r = client.post("/foo", data={"a": "b"}, cookies=_session(), follow_redirects=False)
    assert r.status_code != 500


def test_p14_no_await_on_sync_render_fallback():
    """main.py 里 render_fallback 是同步函数，不能 await（TypeError）。"""
    from routers.panel import render_fallback
    import inspect

    assert not inspect.iscoroutinefunction(render_fallback)
    src = (ROOT / "main.py").read_text("utf-8")
    assert "await render_fallback(" not in src, "仍有 await 同步函数"


def test_p14_all_awaits_are_async():
    """main.py 中每一处 `await f(` 的 f 都必须是 async 函数（防同类误用）。"""
    import inspect

    src = (ROOT / "main.py").read_text("utf-8")
    names = set(re.findall(r"await\s+([A-Za-z_]\w*)\s*\(", src))
    assert names, "main.py 应有 await 调用"
    mod = sys.modules["main"]
    for name in names:
        fn = getattr(mod, name, None)
        if fn is None:
            continue  # 可能是局部变量 / 方法调用，跳过
        if inspect.isfunction(fn):
            assert inspect.iscoroutinefunction(fn), f"await 了同步函数 {name}"


# ================= P1-15：同步重函数不得阻塞事件循环 =================

def test_p15_page_response_runs_in_thread():
    """dispatch_get 必须用 to_thread 跑 page_response（内部 alert_tick 有子进程）。"""
    src = (ROOT / "main.py").read_text("utf-8")
    seg = src.split("async def dispatch_get")[1].split("async def dispatch_post")[0]
    assert "to_thread" in seg, "dispatch_get 仍在事件循环里同步渲染/巡检"
    assert "page_response" in seg


def test_p15_to_thread_actually_used(monkeypatch):
    """行为级：GET / 时 page_response 在非主线程执行。"""
    import anyio
    import threading

    import services.pagectx as P

    seen = {}
    real = P.page_response

    def spy(request, **kw):
        seen["thread"] = threading.current_thread().name
        seen["main"] = threading.main_thread().name
        return real(request, **kw)

    monkeypatch.setattr(P, "page_response", spy)
    r = client.get("/", cookies=_session(), follow_redirects=False)
    assert r.status_code == 200
    assert "thread" in seen, "page_response 未被调用"
    # 线程池线程名形如 AnyIO worker thread
    assert seen["thread"] != seen["main"], f"仍在主线程执行: {seen}"


# ================= P1-22：cron 用 X-M7A-Key 头 =================

def test_p22_cron_commands_use_header_not_query():
    """面板生成的 3 条 cron 必须用 -H "X-M7A-Key: ..."，不能把 key 放 URL。"""
    src = (ROOT / "services" / "pagectx.py").read_text("utf-8")
    assert "&key=" not in src, "仍有把 key 拼进 URL query 的 cron"
    assert 'key={' not in src
    assert src.count("X-M7A-Key: ") >= 3, "三条 cron 都要用 header 传 key"


def test_p22_rendered_page_cron_lines_use_header():
    """渲染出来的实际 cron 文本里也必须是 header 形式。"""
    inst = _mk_inst("cron_" + uuid.uuid4().hex[:6])
    instances_write([inst])
    r = client.get("/", cookies=_session(), follow_redirects=False)
    assert r.status_code == 200
    html = r.text
    # 模板 autoescape 会把 " 转成 &#34;、> 转成 &gt;，先还原再断言
    plain = (html.replace("&#34;", '"').replace("&quot;", '"')
                 .replace("&gt;", ">").replace("&amp;", "&"))
    for param in ("monitor_sampler=1", "alerter=1", "scheduler=1"):
        pat = (r'curl -s -H "X-M7A-Key: [^"]+" '
               r'"[^"]*/action\?' + re.escape(param) + '"')
        assert re.search(pat, plain), f"cron 未用 header 传 key: {param}"
    assert "&key=" not in plain
    assert "?key=" not in plain


def test_p22_header_key_accepted_query_rejected(monkeypatch):
    """header 形式 200 / query 形式 403（默认）——即修复后 cron 真的能跑。"""
    import routers.get_write as G
    from services.instances import panel_config_save

    panel_config_save({"scheduler_key": "sk-header-1"})
    monkeypatch.setattr(G, "schedule_run_due",
                        lambda inst, now=None: {"ok": True, "ran": []})
    monkeypatch.setattr(G, "alert_tick", lambda inst, force=False: {"pushed": False})
    monkeypatch.setattr(G, "heartbeat_touch", lambda inst, who: None)

    ok = client.get("/?scheduler=1",
                    headers={"X-M7A-Key": "sk-header-1"},
                    cookies={}, follow_redirects=False)
    assert ok.status_code == 200, ok.text

    bad = client.get("/?scheduler=1&key=sk-header-1",
                     cookies={}, follow_redirects=False)
    assert bad.status_code == 403


def test_p22_monitor_sampler_header_ok_query_invalid(monkeypatch):
    import routers.get_write as G
    from services.instances import panel_config_save

    panel_config_save({"monitor_key": "mk-header-1"})
    monkeypatch.setattr(G, "services_monitor_sample",
                        lambda inst, iv: {"sampled": True, "running": False,
                                          "data": {"points": []}})
    monkeypatch.setattr(G, "alert_tick", lambda inst, force=False: {"pushed": False})
    monkeypatch.setattr(G, "heartbeat_touch", lambda inst, who: None)

    ok = client.get("/?monitor_sampler=1",
                    headers={"X-M7A-Key": "mk-header-1"},
                    cookies={}, follow_redirects=False)
    assert ok.status_code == 200 and ok.json().get("ok") is True, ok.text

    bad = client.get("/?monitor_sampler=1&key=mk-header-1",
                     cookies={}, follow_redirects=False)
    assert bad.json().get("ok") is not True


@pytest.mark.parametrize("bad_key", ["中文", "中文key", "ünicode"])
def test_p22_non_ascii_header_key_no_500(bad_key):
    """非 ASCII 头值不能让 hmac.compare_digest 抛 TypeError → 未认证 500。

    httpx 客户端拒绝编码非 ASCII 头值，这里直接发原始字节（服务端按 latin-1
    解码成非 ASCII str，正是线上 curl -H "X-M7A-Key: 中文" 的形态）。
    """
    from services.instances import panel_config_save

    panel_config_save({"scheduler_key": "sk-ascii", "monitor_key": "mk-ascii",
                       "alerter_key": "al-ascii"})
    hdr = {b"X-M7A-Key": bad_key.encode("utf-8")}
    for path, code in (("/?scheduler=1", 403), ("/?alerter=1", 403)):
        r = client.get(path, headers=hdr, cookies={}, follow_redirects=False)
        assert r.status_code == code, f"{path} → {r.status_code}: {r.text}"
    r = client.get("/?monitor_sampler=1", headers=hdr,
                   cookies={}, follow_redirects=False)
    assert r.status_code != 500, r.text
    assert r.json().get("ok") is not True


def test_p22_req_key_filters_non_ascii():
    """_req_key 入口即过滤非 ASCII，两条比对路径都不会炸。"""
    import routers.get_write as G

    class _R:
        def __init__(self, hdr):
            self.headers = {"x-m7a-key": hdr}
            self.query_params = {}

    assert G._req_key(_R("中文")) == ""
    assert G._req_key(_R("ascii-ok")) == "ascii-ok"
    assert G._key_eq("中文", "中文") is False
    assert G._key_eq("", "") is False
    assert G._key_eq("abc", "abc") is True


# ================= 前端 P3：转义与隐式 event =================

def test_p3_search_hint_escapes_query():
    src = (ROOT / "static" / "panel.js").read_text("utf-8")
    assert "没有找到「' + q + '」相关内容" not in src, "搜索结果提示仍未转义 q"
    assert "没有找到「' + escapeHtml(q) + '」相关内容" in src


def test_p3_no_implicit_global_event():
    src = (ROOT / "static" / "panel.js").read_text("utf-8")
    # switchCfgTab / updateAssistantImage 不再直接读隐式全局 event
    assert "event.target.classList" not in src
    assert "var btn = event.target;" not in src
    seg = src.split("function switchCfgTab")[1].split("function toggleCfgGroup")[0]
    assert "window.event" in seg, "需保留旧模板无参调用的回退"
    seg2 = src.split("function updateAssistantImage")[1].split("function ")[0]
    assert "window.event" in seg2
