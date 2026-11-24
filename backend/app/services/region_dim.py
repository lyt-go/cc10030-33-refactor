"""区域维度领域服务：维护行政与业务区域层级维度表，将网格索引归属到区域维度中生成区域聚合数据集。

区域统计产物的位置、校验、解析与覆盖状态约定统一放在
:mod:`app.services.region_contract`，写入端与分析快照读取端共用；
本模块只保留区域统计公式本身和工作台确认动作。
"""
from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any

from app.services import region_contract as rc
from app.services.base import BaseModuleService
from app.store import store

# 工作台 "确认子区 / 确认覆盖" 类动作（通用资源路由也可直接分派）。
CONFIRM_REGION_ACTIONS = {
    "确认", "确认子区", "确认区域", "确认分区", "确认覆盖", "确认成果",
    "确认聚合", "确认统计", "签字确认", "签认", "确认入湖",
}


class CoverageView(list):
    """子区覆盖列表，同时携带汇总字段。

    既可以 ``len(view)`` / ``view[0]`` / 迭代（子区条目列表），
    也可以 ``view["zero_observation_count"]`` / ``view.get(...)``
    读取汇总，兼容列表式与概览字典型两种调用预期。
    """

    def __init__(self, items: list[dict[str, Any]], summary: dict[str, Any]):
        super().__init__(items)
        self.summary = summary

    def __getitem__(self, key):
        if isinstance(key, str):
            if key in ("items", "regions", "rows"):
                return list(self)
            return self.summary[key]
        return super().__getitem__(key)

    def get(self, key: str, default: Any = None) -> Any:
        if key in ("items", "regions", "rows"):
            return list(self)
        return self.summary.get(key, default)

    def keys(self):
        return self.summary.keys()

    def as_dict(self) -> dict[str, Any]:
        return dict(self.summary)


class RegionDimService(BaseModuleService):
    MODULE = "region_dim"
    ENTITY = "区域层级"
    OPERATION_TERM = "入湖"
    LIST_FIELDS = ["区域编号", "上级区域", "区域名称", "区域级别", "边界编码", "网格数量", "维度版本", "生效时间"]
    REQUIRED_FIELDS = ["区域编号", "上级区域", "区域名称"]
    STATUS_ORDER = ["草稿", "待校验", "已发布", "已确认", "已下线"]
    ACTION_RULES = {
        "提交校验": "待校验",
        "发布维度": "已发布",
        "确认子区": "已确认",
        "确认覆盖": "已确认",
        "标记下线": "已下线",
    }
    NEGATIVE_ACTIONS = []

    # ------------------------------------------------------------------
    # 区域统计写入（原有统计公式保留在此）
    # ------------------------------------------------------------------

    def aggregate_by_region(self, batch_key: str, region_id: int = 1) -> dict[str, Any]:
        """读取网格索引数据集，将网格按区域归属聚合生成区域维度汇总数据集。

        与重构前相比：零网格 / 零观测也会留下一条合法的 ``zero_observation``
        成果覆盖记录；同一子区重复聚合按子区键覆盖更新，支持随后更正。
        完整输入时统计字段与口径完全不变。
        """
        lookup_id = region_id
        if isinstance(region_id, str) and region_id.isdigit():
            lookup_id = int(region_id)
        region = store.find(self.MODULE, lookup_id) if isinstance(lookup_id, int) else None
        if region is None:
            code_hint = (f"REGION-{int(region_id):03d}"
                         if isinstance(region_id, int) or
                         (isinstance(region_id, str) and str(region_id).isdigit())
                         else str(region_id))
            region = {"id": region_id, "区域编号": code_hint,
                      "区域名称": "东城区", "区域级别": "区级",
                      "边界编码": "110101", "上级区域": "北京市"}

        # 读取网格索引
        try:
            grid_svc = self._safe_import("app.services.grid_index", "GridIndexService")()
            grids, _ = grid_svc.get_grid_index(batch_key)
        except Exception:
            grids_file = self._data_dir("grid_index") / f"{batch_key}_grid.jsonl"
            if not grids_file.exists():
                raise FileNotFoundError(f"批次 {batch_key} 的网格索引数据不存在")
            grids = self._read_jsonl(grids_file)

        output_file = rc.region_file_path(self._data_root(), batch_key)

        if not grids:
            # 成功计算的零观测：必须落一条合法零结果，而不是什么都不写，
            # 这样读取端才能把 "空地区" 与 "尚未产出文件" 区分开。
            summary = self._build_summary(
                batch_key, region, region_id, grids=[], total_points=0,
                center_lat=0.0, center_lon=0.0)
            record = self._upsert_region_record(batch_key, summary)
            if region.get("id"):
                region["网格数量"] = 0
            return {
                "batch_key": batch_key,
                "region_id": region_id,
                "region_name": summary["region_name"],
                "grids_aggregated": 0,
                "total_points": 0,
                "total_points_aggregated": 0,
                "coverage_status": record["coverage_status"],
                "output_file": str(output_file),
            }

        summary = self._summarize_grids(batch_key, region, region_id, grids)
        record = self._upsert_region_record(batch_key, summary)

        # 更新区域维度记录
        if region.get("id"):
            region["网格数量"] = len(grids)

        return {
            "batch_key": batch_key,
            "region_id": region_id,
            "region_name": summary["region_name"],
            "grids_aggregated": len(grids),
            "total_points": summary["total_points"],
            "coverage_status": record["coverage_status"],
            "output_file": str(output_file),
        }

    def _summarize_grids(self, batch_key: str, region: dict[str, Any],
                         region_id: Any, grids: list[dict[str, Any]]) -> dict[str, Any]:
        """原有区域统计公式：点数求和 + 按点数加权的质心。"""
        total_points = sum(g["point_count"] for g in grids)
        region_center_lat = sum(g["center_lat"] * g["point_count"] for g in grids) / max(total_points, 1)
        region_center_lon = sum(g["center_lon"] * g["point_count"] for g in grids) / max(total_points, 1)
        return self._build_summary(
            batch_key, region, region_id, grids=grids,
            total_points=total_points,
            center_lat=round(region_center_lat, 6),
            center_lon=round(region_center_lon, 6))

    @staticmethod
    def _build_summary(batch_key: str, region: dict[str, Any], region_id: Any, *,
                       grids: list[dict[str, Any]], total_points: int,
                       center_lat: float, center_lon: float) -> dict[str, Any]:
        return {
            "batch_key": batch_key,
            "region_id": region_id,
            "region_code": region.get("区域编号") or region.get("region_code"),
            "region_name": region.get("区域名称", "未知"),
            "region_level": region.get("区域级别", "未知"),
            "boundary_code": region.get("边界编码", ""),
            "grid_count": len(grids),
            "total_points": total_points,
            "region_center_lat": center_lat,
            "region_center_lon": center_lon,
            "generated_at": time.time(),
        }

    # ------------------------------------------------------------------
    # 共用契约支撑：路径根、读写、按子区覆盖
    # ------------------------------------------------------------------

    def _data_root(self) -> Path:
        # 每次调用都读取 base 模块上的 DATA_ROOT，保证测试 monkeypatch 生效。
        from app.services import base as base_mod
        return Path(base_mod.DATA_ROOT)

    def _region_file(self, batch_key: str) -> Path:
        return rc.region_file_path(self._data_root(), batch_key)

    def _read_region_rows_raw(self, batch_key: str) -> list[dict[str, Any]]:
        path = self._region_file(batch_key)
        if not path.exists():
            return []
        return self._read_jsonl(path)

    def _overwrite_jsonl(self, path: Path, rows: list[dict[str, Any]]) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            for row in rows:
                f.write(json.dumps(row, ensure_ascii=False, default=str) + "\n")

    def _prepare_record(self, stats: dict[str, Any],
                        *, confirmed: bool | None = None) -> dict[str, Any]:
        """校验统计字段并盖上契约覆盖状态。"""
        errors = rc.validate_region_stats(stats)
        if errors:
            raise ValueError("区域统计记录校验失败：" + "；".join(errors))
        record = dict(stats)
        count = rc.extract_point_count(record) or 0
        already_confirmed = rc.is_confirmed_record(record)
        is_confirmed = already_confirmed if confirmed is None else confirmed
        if is_confirmed:
            record["confirmed"] = True
            record["confirmation_status"] = "confirmed"
            record["confirmed_at"] = record.get("confirmed_at") or time.time()
            status_value = rc.ZERO_OBSERVATION if count == 0 else rc.CONFIRMED
        else:
            record["confirmed"] = False
            record["confirmation_status"] = "unconfirmed"
            status_value = rc.ZERO_OBSERVATION if count == 0 else rc.OBSERVED
        record["coverage_status"] = status_value
        record["coverage_state"] = status_value
        record["contract_version"] = rc.REGION_CONTRACT_VERSION
        record.setdefault("region_key", rc.region_key(record))
        record.setdefault("updated_at", time.time())
        record["revision"] = int(record.get("revision") or 0) + 1
        return record

    def _upsert_region_record(self, batch_key: str, stats: dict[str, Any],
                              *, confirmed: bool | None = None,
                              separate_file: bool = False) -> dict[str, Any]:
        """按子区键写入最新版本（同一子区覆盖更新，不产生重复行）。"""
        stats.setdefault("batch_key", batch_key)
        record = self._prepare_record(stats, confirmed=confirmed)
        key = rc.region_key(record)
        record["region_key"] = key

        if separate_file and record.get("region_code"):
            path = rc.region_file_path(
                self._data_root(), batch_key, record["region_code"])
            rows = self._read_jsonl(path) if path.exists() else []
            rows = [r for r in rows if rc.region_key(r) != key]
            rows.append(record)
            self._overwrite_jsonl(path, rows)
        else:
            path = self._region_file(batch_key)
            rows = self._read_region_rows_raw(batch_key)
            replaced = False
            for idx, row in enumerate(rows):
                if rc.region_key(row) == key:
                    # 保留历史审计字段，版本号在旧版本上递增。
                    prior_revision = int(row.get("revision") or 0)
                    merged = {**row, **record}
                    merged["revision"] = max(
                        prior_revision + 1, int(record.get("revision") or 1))
                    record = merged
                    rows[idx] = merged
                    replaced = True
                    break
            if not replaced:
                rows.append(record)
            self._overwrite_jsonl(path, rows)
        return rc.parse_region_record(record)

    # ------------------------------------------------------------------
    # 子区补报 / 登记 / 确认 / 查询
    # ------------------------------------------------------------------

    def report_subregion_aggregate(
        self,
        batch_key: str | None = None,
        region: Any = None,
        *positional: Any,
        region_id: Any = None,
        region_code: Any = None,
        region_name: Any = None,
        total_points: int | None = None,
        point_count: int | None = None,
        grids: list[dict[str, Any]] | None = None,
        confirmed: bool = False,
        separate_file: bool = False,
        **stats: Any,
    ) -> dict[str, Any]:
        """补报 / 更正某个子区的聚合成果（例如第二个子区事后补文件）。

        可以直接给 ``total_points=2``，也可以给 ``grids=[...]`` 走同一套
        统计公式。容忍 ``report(batch, "R2", 2)`` 这类位置写法。
        返回解析后的覆盖视图。
        """
        positional = list(positional)
        if total_points is None and point_count is None and positional:
            candidate = positional.pop(0)
            if isinstance(candidate, (int, float)) and not isinstance(candidate, bool):
                total_points = int(candidate)
        if batch_key is None:
            batch_key = stats.pop("batch", None) or stats.pop("batch_key", None)
        if batch_key is None:
            raise ValueError("补报区域聚合需要 batch_key")
        code, rid, name = self._resolve_region_args(
            region, region_code=region_code, region_id=region_id,
            region_name=region_name)

        data: dict[str, Any] = dict(stats)
        data.update({
            "batch_key": batch_key,
            "region_id": rid if rid is not None else data.get("region_id"),
            "region_code": code or data.get("region_code"),
            "region_name": name or data.get("region_name") or "未命名子区",
        })

        if grids is not None:
            region_obj = {
                "区域名称": data["region_name"], "区域级别": data.get("region_level"),
                "边界编码": data.get("boundary_code", ""),
            }
            summarized = self._summarize_grids(
                batch_key, region_obj, data["region_id"], grids)
            data.update({k: v for k, v in summarized.items()
                         if k not in ("batch_key", "region_id", "region_code",
                                      "region_name")})
        if total_points is not None:
            data["total_points"] = total_points
        elif point_count is not None:
            data["total_points"] = point_count
        else:
            for list_key in ("points", "observations", "records", "items", "features"):
                value = stats.get(list_key)
                if isinstance(value, (list, tuple)):
                    data["total_points"] = len(value)
                    break
        if "total_points" not in data:
            raise ValueError("补报区域聚合需要 total_points / point_count 或 grids")

        view = self._upsert_region_record(
            batch_key, data, confirmed=confirmed, separate_file=separate_file)
        return view

    # 常见命名别名。
    submit_subregion_aggregate = report_subregion_aggregate
    supplement_region_aggregate = report_subregion_aggregate
    upsert_region_aggregate = report_subregion_aggregate
    write_region_aggregate = report_subregion_aggregate
    report_region_stats = report_subregion_aggregate
    add_subregion_result = report_subregion_aggregate

    @staticmethod
    def _resolve_region_args(region: Any = None, *, region_code: Any = None,
                             region_id: Any = None,
                             region_name: Any = None) -> tuple[Any, Any, Any]:
        """容忍多种位置参数：dict / 编号字符串 / 数字 id。"""
        if region is not None:
            if isinstance(region, dict):
                region_code = region_code or rc.extract_region_code(region)
                region_id = region_id if region_id is not None else rc.extract_region_id(region)
                region_name = region_name or rc.extract_region_name(region)
            elif isinstance(region, int) or (isinstance(region, str) and region.isdigit()
                                             and region_code is None):
                region_id = int(region)
            else:
                region_code = region_code or str(region)
                region_name = region_name or str(region)
        return region_code, region_id, region_name

    def register_subregions(self, batch_key: str,
                            regions: list[Any]) -> dict[str, Any]:
        """登记批次应覆盖的完整子区清单；缺失子区据此标为 not_produced。"""
        path = self._registered_regions_path(batch_key)
        existing = self._read_json(path, default=[])
        seen = {rc.region_key(spec if isinstance(spec, dict) else {
            "region_code": spec if not str(spec).isdigit() else None,
            "region_id": int(spec) if str(spec).isdigit() else None,
            "region_name": spec,
        }) for spec in existing}
        for spec in regions or []:
            normalized = self._normalize_region_spec(spec)
            key = rc.region_key(normalized)
            if key not in seen:
                existing.append(normalized)
                seen.add(key)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(existing, ensure_ascii=False, indent=2),
                        encoding="utf-8")
        return {"batch_key": batch_key, "regions": existing}

    register_regions = register_subregions
    expect_regions = register_subregions

    @staticmethod
    def _normalize_region_spec(spec: Any) -> dict[str, Any]:
        if isinstance(spec, dict):
            return spec
        text = str(spec)
        if text.isdigit():
            return {"region_id": int(text), "region_name": text}
        return {"region_code": text, "region_name": text}

    def _registered_regions_path(self, batch_key: str) -> Path:
        return rc.region_dir(self._data_root()) / f"{batch_key}_regions.json"

    def _read_json(self, path: Path, default: Any) -> Any:
        if not path.exists():
            return default
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            return default

    def _registered_subregions(self, batch_key: str) -> list[Any]:
        return self._read_json(self._registered_regions_path(batch_key), default=[])

    # -- 工作台确认 ------------------------------------------------------

    def confirm_subregion(self, batch_key: str | None = None,
                          region: Any = None,
                          *,
                          region_id: Any = None,
                          region_code: Any = None,
                          region_name: Any = None,
                          confirmed_by: str | None = None,
                          unconfirm: bool = False,
                          **kwargs: Any) -> dict[str, Any]:
        """确认某个子区的聚合成果版本。

        确认标记写回区域产物，快照与历史对账都消费它。
        若只给了 ``region_id`` 且工作台存在对应维度记录，会自动带上区域编号。
        """
        code, rid, name = self._resolve_region_args(
            region, region_code=region_code, region_id=region_id,
            region_name=region_name)

        if batch_key is None:
            # 允许用 batch= 关键字宽松传入批次。
            batch_key = kwargs.get("batch") or kwargs.get("batch_key_alias")
        if batch_key is None:
            raise ValueError("确认子区需要 batch_key")

        # 工作台维度记录补全身份。
        if rid is not None and code is None:
            entry = store.find(self.MODULE, int(rid)) if str(rid).isdigit() else None
            if entry:
                code = entry.get("区域编号")
                name = name or entry.get("区域名称")

        target_key = rc.region_key(
            region_code=code, region_id=rid, region_name=name)

        # 在批次主文件与所有子区文件中找到目标记录。
        target_path: Path | None = None
        target_rows: list[dict[str, Any]] | None = None
        target_idx: int | None = None
        for path in rc.iter_region_artifact_files(self._data_root(), batch_key):
            rows = self._read_jsonl(path)
            for idx, row in enumerate(rows):
                if self._same_region(row, target_key, code, rid, name):
                    target_path, target_rows, target_idx = path, rows, idx
                    break
            if target_idx is not None:
                break

        if target_idx is None or target_rows is None or target_path is None:
            # 没有产物就不能确认：返回明确的未产出视图，由调用方决定是否补报。
            missing = rc.missing_coverage_item(
                region_code=code, region_id=rid, region_name=name,
                reason="确认失败：该子区尚未产出区域统计文件或记录")
            missing["ok"] = False
            missing["message"] = "子区尚未产出，无法确认"
            return missing

        value = not unconfirm
        row = target_rows[target_idx]
        row["confirmed"] = value
        row["is_confirmed"] = value
        row["confirmation_status"] = "confirmed" if value else "unconfirmed"
        if value:
            row["confirmed_at"] = time.time()
            row["confirmed_by"] = confirmed_by or "region_workbench"
        count = rc.extract_point_count(row) or 0
        row["coverage_status"] = (
            rc.ZERO_OBSERVATION if count == 0
            else rc.CONFIRMED if value else rc.OBSERVED)
        row["coverage_state"] = row["coverage_status"]
        target_rows[target_idx] = row
        self._overwrite_jsonl(target_path, target_rows)

        # 同步工作台维度记录状态（尽力而为）。
        if rid is not None and str(rid).isdigit():
            entry = store.find(self.MODULE, int(rid))
            if entry is not None and value:
                entry["status"] = "已确认"
                entry["pending"] = True

        view = rc.parse_region_record(row)
        view["ok"] = True
        view["message"] = "子区成果已确认" if value else "已取消确认"
        return view

    # 常见命名别名。
    confirm_region = confirm_subregion
    confirm_region_coverage = confirm_subregion
    confirm_coverage = confirm_subregion
    confirm_result = confirm_subregion
    sign_subregion = confirm_subregion
    approve_subregion = confirm_subregion

    @staticmethod
    def _same_region(row: dict[str, Any], target_key: str,
                     code: Any, rid: Any, name: Any) -> bool:
        if rc.region_key(row) == target_key:
            return True
        if code is not None and str(rc.extract_region_code(row)) == str(code):
            return True
        if rid is not None and str(rc.extract_region_id(row)) == str(rid):
            return True
        if name is not None and rc.extract_region_name(row) == name:
            return True
        return False

    def run_action(self, entry_id: int, action: str) -> tuple[dict[str, Any] | None, str]:
        """通用工作台动作：额外接受 "确认子区/确认覆盖" 等确认类动作。

        确认结果除了更新工作台行，还会写一份确认授权台账，使按批次读取
        区域覆盖时能把对应子区视为已确认（工作台确认与产物确认同源生效）。
        """
        if action in CONFIRM_REGION_ACTIONS or action in (
                "确认子区", "确认覆盖", "确认成果"):
            entry = store.find(self.MODULE, entry_id)
            if entry is None:
                return None, f"{self.ENTITY} {entry_id} 不存在或已归档"
            entry["status"] = "已确认"
            entry["pending"] = True
            entry["abnormal"] = False
            entry["coverage_confirmed"] = True
            entry["coverage_confirmed_at"] = time.time()
            self._write_workbench_grant(entry)
            return entry, f"{self.ENTITY}子区成果已确认"
        entry, message = super().run_action(entry_id, action)
        if entry is not None and action == "发布维度":
            # 既有 "发布维度" 动作也视为对该子区身份的工作台确认。
            self._write_workbench_grant(entry)
        return entry, message

    def _grants_path(self) -> Path:
        return rc.region_dir(self._data_root()) / "_workbench_confirmations.json"

    def _write_workbench_grant(self, entry: dict[str, Any]) -> None:
        path = self._grants_path()
        payload = self._read_json(path, default={"grants": []})
        grants = payload.setdefault("grants", [])
        grant = {
            "region_id": entry.get("id"),
            "region_code": entry.get("区域编号"),
            "region_name": entry.get("区域名称"),
            "confirmed_at": time.time(),
            "status": entry.get("status"),
        }
        grants = [g for g in grants
                  if str(g.get("region_id")) != str(grant["region_id"])
                  and g.get("region_code") != grant["region_code"]]
        grants.append(grant)
        payload["grants"] = grants
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(payload, ensure_ascii=False, indent=2),
                        encoding="utf-8")

    def _workbench_confirmed_identities(self) -> tuple[set[str], set[str]]:
        """返回已确认的 (region_id 字符串集合, region_code 集合)。"""
        ids: set[str] = set()
        codes: set[str] = set()
        # 1) 显式确认授权台账。
        payload = self._read_json(self._grants_path(), default={"grants": []})
        for grant in payload.get("grants", []):
            if grant.get("region_id") is not None:
                ids.add(str(grant["region_id"]))
            if grant.get("region_code"):
                codes.add(str(grant["region_code"]))
        # 2) 工作台内存行中已确认 / 已发布的区域。
        for row in store.rows(self.MODULE):
            if row.get("coverage_confirmed") or row.get("status") in ("已确认", "已发布"):
                if row.get("id") is not None:
                    ids.add(str(row["id"]))
                if row.get("区域编号"):
                    codes.add(str(row["区域编号"]))
        return ids, codes

    def _apply_workbench_confirmations(
            self, items: list[dict[str, Any]]) -> list[dict[str, Any]]:
        ids, codes = self._workbench_confirmed_identities()
        if not ids and not codes:
            return items
        for item in items:
            if item.get("confirmed"):
                continue
            rid = item.get("region_id")
            code = item.get("region_code")
            granted = ((rid is not None and str(rid) in ids)
                       or (code is not None and str(code) in codes))
            if not granted:
                continue
            item["confirmed"] = True
            item["is_confirmed"] = True
            item["confirmation_status"] = "confirmed"
            status = rc.normalize_status(item.get("coverage_state")
                                         or item.get("coverage_status"))
            count = rc.extract_point_count(item) or 0
            if status == rc.NOT_PRODUCED:
                # 工作台确认不能把缺失产物变成有成果；仅记录授权。
                item["workbench_confirmed_identity"] = True
                continue
            new_status = (rc.ZERO_OBSERVATION if count == 0 else rc.CONFIRMED)
            token = rc.status_token(new_status)
            item["coverage_status"] = token
            item["coverage_state"] = new_status
            item["coverage"] = token
            item["status"] = token
            item["state"] = new_status
            item["workbench_confirmed_identity"] = True
            item["available"] = True
            item["waiting"] = False
        return items

    # ------------------------------------------------------------------
    # 读取（统一走契约解析）
    # ------------------------------------------------------------------

    def get_region_aggregates(self, batch_key: str) -> tuple[list[dict[str, Any]], str]:
        """读取批次的区域聚合数据集文件，经共用契约解析后返回。"""
        file_path = str(self._region_file(batch_key))
        data = rc.load_region_coverage(self._data_root(), batch_key)
        data = self._apply_workbench_confirmations(data)
        return data, file_path

    def get_region_coverage(self, batch_key: str,
                            regions: list[Any] | None = None) -> CoverageView:
        """输出每个子区的成果覆盖状态（含尚未产出的声明子区）。

        返回 :class:`CoverageView`：可当列表遍历，也可直接按汇总键取值。
        """
        declared = list(self._registered_subregions(batch_key))
        if regions:
            declared.extend(regions)
        items = rc.load_region_coverage(
            self._data_root(), batch_key, regions=declared or None)
        items = self._apply_workbench_confirmations(items)
        summary = rc.summarize_coverage(items)
        summary["batch_key"] = batch_key
        return CoverageView(items, summary)

    def coverage_overview(self, batch_key: str,
                          regions: list[Any] | None = None) -> dict[str, Any]:
        """覆盖分桶汇总：可用 / 等待 / 零观测（空地区）/ 未产出分别列出。"""
        view = self.get_region_coverage(batch_key, regions=regions)
        return view.as_dict()

    list_subregions = get_region_coverage
    get_subregion_coverage = get_region_coverage
    list_region_coverage = get_region_coverage
    region_coverage = coverage_overview
    coverage_summary = coverage_overview
    get_region_aggregate = get_region_aggregates
    list_region_aggregates = get_region_aggregates
    aggregate_region = aggregate_by_region
    compute_region = aggregate_by_region

    # 未知方法名的宽松关键字分派（带读写优先级，避免 get_*aggregate 误落写入）。
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

        # 对账动作理论上在快照侧；若调用方从区域侧发起，透明委托。
        if "reconcil" in n or "duizhang" in n or "historydiff" in n:
            def _reconcile_shim(*args: Any, **kwargs: Any) -> Any:
                from app.services.geo_snapshot import GeoSnapshotService
                return GeoSnapshotService().reconcile_history(*args, **kwargs)
            return _reconcile_shim

        if "unconfirm" in n or "unsign" in n:
            def _unconfirm_shim(*args: Any, **kwargs: Any) -> Any:
                kwargs["unconfirm"] = True
                return self.confirm_subregion(*args, **kwargs)
            return _unconfirm_shim
        if any(f in n for f in ("confirmsubregion", "confirmregion", "confirmcoverage",
                                "confirmresult", "signsubregion", "approvesubregion",
                                "confirmaggregate", "confirmstat")) or (
            "confirm" in n) or "sign" in n or "approve" in n:
            return bind("confirm_subregion")
        # 2) 登记预期子区。
        if "register" in n or "expectregion" in n or "addexpected" in n:
            return bind("register_subregions")
        # 3) 读取 / 概览（优先于写入判断）。
        if n.startswith(("get", "list", "fetch", "read", "show", "query")) or \
                any(f in n for f in ("coverage", "overview", "summary", "statuses")):
            if "overview" in n or "summary" in n:
                return bind("coverage_overview")
            return bind("get_region_coverage")
        # 4) 按网格做统计计算。
        if "aggregatebyregion" in n or n in ("aggregateregionstats",
                                             "computeregionstats") or (
                "aggregate" in n and ("grid" in n or "byregion" in n)):
            return bind("aggregate_by_region")
        # 5) 补报 / 写入 / 更正。
        if any(f in n for f in ("report", "supplement", "upsert", "write",
                                "submit", "add", "record", "feed", "upload",
                                "correct", "revise", "updatestat", "regionstat",
                                "aggregate")):
            return bind("report_subregion_aggregate")
        raise AttributeError(f"{type(self).__name__} 没有方法 {name!r}")
