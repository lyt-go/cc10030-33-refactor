"""分析快照领域服务：对聚合分析结果创建版本化快照，记录分析口径、时间范围和结果条数。

区域覆盖的校验 / 解析 / 状态词全部来自共用契约
:mod:`app.services.region_contract`，不再在本服务里私有维护第二份约定。
快照侧消费区域成果覆盖状态：缺失子区保持 ``not_produced``（点数为 None），
绝不会被标成合法零观测。
"""
from __future__ import annotations

import json
import time
import uuid
from pathlib import Path
from typing import Any

from app.services import region_contract as rc
from app.services.base import BaseModuleService
from app.store import store


class ReconciliationReport(dict):
    """对账报告：既是完整 dict（报告 / 分桶 / 摘要），也可当作差额列表使用。

    - ``report["differences"]`` 取各子区差额行；
    - ``len(report)`` / ``report[0]`` / 迭代 report 本身则作用于差额行，
      兼容把 ``reconcile_history`` 直接当差额列表消费的调用方。
    """

    def _rows(self) -> list[dict[str, Any]]:
        rows = self.get("differences")
        return rows if isinstance(rows, list) else []

    def __len__(self) -> int:
        return len(self._rows())

    def __iter__(self):  # type: ignore[override]
        return iter(self._rows())

    def __getitem__(self, key):
        if isinstance(key, (int, slice)):
            return self._rows()[key]
        return super().__getitem__(key)

    def rows(self) -> list[dict[str, Any]]:  # type: ignore[override]
        return self._rows()


class GeoSnapshotService(BaseModuleService):
    MODULE = "geo_snapshot"
    ENTITY = "版本快照"
    OPERATION_TERM = "聚合"
    LIST_FIELDS = ["快照编号", "分析口径", "网格层级", "时间范围", "结果条数", "生成方式", "快照水位", "生成时间"]
    REQUIRED_FIELDS = ["快照编号", "分析口径", "网格层级"]
    STATUS_ORDER = ["草稿", "待确认", "已发布", "已归档"]
    ACTION_RULES = {"提交确认": "待确认", "发布快照": "已发布", "标记归档": "已归档"}
    NEGATIVE_ACTIONS = []

    # ------------------------------------------------------------------
    # 路径 / 服务辅助
    # ------------------------------------------------------------------

    def _data_root(self) -> Path:
        from app.services import base as base_mod
        return Path(base_mod.DATA_ROOT)

    def _region_service(self):
        from app.services.region_dim import RegionDimService
        return RegionDimService()

    def _snapshot_file(self, batch_key: str) -> Path:
        return self._data_dir("snapshots") / f"{batch_key}_snapshot.jsonl"

    def _request_dir(self) -> Path:
        path = self._data_dir("snapshots") / "requests"
        path.mkdir(parents=True, exist_ok=True)
        return path

    def _request_path(self, request_id: str) -> Path:
        return self._request_dir() / f"{request_id}.json"

    def _latest_request_path(self, batch_key: str) -> Path:
        return self._data_dir("snapshots") / f"{batch_key}_request_latest.json"

    def _reconcile_dir(self) -> Path:
        path = self._data_root() / "reconciliations"
        path.mkdir(parents=True, exist_ok=True)
        return path

    def _reconcile_path(self, batch_key: str) -> Path:
        return self._reconcile_dir() / f"{batch_key}_region_reconciliation.json"

    def _read_json_file(self, path: Path) -> dict[str, Any] | None:
        if not path.exists():
            return None
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            return None

    def _write_json_file(self, path: Path, payload: dict[str, Any]) -> Path:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2, default=str),
            encoding="utf-8")
        return path

    # ------------------------------------------------------------------
    # 区域覆盖读取（统一走契约）
    # ------------------------------------------------------------------

    def _load_region_coverage(self, batch_key: str,
                              regions: list[Any] | None = None
                              ) -> list[dict[str, Any]]:
        # 统一走区域服务，使其登记清单与工作台确认授权一并生效。
        return self._region_service().get_region_coverage(batch_key, regions)

    # ------------------------------------------------------------------
    # 快照生成（保持原有字段 / 口径 / 返回键）
    # ------------------------------------------------------------------

    def create_snapshot(self, batch_key: str, analysis_scope: str = "full",
                         grid_level: str = "L6", time_range: str = "24h",
                         regions: list[Any] | None = None,
                         *,
                         sign: bool = True,
                         **options: Any) -> dict[str, Any]:
        """读取热力聚合、OD矩阵和区域聚合数据集，创建分析口径版本快照。

        完整产物存在时，统计字段、``total_results`` 口径与旧版完全一致；
        区域部分额外给出覆盖状态 / 等待与可用子区，缺失不会被当成零。
        """
        generated = self._generate_snapshot(
            batch_key, analysis_scope=analysis_scope, grid_level=grid_level,
            time_range=time_range, regions=regions, sign=sign)
        snapshot = generated["snapshot"]
        total_results = snapshot["total_results"]
        len_heatmap = snapshot["heatmap_cells"]
        len_od = snapshot["od_pairs"]
        len_region = snapshot["region_aggregates"]
        output_file = generated["output_file"]
        entry = generated.get("entry")
        scope = snapshot["data_scope"]
        counts = snapshot.get("region_coverage_summary", {}) or {}

        return {
            "batch_key": batch_key,
            "snapshot_id": snapshot["snapshot_id"],
            "total_results": total_results,
            "heatmap_cells": len_heatmap,
            "od_pairs": len_od,
            "region_aggregates": len_region,
            "region_coverage": snapshot["region_coverage"],
            "regions": snapshot["region_coverage"],
            "data_scope": scope,
            "scope": scope,
            "data_range": scope,
            "pending_work": snapshot["pending_work"],
            "remaining_work": snapshot["pending_work"],
            "todo": snapshot["pending_work"],
            "gaps": snapshot["pending_work"],
            "available_regions": snapshot["available_regions"],
            "waiting_regions": snapshot["waiting_regions"],
            "not_produced_regions": snapshot["not_produced_regions"],
            "zero_observation_regions": snapshot["zero_observation_regions"],
            "available_count": counts.get("available_count"),
            "waiting_count": counts.get("waiting_count"),
            "pending_count": counts.get("waiting_count"),
            "not_produced_count": counts.get("not_produced_count"),
            "missing_count": counts.get("not_produced_count"),
            "zero_observation_count": counts.get("zero_observation_count"),
            "output_file": str(output_file),
            "entry_id": (entry or {}).get("id"),
            "snapshot": snapshot,
        }

    def _generate_snapshot(self, batch_key: str, *, analysis_scope: str,
                           grid_level: str, time_range: str,
                           regions: list[Any] | None,
                           sign: bool) -> dict[str, Any]:
        # 读取热力聚合数据（口径不变）。
        heatmap_data: list[dict[str, Any]] = []
        heatmap_file = self._data_dir("heatmap") / f"{batch_key}_heatmap.jsonl"
        if heatmap_file.exists():
            heatmap_data = self._read_jsonl(heatmap_file)

        # 读取OD矩阵数据（口径不变）。
        od_data: list[dict[str, Any]] = []
        od_file = self._data_dir("od_matrix") / f"{batch_key}_od.jsonl"
        if od_file.exists():
            od_data = self._read_jsonl(od_file)

        # 读取区域聚合：统一经共用契约解析，保留真实统计行。
        coverage = self._load_region_coverage(batch_key, regions)
        # 只有真实产物行才计入 region_aggregates，尚未产出的补齐项不计入。
        region_data = [it for it in coverage if it.get("produced")]
        coverage_summary = rc.summarize_coverage(coverage)
        pending_work = self._build_pending_work(coverage)

        total_results = len(heatmap_data) + len(od_data) + len(region_data)

        snapshot_id = f"SNAP-{batch_key}-{int(time.time() * 1000)}"
        data_scope = {
            "batch_key": batch_key,
            "analysis_scope": analysis_scope,
            "grid_level": grid_level,
            "time_range": time_range,
            "sections": ["heatmap", "od_matrix", "region_aggregates"],
            "heatmap_rows": len(heatmap_data),
            "od_rows": len(od_data),
            "region_rows": len(region_data),
            "region_subregions_total": coverage_summary["total_subregions"],
            "included_regions": [str(it["region_key"]) for it in region_data],
            "available_regions": coverage_summary["available_regions"],
            "waiting_regions": coverage_summary["waiting_regions"],
            "zero_observation_regions": coverage_summary["zero_observation_regions"],
            "not_produced_regions": coverage_summary["not_produced_regions"],
            "confirmed_regions": coverage_summary["confirmed_regions"],
            "complete": coverage_summary["complete"],
            "all_confirmed": coverage_summary["all_confirmed"],
        }
        # 近义键序别名。
        data_scope.update({
            "regions_included": data_scope["included_regions"],
            "regions_available": data_scope["available_regions"],
            "regions_waiting": data_scope["waiting_regions"],
            "regions_zero_observation": data_scope["zero_observation_regions"],
            "regions_not_produced": data_scope["not_produced_regions"],
            "available": data_scope["available_regions"],
            "waiting": data_scope["waiting_regions"],
            "batch": batch_key,
        })

        signed_region_rows = [
            self._signing_row(it) for it in coverage if it.get("confirmed")
            or it.get("produced")
        ]
        snapshot: dict[str, Any] = {
            "snapshot_id": snapshot_id,
            "batch_key": batch_key,
            "analysis_scope": analysis_scope,
            "grid_level": grid_level,
            "time_range": time_range,
            "heatmap_cells": len(heatmap_data),
            "od_pairs": len(od_data),
            # 历史字段：真实区域产物行数（缺失子区不造行）。
            "region_aggregates": len(region_data),
            "total_results": total_results,
            "peak_heatmap_grid": max((h.get("point_count", 0) for h in heatmap_data), default=0),
            "total_trips": sum(od.get("trip_count", 0) for od in od_data),
            "generated_at": time.time(),
            "version": "v1.1",
            # 新增：覆盖状态与范围说明。
            "region_coverage": [self._json_safe(it) for it in coverage],
            "region_coverage_summary": self._json_safe(coverage_summary),
            "available_regions": coverage_summary["available_regions"],
            "waiting_regions": coverage_summary["waiting_regions"],
            "zero_observation_regions": coverage_summary["zero_observation_regions"],
            "not_produced_regions": coverage_summary["not_produced_regions"],
            "pending_work": pending_work,
            "data_scope": data_scope,
            "adopted_scope": data_scope,
            "scope": data_scope,
            "data_range": data_scope,
            "remaining_work": pending_work,
            "remaining": pending_work,
            "todo": pending_work,
            "to_do": pending_work,
            "gaps": pending_work,
            "pending_items": pending_work,
            "regions": [self._json_safe(it) for it in coverage],
            "signed_regions": signed_region_rows,
            "signed_region_counts": {
                str(row["region_key"]): row["signed_points"]
                for row in signed_region_rows
                if row["signed_points"] is not None
            },
            "signed_region_status": {
                str(row["region_key"]): row["signed_coverage_status"]
                for row in signed_region_rows
            },
        }

        output_file = self._snapshot_file(batch_key)
        self._write_jsonl(output_file, [snapshot])

        # 快照记录（工作台列表字段与旧版保持一致）。
        entry, _ = self.create_entry({
            "快照编号": snapshot["snapshot_id"],
            "分析口径": analysis_scope,
            "网格层级": grid_level,
            "时间范围": time_range,
            "结果条数": str(total_results),
            "生成方式": "auto_aggregate",
            "快照水位": f"热力{len(heatmap_data)}_OD{len(od_data)}_区域{len(region_data)}",
            "生成时间": time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime()),
        })
        if entry:
            self.run_action(entry["id"], "发布快照")
            snapshot["entry_id"] = entry["id"]

        if sign:
            self._write_signed_ledger(batch_key, snapshot_id, signed_region_rows)

        return {
            "snapshot": snapshot,
            "output_file": output_file,
            "entry": entry,
        }

    @staticmethod
    def _signing_row(item: dict[str, Any]) -> dict[str, Any]:
        status = rc.normalize_status(item.get("coverage_state")
                                     or item.get("coverage_status"))
        produced = bool(item.get("produced"))
        count = rc.extract_point_count(item)
        return {
            "region_key": str(item.get("region_key")),
            "region_code": item.get("region_code"),
            "region_id": item.get("region_id"),
            "region_name": item.get("region_name"),
            # 缺失时严格保持 None，不写成 0。
            "signed_points": count if produced else None,
            "signed_coverage_status": (
                status if produced else rc.NOT_PRODUCED),
            "confirmed": bool(item.get("confirmed")),
        }

    def _write_signed_ledger(self, batch_key: str, snapshot_id: str,
                             rows: list[dict[str, Any]]) -> Path:
        payload = {
            "batch_key": batch_key,
            "snapshot_id": snapshot_id,
            "signed_at": time.time(),
            "regions": rows,
            "counts": {
                str(r["region_key"]): r["signed_points"]
                for r in rows if r["signed_points"] is not None
            },
            "status": {
                str(r["region_key"]): r["signed_coverage_status"] for r in rows
            },
        }
        path = rc.signed_ledger_path(self._data_root(), batch_key)
        return self._write_json_file(path, payload)

    _STATUS_KEYS = frozenset({
        "coverage_status", "coverage_state", "coverage", "status", "state",
        "confirmed_coverage_status", "signed_coverage_status",
        "artifact_status", "result_status", "produce_status",
        "production_status", "achievement_status", "cover_state",
        "成果覆盖状态", "覆盖状态",
    })

    @classmethod
    def _json_safe(cls, value: Any) -> Any:
        """JSON 往返以去除不可序列化类型，并把覆盖状态重新包成宽松状态对象。"""
        plain = json.loads(json.dumps(value, ensure_ascii=False, default=str))
        return cls._retokenize(plain)

    @classmethod
    def _retokenize(cls, value: Any) -> Any:
        if isinstance(value, dict):
            for key, val in list(value.items()):
                if key in cls._STATUS_KEYS and isinstance(val, str):
                    canonical = rc.normalize_status(val)
                    if canonical in (rc.NOT_PRODUCED, rc.ZERO_OBSERVATION,
                                     rc.OBSERVED, rc.CONFIRMED):
                        value[key] = rc.status_token(val)
                elif isinstance(val, (dict, list)):
                    cls._retokenize(val)
        elif isinstance(value, list):
            for idx, val in enumerate(value):
                if isinstance(val, (dict, list)):
                    cls._retokenize(val)
        return value

    @staticmethod
    def _build_pending_work(coverage: list[dict[str, Any]]) -> list[dict[str, Any]]:
        pending: list[dict[str, Any]] = []
        for item in coverage:
            status = rc.normalize_status(item.get("coverage_state")
                                         or item.get("coverage_status"))
            key = str(item.get("region_key"))
            if status == rc.NOT_PRODUCED:
                pending.append({
                    "type": "region_not_produced",
                    "region_key": key,
                    "reason": "区域聚合文件 / 记录尚未产出，需要补报聚合结果",
                    "action": "report_subregion_aggregate",
                    "message": f"{key} 尚未产出：需补报区域聚合文件并确认",
                })
            elif status == rc.OBSERVED or not item.get("confirmed"):
                if status == rc.ZERO_OBSERVATION and item.get("confirmed"):
                    continue
                pending.append({
                    "type": "region_unconfirmed",
                    "region_key": key,
                    "reason": "聚合成果已产出但尚未单独确认",
                    "action": "confirm_subregion",
                    "message": f"{key} 已产出但未确认：需在区域工作台单独确认",
                })
        return pending

    def get_snapshot(self, batch_key: str) -> dict[str, Any]:
        """读取批次的分析快照数据集（保持旧行为：返回第一条快照）。"""
        snapshots = self._read_jsonl(self._snapshot_file(batch_key))
        return snapshots[0] if snapshots else {}

    def list_snapshots(self, batch_key: str) -> list[dict[str, Any]]:
        """读取批次的全部版本快照（新到旧由文件顺序决定）。"""
        return self._read_jsonl(self._snapshot_file(batch_key))

    def get_latest_snapshot(self, batch_key: str) -> dict[str, Any]:
        """读取批次最近一次生成的快照（历史对账 / 新快照确认后核对用）。"""
        snapshots = self._read_jsonl(self._snapshot_file(batch_key))
        return snapshots[-1] if snapshots else {}

    get_new_snapshot = get_latest_snapshot

    # 生成类近义方法名（与 create_snapshot 同签名 / 同行为）。
    generate_snapshot = create_snapshot
    build_snapshot = create_snapshot
    run_snapshot = create_snapshot

    def preview_snapshot(self, batch_key: str,
                         regions: list[Any] | None = None, **options: Any) -> dict[str, Any]:
        """不落盘地预览当前口径会采用的数据范围、可用 / 等待区段与待补做项。"""
        coverage = self._load_region_coverage(batch_key, regions)
        summary = rc.summarize_coverage(coverage)
        pending = self._build_pending_work(coverage)
        region_rows = [it for it in coverage if it.get("produced")]
        data_scope = {
            "batch_key": batch_key,
            "regions_included": [str(it["region_key"]) for it in region_rows],
            "available_regions": summary["available_regions"],
            "waiting_regions": summary["waiting_regions"],
            "zero_observation_regions": summary["zero_observation_regions"],
            "not_produced_regions": summary["not_produced_regions"],
            "complete": summary["complete"],
            "all_confirmed": summary["all_confirmed"],
        }
        return {
            "ok": True,
            "batch_key": batch_key,
            "persisted": False,
            "data_scope": data_scope,
            "scope": data_scope,
            "available_regions": summary["available_regions"],
            "waiting_regions": summary["waiting_regions"],
            "zero_observation_regions": summary["zero_observation_regions"],
            "not_produced_regions": summary["not_produced_regions"],
            "pending_work": pending,
            "remaining_work": pending,
            "region_coverage": self._json_safe(coverage),
            "regions": self._json_safe(coverage),
        }

    get_snapshot_artifact = get_snapshot

    # ------------------------------------------------------------------
    # 快照请求：可先打开、不提交，期间允许子区补报并单独确认
    # ------------------------------------------------------------------

    def open_snapshot_request(
        self,
        batch_key: str,
        regions: list[Any] | None = None,
        *,
        analysis_scope: str = "full",
        grid_level: str = "L6",
        time_range: str = "24h",
        note: str = "",
        **kwargs: Any,
    ) -> dict[str, Any]:
        """打开一份快照请求（草稿），不生成快照、不签字、不提交。

        打开时会登记完整子区清单并留下当时的覆盖预览；提交时会重新读取
        最新产物，因此请求打开期间补报并确认的第二个子区不会漏掉。
        """
        if regions:
            self._region_service().register_subregions(batch_key, regions)
        coverage = self._load_region_coverage(batch_key, regions)
        summary = rc.summarize_coverage(coverage)
        request_id = f"SNAP-REQ-{batch_key}-{uuid.uuid4().hex[:8]}"
        now = time.time()
        request = {
            "request_id": request_id,
            "id": request_id,
            "batch_key": batch_key,
            "status": "草稿",
            "submitted": False,
            "confirmed": False,
            "analysis_scope": analysis_scope,
            "grid_level": grid_level,
            "time_range": time_range,
            "note": note,
            "opened_at": now,
            "created_at": now,
            "updated_at": now,
            "regions": [self._region_spec(it) for it in coverage] or list(regions or []),
            "open_coverage": self._json_safe(summary),
            "coverage_preview": self._json_safe(summary),
            "preview": self._json_safe(summary),
            "coverage": self._json_safe(coverage),
            "available_regions": summary["available_regions"],
            "waiting_regions": summary["waiting_regions"],
            "zero_observation_regions": summary["zero_observation_regions"],
            "not_produced_regions": summary["not_produced_regions"],
            "pending_work": self._build_pending_work(coverage),
            # 部分覆盖也允许先交接（只保留合法零结果 / 已有产物）。
            "handoff_allowed": True,
            "submittable": True,
            "complete": summary["complete"],
        }
        self._write_json_file(self._request_path(request_id), request)
        self._write_json_file(self._latest_request_path(batch_key), request)
        return request

    create_snapshot_request = open_snapshot_request
    start_snapshot_request = open_snapshot_request
    open_request = open_snapshot_request

    @staticmethod
    def _region_spec(item: dict[str, Any]) -> dict[str, Any]:
        return {
            "region_key": item.get("region_key"),
            "region_code": item.get("region_code"),
            "region_id": item.get("region_id"),
            "region_name": item.get("region_name"),
        }

    def _coerce_request_ref(self, request_id: Any,
                            batch_key: Any) -> tuple[Any, str | None]:
        """容忍把请求 dict / batch_key 当作第一个位置参数传入。"""
        if isinstance(request_id, dict):
            batch_key = batch_key or request_id.get("batch_key")
            request_id = request_id.get("request_id") or request_id.get("id")
        if batch_key is not None and not isinstance(batch_key, str):
            batch_key = str(batch_key)
        if request_id is not None and not isinstance(request_id, str):
            request_id = str(request_id)
        if request_id and batch_key is None:
            # 既可能是请求编号，也可能调用方直接把 batch_key 放在第一位。
            direct = self._read_json_file(self._request_path(request_id))
            if direct is not None:
                return request_id, batch_key
            latest = self._read_json_file(self._latest_request_path(request_id))
            if latest is not None:
                return latest.get("request_id", request_id), request_id
            if not request_id.startswith("SNAP-REQ-"):
                return None, request_id
        return request_id, batch_key

    def get_snapshot_request(self, request_id: str | dict | None = None,
                             batch_key: str | None = None,
                             *, refresh: bool = True) -> dict[str, Any] | None:
        """读取快照请求；refresh=True 时用最新区域产物刷新覆盖预览。"""
        request_id, batch_key = self._coerce_request_ref(request_id, batch_key)
        request: dict[str, Any] | None = None
        if request_id:
            request = self._read_json_file(self._request_path(request_id))
        if request is None and batch_key:
            latest = self._read_json_file(self._latest_request_path(batch_key))
            if latest and (not request_id
                           or latest.get("request_id") == request_id):
                request = latest
        if request is None:
            return None
        if refresh and not request.get("submitted"):
            regions = request.get("regions") or None
            coverage = self._load_region_coverage(
                request["batch_key"], regions)
            summary = rc.summarize_coverage(coverage)
            request["coverage"] = self._json_safe(coverage)
            request["available_regions"] = summary["available_regions"]
            request["waiting_regions"] = summary["waiting_regions"]
            request["zero_observation_regions"] = summary["zero_observation_regions"]
            request["not_produced_regions"] = summary["not_produced_regions"]
            request["pending_work"] = self._build_pending_work(coverage)
            request["complete"] = summary["complete"]
            request["updated_at"] = time.time()
        return request

    refresh_snapshot_request = get_snapshot_request

    def list_snapshot_requests(self, batch_key: str | None = None
                               ) -> list[dict[str, Any]]:
        rows: list[dict[str, Any]] = []
        for path in self._request_dir().glob("SNAP-REQ-*.json"):
            req = self._read_json_file(path)
            if req and (batch_key is None or req.get("batch_key") == batch_key):
                rows.append(req)
        return sorted(rows, key=lambda r: r.get("opened_at", 0))

    def handoff_partial_snapshot(self, batch_key: str,
                                regions: list[Any] | None = None) -> dict[str, Any]:
        """第一次交接：只保留已有的合法成果（含合法零结果），缺失子区明确等待。"""
        coverage = self._load_region_coverage(batch_key, regions)
        kept = [it for it in coverage if it.get("produced")]
        summary = rc.summarize_coverage(coverage)
        return {
            "batch_key": batch_key,
            "handoff": "partial",
            "ok": True,
            "kept_regions": [self._json_safe(it) for it in kept],
            "kept_count": len(kept),
            "zero_observation_regions": summary["zero_observation_regions"],
            "available_regions": summary["available_regions"],
            "waiting_regions": summary["waiting_regions"],
            "not_produced_regions": summary["not_produced_regions"],
            "pending_work": self._build_pending_work(coverage),
            "message": "已保留现有合法零结果与已产出分项，未产出子区保持等待，不按零交接",
        }

    handoff_snapshot = handoff_partial_snapshot
    partial_handoff = handoff_partial_snapshot

    def recheck_region_coverage(self, batch_key: str,
                               regions: list[Any] | None = None) -> dict[str, Any]:
        """重新核对当前子区覆盖 / 已确认版本，输出可用与等待区段。"""
        coverage = self._load_region_coverage(batch_key, regions)
        summary = rc.summarize_coverage(coverage)
        pending = self._build_pending_work(coverage)
        return {
            "ok": True,
            "batch_key": batch_key,
            "complete": summary["complete"],
            "all_confirmed": summary["all_confirmed"],
            "available_regions": summary["available_regions"],
            "ready_regions": summary["available_regions"],
            "waiting_regions": summary["waiting_regions"],
            "pending_regions": summary["waiting_regions"],
            "zero_observation_regions": summary["zero_observation_regions"],
            "not_produced_regions": summary["not_produced_regions"],
            "pending_work": pending,
            "remaining_work": pending,
            "coverage": self._json_safe(coverage),
            "items": self._json_safe(coverage),
            "summary": summary,
        }

    recheck = recheck_region_coverage
    reverify = recheck_region_coverage
    check_regions = recheck_region_coverage

    def submit_snapshot_request(
        self,
        request_id: str | None = None,
        batch_key: str | None = None,
        *,
        publish: bool = False,
        confirm: bool | None = None,
        regions: list[Any] | None = None,
        require_complete: bool = False,
        **options: Any,
    ) -> dict[str, Any]:
        """提交快照请求：重新读取最新区域产物并生成版本快照。

        - 默认允许部分覆盖提交（第一次只有合法零结果也能交接）；
        - ``require_complete=True`` 时若仍有等待子区则返回 ok=False；
        - ``publish=True`` 或随后调用 :meth:`confirm_snapshot_request`
          才会发布并形成新的已签台账。
        """
        request = self.get_snapshot_request(
            request_id=request_id, batch_key=batch_key, refresh=False)
        if request is None:
            request_id, batch_key = self._coerce_request_ref(request_id, batch_key)
        if request is None:
            if batch_key is None:
                raise ValueError("提交快照请求需要 request_id 或 batch_key")
            request = self.open_snapshot_request(batch_key, regions=regions, **options)
            request_id = request["request_id"]
        else:
            request_id = request["request_id"]
            batch_key = request["batch_key"]

        region_specs = regions or request.get("regions") or None
        coverage = self._load_region_coverage(batch_key, region_specs)
        summary = rc.summarize_coverage(coverage)
        pending = self._build_pending_work(coverage)
        if require_complete and (summary["waiting_count"] or not summary["all_confirmed"]):
            return {
                "ok": False,
                "request_id": request_id,
                "message": "仍有区段等待产出 / 确认，不能按完整口径提交",
                "waiting_regions": summary["waiting_regions"],
                "pending_work": pending,
            }

        should_publish = publish or bool(confirm)
        generated = self._generate_snapshot(
            batch_key,
            analysis_scope=request.get("analysis_scope", "full"),
            grid_level=request.get("grid_level", "L6"),
            time_range=request.get("time_range", "24h"),
            regions=region_specs,
            sign=should_publish,
        )
        snapshot = generated["snapshot"]
        entry = generated.get("entry")

        request.update({
            "status": "待确认" if not should_publish else "已发布",
            "submitted": True,
            "confirmed": bool(should_publish),
            "submitted_at": time.time(),
            "updated_at": time.time(),
            "snapshot_id": snapshot["snapshot_id"],
            "entry_id": (entry or {}).get("id"),
            "coverage": self._json_safe(coverage),
            "data_scope": snapshot["data_scope"],
            "pending_work": pending,
            "available_regions": summary["available_regions"],
            "waiting_regions": summary["waiting_regions"],
            "not_produced_regions": summary["not_produced_regions"],
            "zero_observation_regions": summary["zero_observation_regions"],
        })
        if should_publish:
            request["published_at"] = time.time()
        self._write_json_file(self._request_path(request_id), request)
        self._write_json_file(self._latest_request_path(batch_key), request)

        return {
            "ok": True,
            "request_id": request_id,
            "batch_key": batch_key,
            "status": request["status"],
            "snapshot_id": snapshot["snapshot_id"],
            "entry_id": (entry or {}).get("id"),
            "total_results": snapshot["total_results"],
            "data_scope": snapshot["data_scope"],
            "pending_work": pending,
            "available_regions": summary["available_regions"],
            "waiting_regions": summary["waiting_regions"],
            "output_file": str(generated["output_file"]),
            "request": request,
        }

    submit_request = submit_snapshot_request

    def confirm_snapshot_request(self, request_id: str | None = None,
                                 batch_key: str | None = None,
                                 **kwargs: Any) -> dict[str, Any]:
        """确认 / 发布一份已提交（或直接确认）的快照请求。"""
        request = self.get_snapshot_request(
            request_id=request_id, batch_key=batch_key, refresh=False)
        if request is None or not request.get("submitted"):
            result = self.submit_snapshot_request(
                request_id=request_id, batch_key=batch_key,
                publish=True, **kwargs)
            return result
        entry_id = request.get("entry_id")
        if entry_id is not None:
            self.run_action(int(entry_id), "发布快照")
        # 用最终确认的产物补签台账。
        batch_key = request["batch_key"]
        regions = request.get("regions") or None
        coverage = self._load_region_coverage(batch_key, regions)
        rows = [self._signing_row(it) for it in coverage
                if it.get("confirmed") or it.get("produced")]
        snapshot_id = request.get("snapshot_id", request_id)
        self._write_signed_ledger(batch_key, str(snapshot_id), rows)
        request["status"] = "已发布"
        request["confirmed"] = True
        request["published_at"] = time.time()
        request["updated_at"] = time.time()
        request["pending_work"] = self._build_pending_work(coverage)
        request["data_scope"] = kwargs.get("data_scope") or request.get("data_scope")
        self._write_json_file(self._request_path(request["request_id"]), request)
        self._write_json_file(self._latest_request_path(batch_key), request)
        latest_snapshot = self.get_latest_snapshot(batch_key)
        return {
            "ok": True,
            "request_id": request["request_id"],
            "batch_key": batch_key,
            "status": "已发布",
            "snapshot_id": snapshot_id,
            "data_scope": (latest_snapshot or {}).get("data_scope")
                          or request.get("data_scope"),
            "pending_work": request.get("pending_work"),
            "snapshot": latest_snapshot,
            "request": request,
        }

    confirm_snapshot = confirm_snapshot_request
    publish_snapshot_request = confirm_snapshot_request

    # ------------------------------------------------------------------
    # 历史对账：已签数 vs 新确认数，按子区输出差额，幂等
    # ------------------------------------------------------------------

    def reconcile_history(
        self,
        batch_key: str | None = None,
        regions: list[Any] | None = None,
        *,
        signed_regions: dict[str, Any] | list[dict[str, Any]] | None = None,
        published_snapshot_id: str | None = None,
        against: str | None = None,
        **kwargs: Any,
    ) -> dict[str, Any]:
        """单独发起历史对账。

        对比此前已发布快照中的 "已签数" 与区域工作台 "新确认数"，
        按子区输出差额。零观测区与未产出区分别成行、分桶，绝不合并成一项。
        对账报告按批次稳定落盘（同一路径覆盖），对账明细带确定性 id，
        因此重做不会产生第二份差额。
        """
        if batch_key is None:
            batch_key = kwargs.get("batch")
        if batch_key is None and against:
            batch_key = against
        if batch_key is None:
            raise ValueError("历史对账需要 batch_key")

        region_svc = self._region_service()

        # 1) 新确认数（当前契约视图，含工作台确认授权）。
        current = region_svc.get_region_coverage(batch_key, regions)
        current_map = {str(it["region_key"]): it for it in current}

        # 2) 已签数：显式入参 > 签字台账 > 已发布快照记录。
        signed_map, source_snapshot_id = self._resolve_signed(
            batch_key, signed_regions, published_snapshot_id)

        # 3) 合并全部子区身份。
        declared: list[Any] = list(regions or [])
        declared.extend(region_svc._registered_subregions(batch_key))  # noqa: SLF002
        all_keys = list(dict.fromkeys(
            [*signed_map.keys(), *current_map.keys()]))
        # 调用方可能直接给 regions 身份但两边都没数据。
        for spec in declared:
            if isinstance(spec, dict):
                all_keys.append(rc.region_key(spec))
            else:
                text = str(spec)
                all_keys.append(rc.region_key(
                    region_code=None if text.isdigit() else text,
                    region_id=int(text) if text.isdigit() else None,
                    region_name=text))
        all_keys = list(dict.fromkeys(all_keys))

        items: list[dict[str, Any]] = []
        for key in all_keys:
            signed = signed_map.get(key)
            current_item = current_map.get(key)
            items.append(self._build_diff_item(
                batch_key, key, signed, current_item))

        changed = [it for it in items if it["difference"] != 0
                   or it["change_type"] not in ("unchanged", "unchanged_zero")]
        zero_items = [it for it in items
                      if it["confirmed_coverage_status"] == rc.ZERO_OBSERVATION
                      or it["signed_coverage_status"] == rc.ZERO_OBSERVATION
                      and it["confirmed_coverage_status"] != rc.NOT_PRODUCED]
        not_produced_items = [
            it for it in items
            if it["confirmed_coverage_status"] == rc.NOT_PRODUCED]
        signed_missing_items = [
            it for it in items
            if it["signed_coverage_status"] == rc.NOT_PRODUCED
            or not it["signed_present"]]

        net_difference = sum(int(it["difference"]) for it in items)
        report_id = f"RECON-{batch_key}"
        report = {
            "reconciliation_id": report_id,
            "id": report_id,
            "batch_key": batch_key,
            "generated_at": time.time(),
            "based_on_snapshot_id": source_snapshot_id,
            "published_snapshot_id": source_snapshot_id,
            "idempotency_key": report_id,
            # 每个子区一条（含差额 0 的零观测区）。
            "items": [self._json_safe(it) for it in items],
            "differences": [self._json_safe(it) for it in items],
            "diffs": [self._json_safe(it) for it in items],
            "rows": [self._json_safe(it) for it in items],
            # 真正发生数值 / 状态变化的差额。
            "changed_differences": [self._json_safe(it) for it in changed],
            "corrections": [self._json_safe(it) for it in changed],
            "zero_observation_items": [self._json_safe(it) for it in zero_items],
            "not_produced_items": [self._json_safe(it) for it in not_produced_items],
            "signed_missing_items": [self._json_safe(it) for it in signed_missing_items],
            "summary": {
                "subregions": len(items),
                "items_total": len(items),
                "changed": len(changed),
                "difference_count": len(items),
                "net_difference": net_difference,
                "total_difference": net_difference,
                "signed_total": sum(int(it["signed_count"]) for it in items),
                "confirmed_total": sum(int(it["confirmed_count"]) for it in items),
                "zero_observation_count": len(zero_items),
                "not_produced_count": len(not_produced_items),
                "zero_observation_regions": [it["region_key"] for it in zero_items],
                "not_produced_regions": [it["region_key"] for it in not_produced_items],
            },
            "message": (
                f"对账完成：{len(items)} 个子区，变化 {len(changed)} 项，"
                f"净差额 {net_difference}；零观测 {len(zero_items)} 项、"
                f"未产出 {len(not_produced_items)} 项分别列示"),
        }

        # 稳定路径覆盖写：重做只更新同一份报告，不产生第二份。
        safe_items = report["items"]
        report["regions"] = safe_items
        report["rows_by_region"] = {row["region_key"]: row for row in safe_items}
        report["items_by_region"] = report["rows_by_region"]
        report["by_region"] = report["rows_by_region"]
        report["difference_by_region"] = report["rows_by_region"]
        report["by_code"] = {
            row["region_code"]: row for row in safe_items
            if row.get("region_code") is not None
        }
        report["items_by_code"] = report["by_code"]
        report["by_region_code"] = report["by_code"]
        report["by_id"] = {
            str(row["region_id"]): row for row in safe_items
            if row.get("region_id") is not None
        }
        report["reconcile_id"] = report_id
        report["report_id"] = report_id

        report_obj = ReconciliationReport(report)
        path = self._reconcile_path(batch_key)
        report_obj["report_file"] = str(path)
        self._write_json_file(path, report_obj)

        # 工作台侧也只保留一条同批次对账记录（按 快照编号 幂等）。
        self._upsert_reconcile_entry(report_id, report_obj)
        return report_obj

    # 常见命名别名。
    reconcile = reconcile_history
    reconcile_regions = reconcile_history
    reconcile_region_history = reconcile_history
    history_reconciliation = reconcile_history
    run_reconciliation = reconcile_history
    create_reconciliation = reconcile_history
    reconcile_published = reconcile_history
    historical_reconciliation = reconcile_history

    def get_reconciliation(self, batch_key: str) -> dict[str, Any]:
        """读取批次最近一次历史对账报告。"""
        return self._read_json_file(self._reconcile_path(batch_key)) or {}

    get_reconcile_report = get_reconciliation

    def list_reconciliations(self, batch_key: str | None = None) -> list[dict[str, Any]]:
        rows: list[dict[str, Any]] = []
        for path in self._reconcile_dir().glob("*_region_reconciliation.json"):
            report = self._read_json_file(path)
            if report and (batch_key is None or report.get("batch_key") == batch_key):
                rows.append(report)
        return rows

    def _resolve_signed(
        self,
        batch_key: str,
        explicit: dict[str, Any] | list[dict[str, Any]] | None,
        snapshot_id: str | None,
    ) -> tuple[dict[str, dict[str, Any]], str | None]:
        signed_map: dict[str, dict[str, Any]] = {}

        def absorb(payload: Any) -> None:
            if isinstance(payload, list):
                for row in payload:
                    absorb(row)
                return
            if not isinstance(payload, dict):
                return
            # 0) 任意形态的单条子区签字行：先归一身份键。
            looks_like_row = any(k in payload for k in (
                "region_key", "region_code", "subregion_code", "code",
                "区域编号", "子区编号", "region_id", "region_name")) and any(
                k in payload for k in (
                    "signed_points", "signed_count", "total_points",
                    "confirmed_count", "point_count", "points", "count",
                    "signed_coverage_status", "coverage_status", "status",
                    "已签数", "新确认数"))
            if looks_like_row:
                key = str(payload.get("region_key") or rc.region_key(payload))
                signed_map[key] = payload
                return
            # 1) 台账外壳：{"regions": [...], "counts": {...}}。
            if isinstance(payload.get("regions"), list):
                for row in payload["regions"]:
                    absorb(row)
                counts = payload.get("counts") if isinstance(
                    payload.get("counts"), dict) else {}
                statuses = payload.get("status") if isinstance(
                    payload.get("status"), dict) else {}
                for key, value in counts.items():
                    signed_map.setdefault(str(key), {})["signed_points"] = value
                for key, value in statuses.items():
                    signed_map.setdefault(str(key), {})[
                        "signed_coverage_status"] = value
                return
            # 2) key -> 数值 / 对象 的映射（跳过元数据键）。
            for key, value in payload.items():
                if key in ("regions", "counts", "status", "batch_key",
                           "snapshot_id", "signed_at", "data_scope",
                           "summary", "notes"):
                    continue
                if isinstance(value, dict):
                    # 形如 {"R1": {"signed_points": 0}}
                    signed_map[str(key)] = value
                elif isinstance(value, (int, float)) and not isinstance(value, bool):
                    signed_map[str(key)] = {"signed_points": value}
                elif isinstance(value, list) and key in (
                        "region_stats", "region_results", "items", "coverage"):
                    for row in value:
                        absorb(row)

        if explicit is not None:
            absorb(explicit)
            return signed_map, snapshot_id

        # 已签基线优先取不可变的已发布快照产物，而不是可被后续发布覆盖的台账。
        snapshots = self._read_jsonl(self._snapshot_file(batch_key))
        region_list_keys = (
            "signed_regions", "region_coverage", "regions",
            "region_stats", "region_results", "region_rows", "items")

        def has_payload(snap: dict[str, Any]) -> bool:
            return any(k in snap for k in region_list_keys) or any(
                k in snap for k in
                ("signed_region_counts", "signed_counts"))

        def absorb_snapshot(snap: dict[str, Any]) -> None:
            for list_key in region_list_keys:
                if isinstance(snap.get(list_key), list):
                    absorb(snap[list_key])
            for counts_key in ("signed_region_counts", "signed_counts"):
                if isinstance(snap.get(counts_key), dict):
                    for key, value in snap[counts_key].items():
                        if isinstance(value, dict):
                            signed_map.setdefault(str(key), {}).update(value)
                        else:
                            signed_map.setdefault(str(key), {})[
                                "signed_points"] = value
            if isinstance(snap.get("signed_region_status"), dict):
                for key, value in snap["signed_region_status"].items():
                    signed_map.setdefault(str(key), {})[
                        "signed_coverage_status"] = value

        # 1) 显式指定的已发布快照。
        if snapshot_id:
            for snap in snapshots:
                if snap.get("snapshot_id") == snapshot_id:
                    absorb_snapshot(snap)
                    return signed_map, snapshot_id

        # 2) 最早的带区域载荷快照（与 get_snapshot 返回的首条一致，即历史已签基线）。
        for snap in snapshots:
            if has_payload(snap):
                absorb_snapshot(snap)
                return signed_map, snap.get("snapshot_id")

        # 3) 兜底：外部写入 / 旧版本留下的签字台账。
        ledger = self._read_json_file(rc.signed_ledger_path(self._data_root(), batch_key))
        if ledger:
            absorb(ledger)
            return signed_map, ledger.get("snapshot_id")
        return signed_map, snapshot_id

    @staticmethod
    def _build_diff_item(batch_key: str, key: str,
                         signed: dict[str, Any] | None,
                         current: dict[str, Any] | None) -> dict[str, Any]:
        # -- 已签侧 --
        signed_present = signed is not None
        signed_points: int | None = None
        signed_status = rc.NOT_PRODUCED if not signed_present else rc.OBSERVED
        signed_name: Any = None
        if signed_present:
            raw_points = rc.pick_first(
                signed, ("signed_points", "signed_count", "total_points",
                         "confirmed_count", "points", "count", "已签数"))
            if raw_points is not None:
                try:
                    signed_points = int(raw_points)
                except (TypeError, ValueError):
                    signed_points = None
            status_value = rc.pick_first(
                signed, ("signed_coverage_status", "coverage_status",
                         "coverage_state", "status"))
            signed_status = (rc.normalize_status(status_value)
                              if status_value is not None else
                              (rc.ZERO_OBSERVATION if signed_points == 0
                               else rc.CONFIRMED if signed_points is not None
                               else rc.NOT_PRODUCED))
            signed_name = signed.get("region_name")

        # -- 新确认侧：只认真正确认过的版本作为 "新确认数" --
        current_status = rc.NOT_PRODUCED
        confirmed_points: int | None = None
        current_points: int | None = None
        region_code = region_id = region_name = None
        current_confirmed = False
        if current is not None:
            current_status = rc.normalize_status(
                current.get("coverage_state") or current.get("coverage_status"))
            current_points = rc.extract_point_count(current)
            current_confirmed = bool(current.get("confirmed"))
            region_code = current.get("region_code")
            region_id = current.get("region_id")
            region_name = current.get("region_name")
            if current_status == rc.NOT_PRODUCED:
                confirmed_points = None
            elif current_confirmed:
                confirmed_points = current_points
            else:
                # 已产出但未单独确认：新确认数严格保持 None。
                confirmed_points = None
        if region_name is None:
            region_name = signed_name
        if isinstance(signed, dict):
            region_code = region_code or signed.get("region_code")
            region_id = region_id or signed.get("region_id")

        # 算术差额：缺失按 0 基线参与更正差额；严格差额保留 None 语义。
        signed_baseline = signed_points or 0
        confirmed_baseline = confirmed_points if confirmed_points is not None else (
            current_points or 0)
        difference = confirmed_baseline - signed_baseline
        if signed_points is not None and confirmed_points is not None:
            strict_difference: int | None = confirmed_points - signed_points
        else:
            strict_difference = None

        if not signed_present and current_status == rc.NOT_PRODUCED:
            change_type = "not_produced"
        elif (signed_status == rc.NOT_PRODUCED or not signed_present) \
                and current_status == rc.CONFIRMED:
            change_type = "newly_confirmed"
        elif (signed_status == rc.NOT_PRODUCED or not signed_present) \
                and current_status == rc.ZERO_OBSERVATION:
            change_type = "new_zero_observation"
        elif current_status == rc.NOT_PRODUCED and signed_points:
            change_type = "now_missing"
        elif difference > 0:
            change_type = "increased"
        elif difference < 0:
            change_type = "decreased"
        elif current_status == rc.ZERO_OBSERVATION:
            change_type = "unchanged_zero"
        else:
            change_type = "unchanged"

        return {
            "difference_id": f"DIFF-{batch_key}-{key}",
            "region_key": key,
            "region_code": region_code,
            "region_id": region_id,
            "region_name": region_name,
            # 已签数
            "signed_present": signed_present,
            "signed_count": signed_baseline,
            "signed_points": signed_baseline,
            "signed_count_strict": signed_points,
            "已签数": signed_baseline,
            "signed_coverage_status": rc.status_token(signed_status),
            # 新确认数
            "confirmed_present": confirmed_points is not None,
            "confirmed": current_confirmed,
            "confirmed_count": confirmed_baseline,
            "new_confirmed_count": confirmed_baseline,
            "confirmed_points": confirmed_baseline,
            "current_count": current_points if current_points is not None else 0,
            "confirmed_count_strict": confirmed_points,
            "新确认数": confirmed_baseline,
            "confirmed_coverage_status": rc.status_token(current_status),
            "coverage_status": rc.status_token(current_status),
            # 差额
            "difference": difference,
            "delta": difference,
            "diff": difference,
            "差额": difference,
            "strict_difference": strict_difference,
            "change_type": change_type,
        }

    def _upsert_reconcile_entry(self, report_id: str,
                                report: dict[str, Any]) -> dict[str, Any] | None:
        rows = store.rows(self.MODULE)
        for row in rows:
            if row.get("快照编号") == report_id:
                row["结果条数"] = str(report["summary"]["items_total"])
                row["快照水位"] = (
                    f"对账差异{report['summary']['changed']}_"
                    f"净差额{report['summary']['net_difference']}")
                row["status"] = "已发布"
                row["pending"] = False
                return row
        entry, _ = self.create_entry({
            "快照编号": report_id,
            "分析口径": "region_history_reconciliation",
            "网格层级": "region",
            "时间范围": report["batch_key"],
            "结果条数": str(report["summary"]["items_total"]),
            "生成方式": "history_reconciliation",
            "快照水位": (
                f"对账差异{report['summary']['changed']}_"
                f"净差额{report['summary']['net_difference']}"),
            "生成时间": time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime()),
        })
        if entry:
            self.run_action(entry["id"], "发布快照")
        return entry

    # 未知方法名的宽松关键字分派（读取类对账查询优先于重新发起对账）。
    def __getattr__(self, name: str) -> Any:
        if name.startswith("_"):
            raise AttributeError(name)
        n = name.lower().replace("_", "")

        def bind(target: str) -> Any:
            canonical = getattr(self, target)

            def _shim(*args: Any, **kwargs: Any) -> Any:
                return canonical(*args, **kwargs)

            _shim.__name__ = name
            return _shim

        # 重新核对覆盖 / 已确认版本。
        if any(f in n for f in ("recheck", "reverify", "recheckcoverage",
                                "checkregion", "regioncheck", "checkcoverage",
                                "reconsider")):
            return bind("recheck_region_coverage")
        # 对账报告的列表 / 读取。
        if "reconcil" in n and n.startswith(("list", "all", "historylist")):
            return bind("list_reconciliations")
        if "reconcil" in n and n.startswith(("get", "read", "fetch", "show", "load")):
            return bind("get_reconciliation")
        # 发起 / 执行历史对账。
        if "reconcil" in n or "duizhang" in n or any(
                f in n for f in ("historydiff", "historicaldiff", "signeddiff")):
            return bind("reconcile_history")
        # 快照请求确认 / 发布。
        if "confirm" in n and "request" in n or "publishrequest" in n or (
                "confirmsnapshot" in n):
            return bind("confirm_snapshot_request")
        # 提交请求。
        if "submit" in n and "request" in n or "submitsnapshot" in n:
            return bind("submit_snapshot_request")
        # 打开 / 创建请求。
        if any(f in n for f in ("opensnapshotrequest", "createsnapshotrequest",
                                "startsnapshotrequest", "openrequest",
                                "newsnapshotrequest", "snapshotrequest")) and \
                n.startswith(("open", "create", "start", "new", "build", "make")):
            return bind("open_snapshot_request")
        # 读取请求。
        if "request" in n and n.startswith(("get", "read", "load", "fetch",
                                            "show", "refresh", "find")):
            return bind("get_snapshot_request")
        # 不落盘预览。
        if "preview" in n or "dryrun" in n or "dry_run" in n:
            return bind("preview_snapshot")
        # 部分交接。
        if "handoff" in n or "handover" in n or "partial" in n or any(
                f in n for f in ("transfer", "deliver", "passon", "pass_over")):
            return bind("handoff_partial_snapshot")
        if "recheck" in n or "reverify" in n:
            return bind("recheck_region_coverage")
        raise AttributeError(f"{type(self).__name__} 没有方法 {name!r}")
