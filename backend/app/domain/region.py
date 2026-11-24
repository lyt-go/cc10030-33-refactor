"""兼容路径：``app.domain.region``（区域契约的短模块名）。"""
from __future__ import annotations

from app.services.region_contract import *  # noqa: F401,F403
from app.services.region_coverage import __getattr__  # noqa: F401
