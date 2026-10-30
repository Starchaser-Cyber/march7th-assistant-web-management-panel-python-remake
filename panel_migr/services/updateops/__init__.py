"""自动更新 / 镜像更新 / 版本备份回滚 —— 等价 PHP update_*/image_*/backup_* 全套。

v1.20 起自动更新改为「Release 整包 ZIP 自更新」（旧版 PHP release 仍保留 index.php
回退流程）：下载 V*.zip → 校验结构与版本一致性 → 整面板备份到 backups/panel_*.zip
→ 解压换入（data/ backups/ 运行数据不动）→ 版本复核（失败自动回滚）→ 延迟重启。

网络请求走 httpx（同步，handler 在线程池执行）；失败语义与 PHP curl 版一致：
code=0 表示连接失败，body='' 表示无内容。
"""

# v1.24.7（方向C）：本模块从 1,894 行的单文件拆成 services/updateops/ 包。
# 这里只做「门面」：常量/状态 + 全部名字的 re-export，保证
#   from services.updateops import X   与   from services import updateops as U; U.X
# 两种用法与拆分前完全一致（外部模块的 import 行一个字都不用改）。
#
# ⚠ monkeypatch 兼容：测试大量使用 `monkeypatch.setattr(U, "http_get", ...)`。
# re-export 只是名字绑定，子模块内部若直接绑定函数对象，patch 包级名字不会生效。
# 因此子模块一律通过 `from . import _rt` 在**调用点**取包对象属性（见 _rt.py），
# 包级名字与子模块看到的始终是同一个对象。

from __future__ import annotations

import threading

# —— 与拆分前保持一致的「模块级可用名字」：测试会 patch U.cfg / U.time / U.httpx /
# U.shutil / U.run_cmd，缺一个就会 AttributeError。
import httpx  # noqa: F401  （re-export：测试 patch U.httpx.get / U.httpx.stream）
import shutil  # noqa: F401  （re-export：测试 patch U.shutil.move / U.shutil.which）
import time  # noqa: F401  （re-export：测试整体替换 U.time）

import config as cfg  # noqa: F401,E402  （re-export：测试 patch U.cfg.BASE_DIR）
from services.shell import run_cmd  # noqa: F401  （re-export：测试 patch U.run_cmd）

# 更新互斥（v1.24.4 P1-3）：同一秒并发会共用 .stage_<ts>/.old_<ts> 目录，互相
# rmtree 掉对方正在使用的目录。do_update 全程非阻塞持锁，拿不到直接拒绝。
# 拆分后仍定义在包级：测试会直接 `U._UPDATE_LOCK.acquire(blocking=False)`，
# 必须与 orchestrate.do_update 用的是**同一个**锁对象。
_UPDATE_LOCK = threading.Lock()

# 最近一次整包备份的失败原因（v1.24.4 P1-5）。由 zipops._backup_panel_zip 通过
# _rt.set() 写回包级，供 orchestrate._do_update_locked 如实转述给用户。
_LAST_BACKUP_ERR = ""

from . import _rt  # noqa: E402,F401  （re-export：子模块经它回读包级名字）

from .http import (  # noqa: E402
    UA,
    _MAX_DOWNLOAD,
    http_get,
    http_bytes,
    http_post,
    _probe,
)

from .version import (  # noqa: E402
    _nums,
    _version_gt,
    _version_eq,
    version_gt,
    version_eq,
    _version_from_text,
    _to_ts,
)

from .release import (  # noqa: E402
    MIRRORS,
    update_api_url,
    update_raw_url,
    update_raw_urls,
    panel_update_mode,
    _release_json,
    _VER_ZIP_RE,
    _AUX_ZIP_RE,
    _zip_asset,
    _zip_urls,
    _sha256,
    _asset_digest,
    check_update,
    test_update_source,
)

from .zipops import (  # noqa: E402
    _MAX_TOTAL_SIZE,
    _MAX_ENTRIES,
    _MAX_FILE_SIZE,
    _MAX_RATIO,
    _zip_struct_ok,
    _zip_new_version,
    _extract_zip,
    _BackupError,
    _ZIP_MIN_TS,
    _zip_date_time,
    _zip_tree,
    _backup_panel_zip,
)

from .runtime import (  # noqa: E402
    CODE_ROOT,
    _code_root,
    _installed_version,
    _installed_version_in,
    _CODE_VITAL,
    _code_intact,
    _live_code_intact,
    _venv_pip_sync,
    PIP_MIRROR,
    _runtime_python_ok,
    _ensure_runtime,
)

from .swap import (  # noqa: E402
    _PROTECT_TOP,
    _PROTECT_DIRS,
    _swap_stage,
    _restore_old,
    _cleanup_dirs,
    _old_has_content,
    _recover_interrupted,
)

from .restart import (  # noqa: E402
    _RESTART_UNIT,
    _HEALTH_PROBE_SCRIPT,
    _PROBE_SCRIPT_SRC,
    _schedule_restart,
    _self_under_unit,
    _probe_python,
    _write_probe_script,
    _detach_restart_and_probe,
    _restart_and_health_check,
)

from .backups import (  # noqa: E402
    BACKUP_DIR,
    INDEX_FILE,
    backups_dir,
    _entry,
    backups_list,
    backups_prune,
    backup_current_index,
    _rollback_panel,
    backup_rollback,
)

from .ghcr import (  # noqa: E402
    IMAGE_CACHE,
    IMAGE_REPO,
    GHCR_PREFIXES,
    _ghcr_auth,
    ghcr_config_created,
    ghcr_latest_created,
    assistant_image_candidates,
    image_update,
    image_check,
    _write_cache,
    _compose,
)

from .orchestrate import (  # noqa: E402
    do_update,
    _do_update_locked,
    _apply_zip,
)

__all__ = [
    "BACKUP_DIR",
    "CODE_ROOT",
    "GHCR_PREFIXES",
    "IMAGE_CACHE",
    "IMAGE_REPO",
    "INDEX_FILE",
    "MIRRORS",
    "PIP_MIRROR",
    "UA",
    "_AUX_ZIP_RE",
    "_BackupError",
    "_CODE_VITAL",
    "_HEALTH_PROBE_SCRIPT",
    "_LAST_BACKUP_ERR",
    "_MAX_DOWNLOAD",
    "_MAX_ENTRIES",
    "_MAX_FILE_SIZE",
    "_MAX_RATIO",
    "_MAX_TOTAL_SIZE",
    "_PROBE_SCRIPT_SRC",
    "_PROTECT_DIRS",
    "_PROTECT_TOP",
    "_RESTART_UNIT",
    "_UPDATE_LOCK",
    "_VER_ZIP_RE",
    "_ZIP_MIN_TS",
    "_apply_zip",
    "_asset_digest",
    "_backup_panel_zip",
    "_cleanup_dirs",
    "_code_intact",
    "_code_root",
    "_compose",
    "_detach_restart_and_probe",
    "_do_update_locked",
    "_ensure_runtime",
    "_entry",
    "_extract_zip",
    "_ghcr_auth",
    "_installed_version",
    "_installed_version_in",
    "_live_code_intact",
    "_nums",
    "_old_has_content",
    "_probe",
    "_probe_python",
    "_recover_interrupted",
    "_release_json",
    "_restart_and_health_check",
    "_restore_old",
    "_rollback_panel",
    "_runtime_python_ok",
    "_schedule_restart",
    "_self_under_unit",
    "_sha256",
    "_swap_stage",
    "_to_ts",
    "_venv_pip_sync",
    "_version_eq",
    "_version_from_text",
    "_version_gt",
    "_write_cache",
    "_write_probe_script",
    "_zip_asset",
    "_zip_date_time",
    "_zip_new_version",
    "_zip_struct_ok",
    "_zip_tree",
    "_zip_urls",
    "assistant_image_candidates",
    "backup_current_index",
    "backup_rollback",
    "backups_dir",
    "backups_list",
    "backups_prune",
    "check_update",
    "do_update",
    "ghcr_config_created",
    "ghcr_latest_created",
    "http_bytes",
    "http_get",
    "http_post",
    "image_check",
    "image_update",
    "panel_update_mode",
    "test_update_source",
    "update_api_url",
    "update_raw_url",
    "update_raw_urls",
    "version_eq",
    "version_gt",
]
