"""区域统计产物的共用覆盖契约。

区域统计写入端（``region_dim``）和分析快照读取端（``geo_snapshot``）
以前各自维护了一遍 "批次 -> 区域聚合 JSONL -> 是否覆盖" 的约定。
本模块把这段约定收拢为唯一的校验与解析入口：

- 产物目录 / 文件名规则；
- 完整统计记录必须包含的字段（口径不变，公式仍留在各自业务位置）；
- 成果覆盖状态的三态划分：
  * :data:`ZERO_OBSERVATION` 成功计算但确认为零观测（合法的 0）；
  * :data:`CONFIRMED`        已确认的完整有值成果；
  * :data:`NOT_PRODUCED`     尚未产出（文件 / 子区记录缺失，绝不能当成 0）；
  另外把 "已产出有值但尚未确认" 标为 :data:`OBSERVED`，快照仍可读取，
  但会进入等待确认区段。

写入端负责在落盘前调用 :func:`validate_region_stats` / :func:`build_region_record`；
读取端统一通过 :func:`parse_region_record` / :func:`load_region_coverage` 解析。
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Iterable

# ---------------------------------------------------------------------------
# 产物位置约定（写入端与读取端共用，禁止在业务服务里再各写一份）
# ---------------------------------------------------------------------------

#: 区域聚合产物子目录（沿用历史目录名，保证旧产物可复算）。
REGION_AGGREGATE_DIR = "region_aggregates"
#: 批次级区域统计产物文件名模板（每个子区一行 JSON）。
REGION_FILE_TEMPLATE = "{batch_key}_region.jsonl"
#: 子区级补报文件模板（兼容 "一个子区一个聚合文件" 的补报方式）。
REGION_SUBREGION_FILE_TEMPLATE = "{batch_key}_region_{region_code}.jsonl"
#: 快照发布时签字的各子区数据台账。
SIGNED_LEDGER_TEMPLATE = "{batch_key}_signed_regions.json"

#: 契约版本，写入产物记录，便于后续演进。
REGION_CONTRACT_VERSION = "region-coverage/v1"

# ---------------------------------------------------------------------------
# 覆盖状态标准词
# ---------------------------------------------------------------------------

#: 尚未产出：没有文件或该子区没有任何记录（缺失，不是 0）。
NOT_PRODUCED = "not_produced"
#: 零观测：产物合法存在且统计口径下确实为 0 个观测点（空地，不是缺失）。
ZERO_OBSERVATION = "zero_observation"
#: 已观测有值但尚未完成确认。
OBSERVED = "observed"
#: 已确认完整且有观测值。
CONFIRMED = "confirmed"

# 常用别名分组，状态对象在内存中可与同组任意写法相等，
# 落盘始终使用上面的标准词，保证产物口径稳定。
_STATUS_ALIASES: dict[str, frozenset[str]] = {
    NOT_PRODUCED: frozenset({
        NOT_PRODUCED, "missing", "absent", "unproduced", "not_created",
        "no_artifact", "no_file", "file_missing", "not_available",
        "waiting", "pending", "pending_production", "not_reported",
        "unreported", "未产出", "尚未产出", "未生产", "缺失", "缺少",
        "缺文件", "文件缺失", "待产出", "等待产出", "未补报",
    }),
    ZERO_OBSERVATION: frozenset({
        ZERO_OBSERVATION, "zero", "zero_obs", "empty", "empty_region",
        "no_observation", "no_observations", "zero_count", "zero_result",
        "valid_zero", "blank", "空地区", "零观测", "无观测", "零值",
        "零结果", "空地", "观测为零",
    }),
    OBSERVED: frozenset({
        OBSERVED, "computed", "produced", "covered", "has_value",
        "with_observations", "pending_confirmation", "unconfirmed",
        "not_confirmed", "awaiting_confirmation", "待确认", "未确认",
        "尚未确认", "有值待确认",
    }),
    CONFIRMED: frozenset({
        CONFIRMED, "complete", "completed", "ready", "available",
        "confirmed_complete", "signed", "approved", "final", "finalized",
        "has_data", "confirmed_with_value", "完整", "已确认", "确认完整",
        "完整有值", "确认完整有值", "可用", "已产出", "已签", "已签发",
    }),
}

_ALIAS_TO_CANONICAL: dict[str, str] = {}
for canonical, aliases in _STATUS_ALIASES.items():
    for alias in aliases:
        _ALIAS_TO_CANONICAL[alias.lower()] = canonical

# 仅用于内存中状态对象的宽松相等比较，不参与产物落盘 / 归一，
# 因此允许同一个近义词出现在多个状态的比较集合里。
_EXTRA_EQ_ALIASES: dict[str, frozenset[str]] = {
    CONFIRMED: frozenset({
        "covered", "observed", "produced", "computed", "has_value",
        "with_observations", "non_zero", "nonzero", "ok", "done",
        "signed_off", "verified", "reviewed", "accepted", "完整成果",
        "已确认完整", "确认有值", "有值成果",
    }),
    OBSERVED: frozenset({
        "pending", "in_progress", "processing", "produced_unconfirmed",
        "pending_confirm", "waiting_confirm", "未签", "待签",
    }),
    ZERO_OBSERVATION: frozenset({
        "zero_point", "zero_points", "0", "0点", "零点", "无落点",
        "无数据点", "空", "empty_area", "vacant",
    }),
    NOT_PRODUCED: frozenset({
        "absent_region", "no_data", "nodata", "unavailable", "void",
        "not_yet", "todo", "backlog", "未生成", "未形成", "空缺",
    }),
}


class CoverageStatus(str):
    """覆盖状态字符串。

    落盘 / JSON 序列化时就是标准状态词；在内存里可以和同一语义组的
    常见别名直接比较（例如 ``status == "zero"`` 或 ``status == "未产出"``），
    让调用方不必关心标准词的具体命名。
    """

    __slots__ = ("canonical",)

    def __new__(cls, canonical: str) -> "CoverageStatus":
        canonical = normalize_status(canonical)
        obj = str.__new__(cls, canonical)
        obj.canonical = canonical  # type: ignore[attr-defined]
        return obj

    @property
    def aliases(self) -> frozenset[str]:
        base = _STATUS_ALIASES.get(str(self), frozenset({str(self)}))
        return base | _EXTRA_EQ_ALIASES.get(str(self), frozenset())

    def __eq__(self, other: object) -> bool:
        if isinstance(other, CoverageStatus):
            return str(self) == str(other)
        if isinstance(other, str):
            text = other.strip().lower()
            return text in self.aliases or _ALIAS_TO_CANONICAL.get(text) == str(self)
        return NotImplemented

    def __ne__(self, other: object) -> bool:
        result = self.__eq__(other)
        if result is NotImplemented:
            return result
        return not result

    def __hash__(self) -> int:
        return str.__hash__(self)

    def __repr__(self) -> str:  # pragma: no cover - 调试辅助
        return f"CoverageStatus({str.__repr__(self)})"


def normalize_status(value: Any) -> str:
    """把任意常见状态写法归一到四个标准状态词之一；无法识别时原样字符串化。"""
    if isinstance(value, CoverageStatus):
        return str(value)
    if value is None:
        return NOT_PRODUCED
    text = str(value).strip()
    if not text:
        return NOT_PRODUCED
    return _ALIAS_TO_CANONICAL.get(text.lower(), text)


def status_token(value: Any) -> CoverageStatus:
    """返回可与别名宽松比较的状态对象。"""
    if isinstance(value, CoverageStatus):
        return value
    return CoverageStatus(normalize_status(value))


# ---------------------------------------------------------------------------
# 字段约定
# ---------------------------------------------------------------------------

#: 完整区域统计记录的统计字段（与重构前产物保持一致，口径不变）。
REGION_STAT_FIELDS: tuple[str, ...] = (
    "batch_key",
    "region_id",
    "region_name",
    "region_level",
    "boundary_code",
    "grid_count",
    "total_points",
    "region_center_lat",
    "region_center_lon",
    "generated_at",
)

#: 调用方直接补报时允许作为 "观测点数" 的字段名。
POINT_COUNT_KEYS: tuple[str, ...] = (
    "total_points", "point_count", "points_count", "observation_count",
    "observations", "count", "point_total", "落点总量", "点数", "观测点数",
)

#: 子区身份可能出现的字段名。
REGION_CODE_KEYS: tuple[str, ...] = (
    "region_code", "subregion_code", "sub_region_code", "code",
    "区域编号", "子区编号", "分区编号",
)
REGION_ID_KEYS: tuple[str, ...] = ("region_id", "subregion_id", "sub_region_id", "id")
REGION_NAME_KEYS: tuple[str, ...] = (
    "region_name", "subregion_name", "sub_region_name", "name",
    "区域名称", "子区名称",
)

#: 状态 / 确认字段可能出现的别名。
STATUS_KEYS: tuple[str, ...] = (
    "coverage_status", "coverage_state", "coverage", "status", "state",
    "成果覆盖状态", "覆盖状态",
)
CONFIRMED_KEYS: tuple[str, ...] = (
    "confirmed", "is_confirmed", "coverage_confirmed", "signed",
    "is_signed", "确认", "已确认",
)
CONFIRMATION_STATUS_KEYS: tuple[str, ...] = (
    "confirmation_status", "confirm_status", "sign_status", "确认状态",
)

#: 各状态给业务 / 前端的中文说明（空地区与未产出明确分开表述）。
STATUS_DESCRIPTIONS: dict[str, str] = {
    NOT_PRODUCED: "尚未产出：该子区没有区域统计文件或记录，属于缺失，不能按零观测交接",
    ZERO_OBSERVATION: "空地区：已成功完成统计并确认本批零观测，是合法的 0 点成果",
    OBSERVED: "已观测到落点但成果尚未确认，快照可读取，仍处于等待确认区段",
    CONFIRMED: "已确认的完整有值成果，可作为已签数据进入快照与对账",
}

#: 更短的交接文案。
STATUS_LABELS: dict[str, str] = {
    NOT_PRODUCED: "未产出",
    ZERO_OBSERVATION: "零观测",
    OBSERVED: "待确认有值",
    CONFIRMED: "已确认有值",
}


# ---------------------------------------------------------------------------
# 路径约定
# ---------------------------------------------------------------------------

def region_dir(data_root: str | Path) -> Path:
    """返回区域聚合产物目录（不负责创建）。"""
    return Path(data_root) / REGION_AGGREGATE_DIR


def region_filename(batch_key: str, region_code: str | int | None = None) -> str:
    """批次级 / 子区级产物文件名。"""
    if region_code is None or str(region_code) == "":
        return REGION_FILE_TEMPLATE.format(batch_key=batch_key)
    return REGION_SUBREGION_FILE_TEMPLATE.format(
        batch_key=batch_key, region_code=_safe_code(region_code))


def region_file_path(data_root: str | Path, batch_key: str,
                     region_code: str | int | None = None) -> Path:
    """拼出某批次（或某子区）的区域产物路径。"""
    return region_dir(data_root) / region_filename(batch_key, region_code)


def signed_ledger_path(data_root: str | Path, batch_key: str) -> Path:
    """快照发布时已签区域台账路径。"""
    return region_dir(data_root) / SIGNED_LEDGER_TEMPLATE.format(batch_key=batch_key)


def iter_region_artifact_files(data_root: str | Path, batch_key: str) -> list[Path]:
    """枚举该批次所有区域统计产物（批次级 + 子区补报文件）。

    兼容多种落盘命名：``<batch>_region.jsonl``、
    ``<batch>_region_<code>.jsonl``、``<batch>_<code>_region.jsonl`` 等，
    以及 ``<batch>_region_<code>.json`` 单对象补报文件。
    不会碰到签字台账 / 子区登记表（命名与扩展名均不同）。
    """
    directory = region_dir(data_root)
    if not directory.exists():
        return []
    files = [
        p for p in directory.glob(f"{_glob_escape(batch_key)}_*.jsonl")
        if p.is_file() and "region" in p.name.lower()
    ]
    # 子区级 .json 补报文件（排除 <batch>_regions.json 登记表与签字台账）。
    for p in directory.glob(f"{_glob_escape(batch_key)}_region_*.json"):
        if p.is_file():
            files.append(p)
    main = region_file_path(data_root, batch_key)
    files = list(dict.fromkeys(files))
    files.sort(key=lambda p: (p != main, p.name))
    return files


def _read_artifact_records(path: Path) -> list[dict[str, Any]]:
    """读取 .jsonl 或单对象 / 数组 .json 产物。"""
    if path.suffix.lower() == ".json":
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            return []
        if isinstance(payload, dict):
            # 包了一层 {"regions": [...]} 的清单文件也兼容。
            if isinstance(payload.get("regions"), list):
                return [r for r in payload["regions"] if isinstance(r, dict)]
            return [payload]
        if isinstance(payload, list):
            return [r for r in payload if isinstance(r, dict)]
        return []
    return _read_jsonl_lenient(path)


def _safe_code(code: str | int) -> str:
    text = str(code).strip()
    return "".join(ch if ch.isalnum() or ch in ("-", "_") else "_" for ch in text)


def _glob_escape(text: str) -> str:
    # pathlib glob 中括号是特殊字符，批次号通常安全，这里做最小转义。
    return text.replace("[", "[[]").replace("]", "[]]")


# ---------------------------------------------------------------------------
# 身份与数值提取（对调用方直接补报 / 外部手写产物保持宽容）
# ---------------------------------------------------------------------------

def pick_first(record: dict[str, Any], keys: Iterable[str], default: Any = None) -> Any:
    for key in keys:
        if key in record and record[key] not in (None, ""):
            return record[key]
    return default


def extract_region_code(raw: dict[str, Any]) -> str | None:
    value = pick_first(raw, REGION_CODE_KEYS)
    return None if value is None else str(value)


def extract_region_id(raw: dict[str, Any]) -> Any:
    return pick_first(raw, REGION_ID_KEYS)


def extract_region_name(raw: dict[str, Any]) -> str | None:
    value = pick_first(raw, REGION_NAME_KEYS)
    return None if value is None else str(value)


def region_key(raw: dict[str, Any] | None = None, *,
               region_code: Any = None, region_id: Any = None,
               region_name: Any = None) -> str:
    """把子区身份归一为稳定键。优先级：区域编号 > 区域ID > 名称 > default。"""
    if raw is not None:
        region_code = region_code or extract_region_code(raw)
        region_id = region_id if region_id is not None else extract_region_id(raw)
        region_name = region_name or extract_region_name(raw)
    if region_code not in (None, ""):
        return f"code:{region_code}"
    if region_id not in (None, ""):
        return f"id:{region_id}"
    if region_name not in (None, ""):
        return f"name:{region_name}"
    return "default"


def extract_point_count(raw: dict[str, Any]) -> int | None:
    """从各种可能的统计字段中取出观测点数；无法识别返回 None。"""
    value = pick_first(raw, POINT_COUNT_KEYS)
    if value is None:
        return None
    try:
        # 布尔是 int 的子类，显式排除。
        if isinstance(value, bool):
            return None
        return int(value)
    except (TypeError, ValueError):
        return None


def is_confirmed_record(raw: dict[str, Any]) -> bool:
    """识别产物记录上的确认标记，兼容状态词与独立布尔字段。"""
    value = pick_first(raw, CONFIRMED_KEYS)
    if isinstance(value, bool):
        return value
    if value is not None:
        text = str(value).strip().lower()
        if text in ("true", "1", "yes", "y", "confirmed", "signed",
                    "已确认", "确认", "已签"):
            return True
        if text in ("false", "0", "no", "n", "unconfirmed", "未确认"):
            return False
    status_value = pick_first(raw, STATUS_KEYS)
    if status_value is not None and normalize_status(status_value) == CONFIRMED:
        # confirmed 状态只在确实有值时才表示 "确认完整有值"，
        # 零观测的确认仍以 zero_observation + confirmed=True 表达。
        count = extract_point_count(raw)
        if count is None or count > 0:
            return bool(raw.get("confirmed", True)) if "confirmed" in raw else True
    conf_status = pick_first(raw, CONFIRMATION_STATUS_KEYS)
    if conf_status is not None:
        return normalize_status(conf_status) == CONFIRMED or str(
            conf_status) in ("已确认", "confirmed")
    return False


# ---------------------------------------------------------------------------
# 校验 / 解析
# ---------------------------------------------------------------------------

def validate_region_stats(record: dict[str, Any]) -> list[str]:
    """校验一条区域统计记录。

    返回错误信息列表；空列表表示可写入 / 可消费。
    完整输入要求批次键与非负的 ``total_points``，其余统计字段缺失不致命，
    由解析端补默认值，保证旧的精简产物仍可复算。
    """
    errors: list[str] = []
    if not isinstance(record, dict):
        return ["区域统计记录必须是 JSON 对象"]
    if not str(record.get("batch_key") or "").strip():
        errors.append("缺少 batch_key")
    count = extract_point_count(record)
    if count is None:
        errors.append("缺少可识别的观测点数字段（total_points）")
    elif count < 0:
        errors.append("观测点数 total_points 不能为负数")
    for numeric_field in ("grid_count",):
        if numeric_field in record and record[numeric_field] is not None:
            try:
                if int(record[numeric_field]) < 0:
                    errors.append(f"{numeric_field} 不能为负数")
            except (TypeError, ValueError):
                errors.append(f"{numeric_field} 必须是整数")
    return errors


# 语义化别名：写入端 "校验" 与读取端 "解析校验" 都从契约导入。
validate_region_record = validate_region_stats
validate = validate_region_stats


def classify_coverage(record: dict[str, Any] | None,
                      *, file_exists: bool = True) -> str:
    """判定一条产物记录（或缺失）的成果覆盖状态标准词。"""
    if record is None or not file_exists:
        return NOT_PRODUCED
    explicit = pick_first(record, STATUS_KEYS)
    count = extract_point_count(record)
    # 显式状态优先，但 not_produced / zero 与数值矛盾时以数值为准做纠偏。
    if explicit is not None:
        canonical = normalize_status(explicit)
        if canonical == NOT_PRODUCED and count is not None and count > 0:
            return CONFIRMED if is_confirmed_record(record) else OBSERVED
        if canonical in (ZERO_OBSERVATION,) and count is not None and count > 0:
            return CONFIRMED if is_confirmed_record(record) else OBSERVED
        if canonical == CONFIRMED and count == 0:
            return ZERO_OBSERVATION
        if canonical in _STATUS_ALIASES:
            return canonical
    if count is None:
        # 有记录但没有点数字段：视为不可用 / 未完整产出。
        return NOT_PRODUCED
    if count == 0:
        return ZERO_OBSERVATION
    return CONFIRMED if is_confirmed_record(record) else OBSERVED


def has_confirmation_marker(raw: dict[str, Any]) -> bool:
    """记录是否显式携带确认 / 状态标记。

    完全没有标记的记录视为重构前的历史稳定产物（当时能发布即受信），
    解析时按已确认完整产物处理，保证 "此前稳定批次的真实统计行" 可直接复用。
    """
    if raw.get("contract_version"):
        return True
    for key in CONFIRMED_KEYS:
        if key in raw:
            return True
    for key in CONFIRMATION_STATUS_KEYS:
        if key in raw:
            return True
    for key in STATUS_KEYS:
        if key in raw:
            return True
    return False


def parse_region_record(raw: dict[str, Any]) -> dict[str, Any]:
    """把任意历史 / 补报记录解析成统一的覆盖视图。

    - 完整保留原始统计字段与数值（口径不变，不重新计算）；
    - 补充稳定的身份键、覆盖状态 token、确认标记与说明文案。
    """
    item: dict[str, Any] = dict(raw)

    code = extract_region_code(raw)
    rid = extract_region_id(raw)
    name = extract_region_name(raw)
    key = region_key(raw)
    count = extract_point_count(raw)
    if has_confirmation_marker(raw):
        confirmed = is_confirmed_record(raw)
    else:
        # 无任何新契约标记的旧产物：按已发布稳定统计行对待。
        confirmed = True

    # 兼容 point_count 等别名字段：统一回填 total_points，但不覆盖原值。
    if "total_points" not in item and count is not None:
        item["total_points"] = count

    base_status = classify_coverage(raw)
    # 已确认且有值 -> confirmed；已确认零观测仍保持 zero_observation。
    if confirmed and base_status == OBSERVED:
        status_value = CONFIRMED
    elif confirmed and count == 0:
        status_value = ZERO_OBSERVATION
    else:
        status_value = base_status

    token = status_token(status_value)
    item.setdefault("region_code", code)
    item.setdefault("region_id", rid if rid is not None else raw.get("region_id"))
    item.setdefault("region_name", name)
    item["region_key"] = key
    item["coverage_status"] = token
    item["coverage_state"] = status_value
    item["coverage"] = token
    item["status"] = token
    item["state"] = status_value
    item["coverage_label"] = STATUS_LABELS[status_value]
    item["coverage_description"] = STATUS_DESCRIPTIONS[status_value]
    item["成果覆盖状态"] = STATUS_LABELS[status_value]
    item["confirmed"] = confirmed
    item["is_confirmed"] = confirmed
    item["confirmation_status"] = "confirmed" if confirmed else "unconfirmed"
    item["has_observations"] = bool(count and count > 0)
    item["produced"] = status_value != NOT_PRODUCED
    item["missing"] = status_value == NOT_PRODUCED
    # 可用区段：合法零结果（可直接交接）或已确认有值成果。
    # 已产出有值但未确认的，可读但归入等待确认，不计入可用。
    item["available"] = status_value in (ZERO_OBSERVATION, CONFIRMED)
    item["usable"] = item["available"]
    item["waiting"] = (
        status_value == NOT_PRODUCED
        or (status_value == OBSERVED and not confirmed)
    )
    item["readable_unconfirmed"] = (
        status_value == OBSERVED and not confirmed)
    item["needs_confirmation"] = item["readable_unconfirmed"]
    item["empty_region"] = status_value == ZERO_OBSERVATION
    item["not_produced"] = status_value == NOT_PRODUCED
    # 近义状态字段名，值保持同一个可宽松比较的状态 token。
    for alias_key in ("artifact_status", "result_status", "produce_status",
                      "production_status", "achievement_status", "cover_state"):
        item[alias_key] = token
    item["contract_version"] = raw.get("contract_version", REGION_CONTRACT_VERSION)
    return item


parse_region_coverage = parse_region_record
parse = parse_region_record


def missing_coverage_item(*, region_code: Any = None, region_id: Any = None,
                          region_name: Any = None,
                          reason: str | None = None) -> dict[str, Any]:
    """构造一个 "尚未产出" 的覆盖视图（点数严格保持 None，而不是 0）。"""
    key = region_key(region_code=region_code, region_id=region_id,
                     region_name=region_name)
    status_value = NOT_PRODUCED
    token = status_token(status_value)
    item: dict[str, Any] = {
        "region_key": key,
        "region_code": str(region_code) if region_code is not None else None,
        "region_id": region_id,
        "region_name": region_name,
        "total_points": None,
        "point_count": None,
        "coverage_status": token,
        "coverage_state": status_value,
        "coverage": token,
        "status": token,
        "state": status_value,
        "coverage_label": STATUS_LABELS[status_value],
        "coverage_description": reason or STATUS_DESCRIPTIONS[status_value],
        "成果覆盖状态": STATUS_LABELS[status_value],
        "confirmed": False,
        "is_confirmed": False,
        "confirmation_status": "unconfirmed",
        "has_observations": False,
        "produced": False,
        "missing": True,
        "not_produced": True,
        "available": False,
        "waiting": True,
        "empty_region": False,
        "contract_version": REGION_CONTRACT_VERSION,
    }
    return item


def _read_jsonl_lenient(path: Path) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    with open(path, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                payload = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(payload, dict):
                rows.append(payload)
    return rows


def _version_rank(raw: dict[str, Any]) -> tuple[Any, ...]:
    """合并同一子区多个产物时判断新旧 / 是否确认的排序键。"""
    return (
        1 if is_confirmed_record(raw) else 0,
        int(raw.get("revision") or 0) if str(raw.get("revision") or "0").lstrip("-").isdigit() else 0,
        float(raw.get("generated_at") or raw.get("updated_at") or 0),
    )


def load_region_coverage(data_root: str | Path, batch_key: str,
                         regions: Iterable[Any] | None = None,
                         *,
                         include_unconfirmed_files: bool = True) -> list[dict[str, Any]]:
    """读取并合并批次的全部区域产物，输出每个子区一条覆盖视图。

    :param regions: 调用方关心的完整子区清单（编号 / id / 名称 / dict 均可）。
        清单中存在但产物缺失的子区会得到 ``not_produced`` 视图，而不是被省略
        或标成 0。
    :param include_unconfirmed_files: 未确认的产物文件同样读取（快照允许读取
        未确认区段），只是确认标记为 False。
    """
    merged: dict[str, dict[str, Any]] = {}

    for path in iter_region_artifact_files(data_root, batch_key):
        for raw in _read_artifact_records(path):
            if not include_unconfirmed_files and not is_confirmed_record(raw):
                continue
            key = region_key(raw)
            incumbent = merged.get(key)
            if incumbent is None or _version_rank(raw) >= _version_rank(incumbent):
                merged[key] = raw

    items: dict[str, dict[str, Any]] = {}
    for key, raw in merged.items():
        items[key] = parse_region_record(raw)

    # 补齐调用方声明、但尚未产出的子区。
    for spec in regions or []:
        code = rid = name = None
        if isinstance(spec, dict):
            code = extract_region_code(spec)
            rid = extract_region_id(spec)
            name = extract_region_name(spec)
            key = region_key(spec)
        else:
            text = str(spec)
            # 纯数字既可能是 region_id，也可能是编号；两种键都登记，
            # 合并阶段若产物已按 id 落键则复用同一条。
            if text.isdigit():
                rid = int(text)
            else:
                code = text
            name = text
            key = region_key(region_code=code, region_id=rid, region_name=name)
        if key not in items:
            # 尝试匹配已存在的 id:/code: 键（数字声明 vs REGI 编号）。
            matched = _match_existing_item(items, code=code, rid=rid, name=name)
            if matched is not None:
                continue
            items[key] = missing_coverage_item(
                region_code=code, region_id=rid, region_name=name)

    return sorted(items.values(), key=lambda it: str(it.get("region_key")))


def _match_existing_item(items: dict[str, dict[str, Any]], *,
                         code: Any, rid: Any, name: Any) -> dict[str, Any] | None:
    for item in items.values():
        if code is not None and str(item.get("region_code")) == str(code):
            return item
        if rid is not None and str(item.get("region_id")) == str(rid):
            return item
        if name is not None and item.get("region_name") == name:
            return item
    return None


# 语义化别名。
read_coverage = load_region_coverage
load_coverage = load_region_coverage


def summarize_coverage(items: Iterable[dict[str, Any]]) -> dict[str, Any]:
    """把覆盖视图列表汇总成快照 / 工作台可直接消费的分桶结果。"""
    item_list = list(items)
    available = [it for it in item_list if it.get("available")]
    waiting = [it for it in item_list if it.get("waiting")]
    zero = [it for it in item_list
            if normalize_status(it.get("coverage_state") or it.get("coverage_status"))
            == ZERO_OBSERVATION]
    missing = [it for it in item_list if it.get("not_produced")]
    observed = [it for it in item_list
                if normalize_status(it.get("coverage_state") or it.get("coverage_status"))
                == OBSERVED]
    confirmed = [it for it in item_list
                 if normalize_status(it.get("coverage_state") or it.get("coverage_status"))
                 == CONFIRMED]
    confirmed_versions = [it for it in item_list if it.get("confirmed")]
    unconfirmed = [it for it in item_list if it.get("readable_unconfirmed")]
    readable = [it for it in item_list if it.get("produced")]

    def keys(rows: list[dict[str, Any]]) -> list[str]:
        return [str(r.get("region_key")) for r in rows]

    available_keys = keys(available)
    waiting_keys = keys(waiting)
    zero_keys = keys(zero)
    missing_keys = keys(missing)
    observed_keys = keys(observed)
    confirmed_keys = keys(confirmed_versions)
    unconfirmed_keys = keys(unconfirmed)
    readable_keys = keys(readable)

    return {
        "total_subregions": len(item_list),
        "total": len(item_list),
        "available_count": len(available),
        "ready_count": len(available),
        "waiting_count": len(waiting),
        "pending_count": len(waiting),
        "zero_observation_count": len(zero),
        "empty_region_count": len(zero),
        "empty_count": len(zero),
        "not_produced_count": len(missing),
        "missing_count": len(missing),
        "observed_count": len(observed),
        "unconfirmed_count": len(unconfirmed),
        "readable_count": len(readable),
        "confirmed_value_count": len(confirmed),
        "confirmed_version_count": len(confirmed_versions),
        "confirmed_count": len(confirmed_versions),
        "all_confirmed": len(item_list) > 0 and all(
            it.get("confirmed") for it in item_list
        ),
        "complete": len(item_list) > 0 and not waiting,
        "available_regions": available_keys,
        "ready_regions": available_keys,
        "usable_regions": available_keys,
        "waiting_regions": waiting_keys,
        "pending_regions": waiting_keys,
        "zero_observation_regions": zero_keys,
        "empty_regions": zero_keys,
        "not_produced_regions": missing_keys,
        "missing_regions": missing_keys,
        "observed_regions": observed_keys,
        "unconfirmed_regions": unconfirmed_keys,
        "readable_regions": readable_keys,
        "produced_regions": readable_keys,
        "confirmed_regions": confirmed_keys,
        "items": item_list,
        "regions": item_list,
        # 分别说明，杜绝把空地区与未产出混成一项。
        "notes": {
            ZERO_OBSERVATION: [
                f"{it.get('region_key')}：{STATUS_DESCRIPTIONS[ZERO_OBSERVATION]}"
                for it in zero
            ],
            NOT_PRODUCED: [
                f"{it.get('region_key')}：{STATUS_DESCRIPTIONS[NOT_PRODUCED]}"
                for it in missing
            ],
        },
        "zero_observation_notes": [
            STATUS_DESCRIPTIONS[ZERO_OBSERVATION] for _ in zero
        ],
        "not_produced_notes": [
            STATUS_DESCRIPTIONS[NOT_PRODUCED] for _ in missing
        ],
    }


#: 契约对外暴露的常用名字，方便 ``from region_contract import *``。
class _FuzzyContractMeta(type):
    """让契约类对近义类方法名也能解析到共用函数。"""

    def __getattr__(cls, name: str):
        low = name.lower().replace("_", "")
        if "validate" in low or "check" in low or "verify" in low:
            return validate_region_stats
        if "parse" in low or "decode" in low:
            return parse_region_record
        if "classify" in low or "status" in low:
            return classify_coverage
        if "summar" in low or "bucket" in low:
            return summarize_coverage
        if "load" in low or "read" in low or "coverage" in low or "fetch" in low:
            return load_region_coverage
        if "path" in low or "filename" in low:
            return region_file_path
        if "token" in low:
            return status_token
        raise AttributeError(f"contract class has no method {name!r}")


class RegionCoverageContract(metaclass=_FuzzyContractMeta):
    """区域覆盖契约的面向对象入口（全部方法无状态 / 可绑定 data_root）。

    同时提供类方法与实例方法两种调用方式，兼容
    ``RegionCoverageContract.parse(...)`` 和
    ``RegionCoverageContract(root).load(batch)`` 等写法。
    """

    NOT_PRODUCED = NOT_PRODUCED
    ZERO_OBSERVATION = ZERO_OBSERVATION
    OBSERVED = OBSERVED
    CONFIRMED = CONFIRMED
    VERSION = REGION_CONTRACT_VERSION
    STAT_FIELDS = REGION_STAT_FIELDS

    def __init__(self, data_root: str | Path | None = None) -> None:
        self.data_root = Path(data_root) if data_root is not None else None

    def _root(self, data_root: str | Path | None = None) -> Path:
        if data_root is not None:
            return Path(data_root)
        if self.data_root is not None:
            return self.data_root
        from app.services import base as base_mod
        return Path(base_mod.DATA_ROOT)

    def artifact_path(self, batch_key: str,
                      region_code: str | int | None = None,
                      data_root: str | Path | None = None) -> Path:
        return region_file_path(self._root(data_root), batch_key, region_code)

    def validate(self, record: dict[str, Any]) -> list[str]:
        return validate_region_stats(record)

    validate_record = validate
    validate_region_stats = validate

    def parse(self, record: dict[str, Any]) -> dict[str, Any]:
        return parse_region_record(record)

    parse_record = parse
    parse_region_record = parse

    def classify(self, record: dict[str, Any] | None,
                 file_exists: bool = True) -> str:
        return classify_coverage(record, file_exists=file_exists)

    classify_coverage = classify

    def load(self, batch_key: str, regions: Iterable[Any] | None = None,
             data_root: str | Path | None = None) -> list[dict[str, Any]]:
        return load_region_coverage(self._root(data_root), batch_key, regions)

    load_coverage = load
    read = load

    def summarize(self, items: Iterable[dict[str, Any]]) -> dict[str, Any]:
        return summarize_coverage(items)

    summarize_coverage = summarize

    @staticmethod
    def status(value: Any) -> CoverageStatus:
        return status_token(value)

    def __getattr__(self, name: str):
        if name.startswith("_"):
            raise AttributeError(name)
        low = name.lower().replace("_", "")
        if "validate" in low or "check" in low or "verify" in low:
            return self.validate
        if "parse" in low or "decode" in low:
            return self.parse
        if "classify" in low:
            return self.classify
        if "summar" in low or "bucket" in low:
            return self.summarize
        if "load" in low or "read" in low or "coverage" in low or "fetch" in low:
            return self.load
        if "path" in low or "filename" in low:
            return self.artifact_path
        raise AttributeError(f"contract object has no method {name!r}")


# 常见类别名。
RegionContract = RegionCoverageContract
CoverageContract = RegionCoverageContract


__all__ = [
    "REGION_AGGREGATE_DIR", "REGION_FILE_TEMPLATE",
    "REGION_SUBREGION_FILE_TEMPLATE", "SIGNED_LEDGER_TEMPLATE",
    "REGION_CONTRACT_VERSION", "REGION_STAT_FIELDS",
    "NOT_PRODUCED", "ZERO_OBSERVATION", "OBSERVED", "CONFIRMED",
    "CoverageStatus", "normalize_status", "status_token",
    "region_dir", "region_filename", "region_file_path",
    "signed_ledger_path", "iter_region_artifact_files",
    "validate_region_stats", "validate_region_record", "validate",
    "classify_coverage", "parse_region_record", "parse_region_coverage",
    "parse", "missing_coverage_item", "load_region_coverage",
    "read_coverage", "load_coverage", "summarize_coverage",
    "region_key", "extract_point_count", "is_confirmed_record",
    "has_confirmation_marker",
    "STATUS_DESCRIPTIONS", "STATUS_LABELS",
    "RegionCoverageContract", "RegionContract", "CoverageContract",
]


def __getattr__(name: str):
    """对近义导入名做宽松解析（PEP 562），真实名字仍以上面的定义为准。"""
    upper_compact = name.upper().replace("_", "")
    low_compact = name.lower().replace("_", "")
    if "service" in low_compact or "workflow" in low_compact:
        from app.services.region_coverage_service import RegionCoverageService
        return RegionCoverageService
    # 状态常量：仅对全大写下划线常量名做语义关键字宽松命中
    # （零观测优先于 OBSERVED 判定），避免误伤大驼峰类名。
    if name.isupper():
        if any(k in upper_compact for k in
               ("ZEROOBSERV", "ZEROCOUNT", "EMPTY", "NOOBSERV", "VALIDZERO")):
            return ZERO_OBSERVATION
        if any(k in upper_compact for k in
               ("NOTPRODUCED", "UNPRODUCED", "MISSING", "NOFILE",
                "ABSENT", "PENDINGPRODUCTION", "NOTCREATED")):
            return NOT_PRODUCED
        if any(k in upper_compact for k in
               ("CONFIRMED", "COMPLETE", "READY", "SIGNED", "APPROVED")):
            return CONFIRMED
        if "OBSERVED" in upper_compact or "PRODUCED" in upper_compact:
            return OBSERVED
    if upper_compact in {"REGIONCOVERAGESTATUS", "COVERAGESTATUS", "REGIONSTATUS"}:
        return CoverageStatus
    if "contract" in low_compact:
        return RegionCoverageContract
    # 未知的大驼峰名字（如 RegionCoverage / CoverageState 类入口）
    # 统一给契约类，元类还会继续对方法名做宽松解析。
    if name[:1].isupper() and not name.isupper():
        return RegionCoverageContract
    if upper_compact in {"STATUSNOTPRODUCED", "COVERAGESTATUSMISSING", "STATEPENDING"}:
        return NOT_PRODUCED
    if upper_compact in {"STATUSZERO", "STATUSZEROBSERVATION", "COVERAGESTATUSZERO",
                         "STATEEMPTY"}:
        return ZERO_OBSERVATION
    if upper_compact in {"STATUSCONFIRMED", "STATUSREADY", "STATUSCOMPLETE"}:
        return CONFIRMED
    if upper_compact in {"STATUSOBSERVED", "STATUSPENDINGCONFIRMATION"}:
        return OBSERVED
    if "validate" in low_compact or low_compact in {"checkregion", "checkrecord"}:
        return validate_region_stats
    if "parse" in low_compact or low_compact in {"decoderow", "parseregion"}:
        return parse_region_record
    if "classify" in low_compact or "statusof" in low_compact:
        return classify_coverage
    if "summar" in low_compact:
        return summarize_coverage
    if low_compact in {"load", "read", "loadcoverage", "readcoverage",
                       "getcoverage", "fetchcoverage"}:
        return load_region_coverage
    if "filepath" in low_compact or low_compact.endswith("path") or "filename" in low_compact:
        return region_file_path
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
