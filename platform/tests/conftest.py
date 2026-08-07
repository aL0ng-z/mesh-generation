"""让平台测试从仓库根或 ``platform/`` 启动时具有一致导入边界。"""

from __future__ import annotations

import sys
from pathlib import Path


PLATFORM_DIR = Path(__file__).resolve().parents[1]
if str(PLATFORM_DIR) not in sys.path:
    sys.path.insert(0, str(PLATFORM_DIR))
