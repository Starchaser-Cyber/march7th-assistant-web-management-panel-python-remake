"""Q-10 回归：Release 资产选择必须挑发布包，不能挑审计包。

背景：`_zip_asset()` 旧判据是 `re.match(r"^[Vv]\\d", name)`，而 Release 里
`V1.24.6.zip`（发布包，zip 根直接是 `panel_migr/`）与 `V1.24.6-audit.zip`
（审计包，带一层 `m7a_panel_V1.24.6_20261030/` 外层目录）**都**匹配该判据，
于是按 GitHub API 返回顺序取第一个 → 选中审计包 → `_zip_struct_ok()` 返回
False → 在线更新 100% 失败。

真实事故：V1.24.4 / V1.24.5 两个 Release 都同时挂了发布包与审计包，线上
「检查更新」显示有新版、点「立即更新」必然失败。本文件钉住选择逻辑。
"""
import pytest

from services import updateops as U


def _release(*names: str) -> dict:
    """构造 GitHub Release JSON 的最小形状。"""
    return {
        "tag_name": "V1.24.6",
        "assets": [
            {"name": n, "browser_download_url": f"https://example.invalid/{n}"}
            for n in names
        ],
    }


def _pick(*names: str) -> str:
    return U._zip_asset(_release(*names))[1]


# ---------- 核心回归：发布包与审计包共存 ----------


@pytest.mark.parametrize(
    "names, expect",
    [
        # GitHub API 实际返回顺序（m7a-audit-report 最先，审计包在发布包之前）
        (("m7a-audit-report.zip", "V1.24.6-audit.zip", "V1.24.6.zip"), "V1.24.6.zip"),
        (("V1.24.6-audit.zip", "V1.24.6.zip"), "V1.24.6.zip"),
        # 顺序反过来也必须选发布包
        (("V1.24.6.zip", "V1.24.6-audit.zip"), "V1.24.6.zip"),
        # 只有发布包
        (("V1.24.6.zip",), "V1.24.6.zip"),
        # 小写 v 前缀
        (("v1.24.6.zip",), "v1.24.6.zip"),
        # 四段版本号（预发布/补丁号）
        (("V1.24.6.1.zip", "V1.24.6.zip"), "V1.24.6.1.zip"),
        # 老版本命名（无 V 前缀）也不该被审计包挤掉
        (("weird.zip", "V1.24.6.zip"), "V1.24.6.zip"),
    ],
)
def test_picks_release_package(names, expect):
    assert _pick(*names) == expect


# ---------- 附加包（audit / report / handoff）不得被选中 ----------


@pytest.mark.parametrize(
    "names, expect",
    [
        # 没有严格 V{ver}.zip 时，优先挑不带附加标记的
        (("m7a-audit-report.zip", "V1.24.6-handoff.zip"), "V1.24.6-handoff.zip"),
        # 只有审计包 → 只能返回它（调用方会因结构校验失败而报错，但选择逻辑不该崩）
        (("m7a-audit-report.zip",), "m7a-audit-report.zip"),
        (("audit-only.zip",), "audit-only.zip"),
    ],
)
def test_aux_packages_only_as_fallback(names, expect):
    assert _pick(*names) == expect


def test_aux_regex_covers_known_suffixes():
    for bad in (
        "V1.24.6-audit.zip",
        "m7a-audit-report.zip",
        "V1.24.6-handoff.zip",
        "source.zip",
        "V1.24.6-src.zip",
    ):
        assert U._AUX_ZIP_RE.search(bad), f"{bad} 应被判为附加包"
    for good in ("V1.24.6.zip", "v1.24.6.zip", "weird.zip"):
        assert not U._AUX_ZIP_RE.search(good), f"{good} 不应被判为附加包"


# ---------- 边界与异常输入 ----------


def test_no_zip_assets_returns_empty():
    assert U._zip_asset(_release()) == ("", "")
    assert U._zip_asset({"tag_name": "V1.24.6", "assets": []}) == ("", "")


def test_non_dict_returns_empty():
    assert U._zip_asset(None) == ("", "")
    assert U._zip_asset([]) == ("", "")
    assert U._zip_asset("V1.24.6") == ("", "")


def test_ignores_non_zip_and_broken_entries():
    j = {
        "tag_name": "V1.24.6",
        "assets": [
            None,
            "not-a-dict",
            {"name": "notes.txt", "browser_download_url": "https://example.invalid/n"},
            {"name": "V1.24.6.zip", "browser_download_url": "https://example.invalid/ok"},
        ],
    }
    assert U._zip_asset(j) == ("https://example.invalid/ok", "V1.24.6.zip")


def test_returns_download_url_of_selected_asset():
    j = _release("V1.24.6-audit.zip", "V1.24.6.zip")
    url, name = U._zip_asset(j)
    assert name == "V1.24.6.zip"
    assert url == "https://example.invalid/V1.24.6.zip"


# ---------- 与结构校验联动：选中的包必须能通过 _zip_struct_ok ----------


def test_selected_asset_passes_struct_check(tmp_path):
    """选中的资产解压后顶层必须是 panel_migr/，否则 _zip_struct_ok 会拒绝。"""
    import zipfile

    src = tmp_path / "V1.24.6.zip"
    with zipfile.ZipFile(src, "w") as zf:
        for rel in ("main.py", "config.py", "routers/__init__.py", "static/panel.js"):
            zf.writestr(f"panel_migr/{rel}", "# stub\n")
    data = src.read_bytes()
    assert U._zip_struct_ok(data) is True

    # 审计包形状：多一层外层目录 → 必须被判为不合法
    audit = tmp_path / "V1.24.6-audit.zip"
    with zipfile.ZipFile(audit, "w") as zf:
        for rel in ("main.py", "config.py", "routers/__init__.py", "static/panel.js"):
            zf.writestr(f"m7a_panel_V1.24.6_20261030/panel_migr/{rel}", "# stub\n")
    assert U._zip_struct_ok(audit.read_bytes()) is False

    # 两个都挂着时，选择逻辑必须挑能通过校验的那个
    assert _pick("V1.24.6-audit.zip", "V1.24.6.zip") == "V1.24.6.zip"
