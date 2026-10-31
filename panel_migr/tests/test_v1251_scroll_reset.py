"""V1.25.1 前端回归：切换页面时视口滚回顶部。

背景：V1.25 之前 `switchTab()` 只切 `.page.active`，不动滚动位置。浏览器在
DOM 高度骤减（长配置页 → 短概览页）时会把 `window.scrollY` 钳到新上限，但
用户看到的是「切过去落在半空中」——尤其从配置页（几十屏）切到概览页。

覆盖：
1. `switchTab` 里有 `window.scrollTo(0, 0)` 且带 try 包裹（老浏览器不抛错）
2. 滚动重置只在**换页**时发生，同页重复点击不重置（`changed` 守卫）
3. 滚动重置发生在 `.page.active` 挂载**之后**（否则会按旧页高度钳位）
4. 模板里 4 个 nav-item + 4 个 tab-item 仍全部走 `switchTab(...)`（不要出现
   绕过 switchTab 直接改 class 的新入口）
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

ROOT = Path(__file__).resolve().parent.parent
JS = ROOT / "static" / "panel.js"
TPL = ROOT / "templates" / "panel.html.j2"


def _switch_tab_body() -> str:
    src = JS.read_text("utf-8")
    assert "function switchTab" in src
    return src.split("function switchTab", 1)[1].split("\n}\n", 1)[0]


def test_switch_tab_scrolls_to_top():
    body = _switch_tab_body()
    assert "window.scrollTo(0, 0)" in body, "切换页面后没有把视口滚回顶部"


def test_scroll_reset_is_guarded_by_try():
    body = _switch_tab_body()
    line = [ln for ln in body.splitlines() if "window.scrollTo(0, 0)" in ln][0]
    assert "try" in line and "catch" in line, "scrollTo 需 try/catch 包裹，避免旧浏览器抛错中断切页"


def test_scroll_reset_only_on_tab_change():
    body = _switch_tab_body()
    assert "changed" in body, "缺少「是否真的换页」的判断，同页重复点击会白白重置滚动"
    # 守卫必须在 scrollTo 之前
    assert body.index("changed") < body.index("window.scrollTo(0, 0)")


def test_scroll_reset_after_panel_activation():
    body = _switch_tab_body()
    activate = body.index("panel.classList.add('active')")
    scroll = body.index("window.scrollTo(0, 0)")
    assert activate < scroll, "必须在 .page.active 挂载之后再重置滚动，否则按旧页高度钳位"


def test_active_tab_state_is_tracked():
    src = JS.read_text("utf-8")
    assert re.search(r"var\s+_activeTab\s*=\s*''", src), "缺少 _activeTab 模块级状态"
    assert "_activeTab = name;" in src, "_activeTab 没有在 switchTab 里更新"


def test_nav_entries_go_through_switch_tab():
    tpl = TPL.read_text("utf-8")
    # 桌面导航 4 项 + 移动端 tabbar 4 项
    nav = re.findall(r'class="nav-item[^"]*"\s+data-tab="(\w+)"\s+onclick="switchTab\(\'\1\'\)"', tpl)
    tab = re.findall(r'class="tab-item[^"]*"\s+data-tab="(\w+)"\s+onclick="switchTab\(\'\1\'\)"', tpl)
    assert sorted(nav) == ["config", "log", "overview", "tasks"], nav
    assert sorted(tab) == ["config", "log", "overview", "tasks"], tab
