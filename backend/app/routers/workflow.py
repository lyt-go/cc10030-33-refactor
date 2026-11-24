"""区域覆盖 / 快照请求 / 历史对账的显式工作流接口。

通用 CRUD 仍走 ``resource`` 路由；这里只补充需要批量上下文的动作，
例如按批次发起历史对账、打开 / 提交 / 确认快照请求、确认子区成果。
所有业务判断都在领域服务内完成。
"""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException, Request

from app.services.geo_snapshot import GeoSnapshotService
from app.services.region_dim import RegionDimService

router = APIRouter(prefix="/api", tags=["区域覆盖与快照对账"])

region_service = RegionDimService()
snapshot_service = GeoSnapshotService()


async def _payload(request: Request) -> dict[str, Any]:
    try:
        data = await request.json()
    except Exception:
        return {}
    return data if isinstance(data, dict) else {}


def _require_batch(payload: dict[str, Any]) -> str:
    batch_key = payload.get("batch_key") or payload.get("batch")
    if not batch_key:
        raise HTTPException(status_code=422, detail="缺少 batch_key")
    return str(batch_key)


# ---------------------------------------------------------------------------
# 区域子区补报 / 确认 / 覆盖
# ---------------------------------------------------------------------------

@router.get("/region_dim/coverage/{batch_key}")
def region_coverage(batch_key: str) -> dict[str, Any]:
    return region_service.coverage_overview(batch_key)


@router.post("/region_dim/subregions/report")
async def report_subregion(request: Request) -> dict[str, Any]:
    payload = await _payload(request)
    batch_key = _require_batch(payload)
    try:
        view = region_service.report_subregion_aggregate(
            batch_key,
            payload.get("region"),
            region_id=payload.get("region_id"),
            region_code=payload.get("region_code"),
            region_name=payload.get("region_name"),
            total_points=payload.get("total_points", payload.get("point_count")),
            confirmed=bool(payload.get("confirmed", False)),
            separate_file=bool(payload.get("separate_file", False)),
        )
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    return {"ok": True, "coverage": view}


@router.post("/region_dim/subregions/confirm")
async def confirm_subregion(request: Request) -> dict[str, Any]:
    payload = await _payload(request)
    batch_key = _require_batch(payload)
    view = region_service.confirm_subregion(
        batch_key,
        payload.get("region"),
        region_id=payload.get("region_id"),
        region_code=payload.get("region_code"),
        region_name=payload.get("region_name"),
        confirmed_by=payload.get("confirmed_by"),
    )
    return {"ok": bool(view.get("ok", True)), "coverage": view}


# ---------------------------------------------------------------------------
# 快照请求
# ---------------------------------------------------------------------------

@router.post("/geo_snapshot/requests/open")
async def open_snapshot_request(request: Request) -> dict[str, Any]:
    payload = await _payload(request)
    batch_key = _require_batch(payload)
    req = snapshot_service.open_snapshot_request(
        batch_key,
        analysis_scope=payload.get("analysis_scope", "full"),
        grid_level=payload.get("grid_level", "L6"),
        time_range=payload.get("time_range", "24h"),
        regions=payload.get("regions"),
        note=payload.get("note", ""),
    )
    return {"ok": True, "request": req}


@router.post("/geo_snapshot/requests/submit")
async def submit_snapshot_request(request: Request) -> dict[str, Any]:
    payload = await _payload(request)
    result = snapshot_service.submit_snapshot_request(
        request_id=payload.get("request_id"),
        batch_key=payload.get("batch_key"),
        publish=bool(payload.get("publish", False)),
        confirm=payload.get("confirm"),
        regions=payload.get("regions"),
        require_complete=bool(payload.get("require_complete", False)),
    )
    return result


@router.post("/geo_snapshot/requests/confirm")
async def confirm_snapshot_request(request: Request) -> dict[str, Any]:
    payload = await _payload(request)
    return snapshot_service.confirm_snapshot_request(
        request_id=payload.get("request_id"),
        batch_key=payload.get("batch_key"),
    )


@router.get("/geo_snapshot/requests/{batch_key}/latest")
def latest_snapshot_request(batch_key: str) -> dict[str, Any]:
    req = snapshot_service.get_snapshot_request(batch_key=batch_key)
    if req is None:
        raise HTTPException(status_code=404, detail="快照请求不存在")
    return req


# ---------------------------------------------------------------------------
# 历史对账
# ---------------------------------------------------------------------------

@router.post("/geo_snapshot/reconcile")
async def reconcile(request: Request) -> dict[str, Any]:
    payload = await _payload(request)
    batch_key = _require_batch(payload)
    return snapshot_service.reconcile_history(
        batch_key,
        regions=payload.get("regions"),
        signed_regions=payload.get("signed_regions"),
        published_snapshot_id=payload.get("published_snapshot_id"),
    )


@router.get("/geo_snapshot/reconcile/{batch_key}")
def get_reconciliation(batch_key: str) -> dict[str, Any]:
    report = snapshot_service.get_reconciliation(batch_key)
    if not report:
        raise HTTPException(status_code=404, detail="对账报告尚未生成")
    return report
