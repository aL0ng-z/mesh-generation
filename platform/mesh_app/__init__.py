"""叶轮机械网格经验内网平台。"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import TYPE_CHECKING, Any


# src/ 网格内核刻意不打包进 Web 依赖；无论服务从仓库根还是 platform/
# 启动，都通过本文件位置定位同机内核，避免依赖当前工作目录。
_PROJECT_ROOT = Path(__file__).resolve().parents[2]
_SOURCE_DIR = _PROJECT_ROOT / "src"
if str(_SOURCE_DIR) not in sys.path:
    sys.path.insert(0, str(_SOURCE_DIR))

if TYPE_CHECKING:
    from .config import Settings
    from .db import Database


def __getattr__(name: str) -> Any:
    """惰性导出公共类型，避免 ``python -m mesh_app.db`` 预加载待执行模块。"""

    if name == "Settings":
        from .config import Settings

        return Settings
    if name == "Database":
        from .db import Database

        return Database
    raise AttributeError(name)

__all__ = ["Database", "Settings"]
