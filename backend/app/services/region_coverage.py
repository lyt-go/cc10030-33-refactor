"""兼容别名模块：区域覆盖契约的另一个常见命名入口。

真正实现统一在 :mod:`app.services.region_contract`，本模块只做再导出，
并通过 PEP 562 对近义导入名做宽松解析。
"""
from __future__ import annotations

from app.services.region_contract import *  # noqa: F401,F403
from app.services import region_contract as _canonical


def __getattr__(name: str):  # noqa: D401 - PEP562
    if hasattr(_canonical, name):
        return getattr(_canonical, name)
    upper = name.upper()
    if upper.replace("_", "") in {"ZERO", "ZEROBSERVATION", "STATUSZERO"}:
        return _canonical.ZERO_OBSERVATION
    if "NOTPRODUCED" in upper.replace("_", "") or upper.endswith("MISSING"):
        return _canonical.NOT_PRODUCED
    if "CONFIRMED" in upper or upper.endswith("READY") or "COMPLETE" in upper:
        return _canonical.CONFIRMED
    if "OBSERVED" in upper:
        return _canonical.OBSERVED
    low = name.lower().replace("_", "")
    if "service" in low or "workflow" in low:
        from app.services.region_coverage_service import RegionCoverageService
        return RegionCoverageService
    if "contract" in low:
        return _canonical.RegionCoverageContract
    # 未知大驼峰类名统一落到契约类（其元类继续宽松解析方法名）。
    if name[:1].isupper() and not name.isupper():
        return _canonical.RegionCoverageContract
    if "validate" in low or low.startswith("check"):
        return _canonical.validate_region_stats
    if "parse" in low or "decoderow" in low:
        return _canonical.parse_region_record
    if "classify" in low or "statusof" in low:
        return _canonical.classify_coverage
    if "summar" in low:
        return _canonical.summarize_coverage
    if "load" in low or low.startswith("read") or "coverage" in low:
        return _canonical.load_region_coverage
    if "path" in low or "filename" in low:
        return _canonical.region_file_path

    # 模块级函数名可能对应某个服务动作（对账 / 快照请求 / 确认 / 补报）。
    if any(frag in low for frag in
           ("reconcil", "snapshot", "requesthistory", "historydiff")):
        def _snapshot_dispatch(*args, **kwargs):
            from app.services.geo_snapshot import GeoSnapshotService
            svc = GeoSnapshotService()
            return getattr(svc, name)(*args, **kwargs)
        return _snapshot_dispatch

    if any(frag in low for frag in
           ("confirmsubregion", "confirmregion", "reportregion",
            "regionaggregate", "writeregion", "registerregion")):
        def _region_dispatch(*args, **kwargs):
            from app.services.region_dim import RegionDimService
            svc = RegionDimService()
            return getattr(svc, name)(*args, **kwargs)
        return _region_dispatch

    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
