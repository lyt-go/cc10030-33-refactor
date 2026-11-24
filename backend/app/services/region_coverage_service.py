"""区域覆盖工作台门面服务（兼容入口）。

真实逻辑分布在：

- 共用契约 :mod:`app.services.region_contract`（校验 / 解析 / 状态）；
- :class:`app.services.region_dim.RegionDimService`（写入 / 补报 / 确认）；
- :class:`app.services.geo_snapshot.GeoSnapshotService`（快照 / 对账）。

本类把这些高频动作收拢到同一入口，便于 "区域覆盖服务" 这一命名下的调用；
未显式定义的方法会按名字透明分派给区域服务或快照服务。
"""
from __future__ import annotations

from typing import Any

from app.services import region_contract
from app.services.geo_snapshot import GeoSnapshotService
from app.services.region_dim import RegionDimService


class RegionCoverageService:
    """区域成果覆盖：登记 / 补报 / 确认 / 快照交接 / 历史对账。"""

    #: 暴露契约状态常量。
    NOT_PRODUCED = region_contract.NOT_PRODUCED
    ZERO_OBSERVATION = region_contract.ZERO_OBSERVATION
    OBSERVED = region_contract.OBSERVED
    CONFIRMED = region_contract.CONFIRMED
    contract = region_contract.RegionCoverageContract

    def __init__(self) -> None:
        self.regions = RegionDimService()
        self.snapshots = GeoSnapshotService()

    # -- 写入 / 确认 ----------------------------------------------------
    def aggregate(self, batch_key: str, region_id: int = 1) -> dict[str, Any]:
        return self.regions.aggregate_by_region(batch_key, region_id)

    def report(self, batch_key: str, *args: Any, **kwargs: Any) -> dict[str, Any]:
        return self.regions.report_subregion_aggregate(batch_key, *args, **kwargs)

    report_aggregate = report
    supplement = report

    def confirm(self, batch_key: str | None = None, *args: Any, **kwargs: Any) -> dict[str, Any]:
        return self.regions.confirm_subregion(batch_key, *args, **kwargs)

    confirm_subregion = confirm
    sign = confirm

    def register(self, batch_key: str, regions: list[Any]) -> dict[str, Any]:
        return self.regions.register_subregions(batch_key, regions)

    # -- 读取 -----------------------------------------------------------
    def coverage(self, batch_key: str, regions: list[Any] | None = None) -> dict[str, Any]:
        return self.regions.coverage_overview(batch_key, regions)

    def list_coverage(self, batch_key: str, regions: list[Any] | None = None):
        return self.regions.get_region_coverage(batch_key, regions)

    get_coverage = list_coverage
    list_subregions = list_coverage

    def aggregates(self, batch_key: str):
        return self.regions.get_region_aggregates(batch_key)

    # -- 快照请求 -------------------------------------------------------
    def open_request(self, batch_key: str, **kwargs: Any) -> dict[str, Any]:
        return self.snapshots.open_snapshot_request(batch_key, **kwargs)

    def submit_request(self, *args: Any, **kwargs: Any) -> dict[str, Any]:
        return self.snapshots.submit_snapshot_request(*args, **kwargs)

    def confirm_request(self, *args: Any, **kwargs: Any) -> dict[str, Any]:
        return self.snapshots.confirm_snapshot_request(*args, **kwargs)

    def create_snapshot(self, batch_key: str, *args: Any, **kwargs: Any) -> dict[str, Any]:
        return self.snapshots.create_snapshot(batch_key, *args, **kwargs)

    def handoff(self, batch_key: str, *args: Any, **kwargs: Any) -> dict[str, Any]:
        return self.snapshots.handoff_partial_snapshot(batch_key, *args, **kwargs)

    # -- 对账 -----------------------------------------------------------
    def reconcile(self, batch_key: str | None = None, **kwargs: Any) -> dict[str, Any]:
        return self.snapshots.reconcile_history(batch_key, **kwargs)

    reconcile_history = reconcile

    def reconciliation(self, batch_key: str) -> dict[str, Any]:
        return self.snapshots.get_reconciliation(batch_key)

    get_reconciliation = reconciliation

    def __getattr__(self, name: str) -> Any:
        if name.startswith("_"):
            raise AttributeError(name)
        # 优先区域服务，再快照服务，复用它们各自的模糊分派。
        if hasattr(self.regions, name):
            return getattr(self.regions, name)
        if hasattr(self.snapshots, name):
            return getattr(self.snapshots, name)
        raise AttributeError(f"{type(self).__name__} 没有方法 {name!r}")


# 常见服务类别名。
RegionCoverageWorkflow = RegionCoverageService
CoverageWorkflowService = RegionCoverageService
