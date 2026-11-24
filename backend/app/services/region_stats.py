"""兼容模块名：``app.services.region_stats``（契约与服务动作的宽松入口）。"""
from __future__ import annotations

from app.services.region_contract import *  # noqa: F401,F403
from app.services.region_coverage import __getattr__  # noqa: F401
