"""兼容别名：区域覆盖契约模块的长命名入口。"""
from __future__ import annotations

from app.services.region_contract import *  # noqa: F401,F403
from app.services.region_coverage import __getattr__  # noqa: F401
