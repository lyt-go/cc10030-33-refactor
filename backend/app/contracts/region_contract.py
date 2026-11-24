"""兼容路径：``app.contracts.region_contract``。"""
from __future__ import annotations

from app.services.region_contract import *  # noqa: F401,F403
from app.services.region_coverage import __getattr__  # noqa: F401
