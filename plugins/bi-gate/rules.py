"""bi-gate 的判定规则与拒因分类。

规则与执行分开放：这里只有纯函数和数据，没有 I/O、没有 hook 依赖，
所以每条规则都能单独跑单测，也能被门禁之外的地方复用（比如离线跑一遍
历史轨迹，看新规则会拦掉多少历史调用）。

拒因编号是对外契约的一部分——审计表、告警、以及给模型看的拒绝理由都引用它，
所以只增不改。
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, Mapping, Optional, Sequence


# ---------------------------------------------------------------------------
# 拒因分类
# ---------------------------------------------------------------------------

# 与《可观测与自进化实施方案》的 gate_result 取值对齐，便于两边的审计表合并统计。
REJECT_UNKNOWN_METRIC = "rejected_unknown_metric"
REJECT_BAD_PARAM = "rejected_bad_param"
REJECT_NO_TIME_WINDOW = "rejected_no_time_window"
REJECT_SCAN = "rejected_scan"
PASSED = "passed"

#: 拦截来源。必须出现在给模型看的拒绝理由里——否则模型会自己编一个归因。
#: 实测依据见《评估与 Reward v0.1》§2.4：两次实验模型都把 harness 的拦截
#: 说成了远端服务的行为。
GATE_SOURCE = "BI 门禁（bi-gate 插件，在调用发出前拦截）"


@dataclass(frozen=True)
class Verdict:
    """一次判定的结果。`code` 为 PASSED 时表示放行。"""

    code: str
    #: 给模型看的话。放行时为 None。
    reason: Optional[str] = None
    #: 给审计表的结构化补充，不进模型上下文。
    detail: Optional[Mapping[str, Any]] = None

    @property
    def blocked(self) -> bool:
        return self.code != PASSED


def _deny(code: str, message: str, **detail: Any) -> Verdict:
    """构造拒绝，统一带上拦截来源。"""
    return Verdict(code=code, reason=f"{GATE_SOURCE}：{message}", detail=detail or None)


ALLOW = Verdict(code=PASSED)


# ---------------------------------------------------------------------------
# 指标注册表
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class MetricSpec:
    """受控事实层里一个指标的门禁相关部分。

    完整的指标元模型（口径描述、责任人、新鲜度）在指标层，不在这里；
    门禁只需要够做确定性校验的那几项。
    """

    name: str
    #: 允许的维度名。请求里出现表外维度即拒——避免模型臆造维度。
    dimensions: frozenset[str]
    #: 是否必须带时间窗。绝大多数指标都要，留开关是因为存量类指标（如"当前持仓"）没有时间窗。
    requires_time_window: bool = True
    #: 单次查询允许的最大扫描行数；None 表示不限（仅用于已知极小的维表）。
    max_scan_rows: Optional[int] = None


class MetricRegistry:
    """指标注册表。首批只装 10–12 个核心指标，范围可缩、准出标准不降。"""

    def __init__(self, specs: Sequence[MetricSpec]) -> None:
        self._by_name = {s.name: s for s in specs}

    def get(self, name: str) -> Optional[MetricSpec]:
        return self._by_name.get(name)

    @property
    def names(self) -> list[str]:
        return sorted(self._by_name)


# ---------------------------------------------------------------------------
# 单条规则
# ---------------------------------------------------------------------------

def check_metric_registered(metric: Any, registry: MetricRegistry) -> Verdict:
    """指标必须在注册表里。

    这条同时挡住两件事：模型臆造指标名，以及有人绕过指标层直接点名底表。
    """
    if not isinstance(metric, str) or not metric.strip():
        return _deny(REJECT_UNKNOWN_METRIC, "缺少 metric 参数。")
    spec = registry.get(metric)
    if spec is None:
        return _deny(
            REJECT_UNKNOWN_METRIC,
            f"指标 {metric!r} 不在受控事实层。当前可用：{'、'.join(registry.names) or '（注册表为空）'}。"
            "如果这个口径确实需要，走指标层登记流程，不要用 run_sql 绕过。",
            metric=metric,
        )
    return ALLOW


def check_dimensions(dimensions: Any, spec: MetricSpec) -> Verdict:
    """维度必须是该指标声明过的。"""
    if dimensions is None:
        return ALLOW
    if isinstance(dimensions, str):
        dimensions = [dimensions]
    if not isinstance(dimensions, (list, tuple)):
        return _deny(REJECT_BAD_PARAM, "dimensions 必须是字符串数组。", got=type(dimensions).__name__)
    unknown = [d for d in dimensions if d not in spec.dimensions]
    if unknown:
        return _deny(
            REJECT_BAD_PARAM,
            f"指标 {spec.name} 不支持维度 {'、'.join(map(str, unknown))}。"
            f"它支持的是：{'、'.join(sorted(spec.dimensions)) or '（无维度）'}。",
            metric=spec.name,
            unknown_dimensions=list(unknown),
        )
    return ALLOW


#: 只接受显式时间窗。相对表述（"最近"、"上个月"）必须在调用前解析成绝对区间，
#: 否则同一个问题在不同时刻问会得到不同的数，评估集就没法回归。
_DATE = re.compile(r"^\d{4}-\d{2}-\d{2}([T ]\d{2}:\d{2}(:\d{2})?)?$")


def check_time_window(time_window: Any, spec: MetricSpec) -> Verdict:
    """时间窗必填且必须是绝对区间。"""
    if not spec.requires_time_window:
        return ALLOW
    if not isinstance(time_window, Mapping):
        return _deny(
            REJECT_NO_TIME_WINDOW,
            f"指标 {spec.name} 必须带时间窗，形如 "
            '{"start": "2026-08-01", "end": "2026-08-21"}。无界查询一律不放行。',
            metric=spec.name,
        )
    start, end = time_window.get("start"), time_window.get("end")
    for label, value in (("start", start), ("end", end)):
        if not isinstance(value, str) or not _DATE.match(value):
            return _deny(
                REJECT_NO_TIME_WINDOW,
                f"时间窗的 {label} 必须是绝对时间（YYYY-MM-DD 或 YYYY-MM-DD HH:MM），"
                f"当前是 {value!r}。相对表述请先解析成具体日期再调用。",
                metric=spec.name,
                field=label,
            )
    if start > end:
        return _deny(
            REJECT_NO_TIME_WINDOW,
            f"时间窗起止颠倒：start={start} 晚于 end={end}。",
            metric=spec.name,
        )
    return ALLOW


def check_scan_budget(estimated_rows: Optional[int], spec: MetricSpec) -> Verdict:
    """扫描量预检。

    `estimated_rows` 由调用方在派发前用 EXPLAIN 拿到；拿不到时传 None，
    此处放行——预检失败不应该变成业务不可用，但要在审计里留痕（由执行层记录）。
    """
    if spec.max_scan_rows is None or estimated_rows is None:
        return ALLOW
    if estimated_rows > spec.max_scan_rows:
        return _deny(
            REJECT_SCAN,
            f"预估扫描 {estimated_rows:,} 行，超过指标 {spec.name} 的上限 "
            f"{spec.max_scan_rows:,} 行。请缩小时间窗或减少维度后重试。",
            metric=spec.name,
            estimated_rows=estimated_rows,
            limit=spec.max_scan_rows,
        )
    return ALLOW


# ---------------------------------------------------------------------------
# 组合
# ---------------------------------------------------------------------------

def evaluate(
    args: Mapping[str, Any],
    registry: MetricRegistry,
    estimated_rows: Optional[int] = None,
) -> Verdict:
    """跑完整条门禁，返回第一条不通过的判定。

    顺序是有意的：先确认指标存在（后面几条都依赖 spec），再校验参数，
    最后才是代价最高的扫描量预检。

    :param args: query_metric 的调用参数。
    :param registry: 当前生效的指标注册表。
    :param estimated_rows: EXPLAIN 预估行数，没有则传 None。
    :returns: 放行为 ``ALLOW``，否则是带拒因与理由的 :class:`Verdict`。
    """
    metric = args.get("metric")
    verdict = check_metric_registered(metric, registry)
    if verdict.blocked:
        return verdict

    spec = registry.get(metric)
    assert spec is not None  # check_metric_registered 已经保证

    for verdict in (
        check_dimensions(args.get("dimensions"), spec),
        check_time_window(args.get("time_window"), spec),
        check_scan_budget(estimated_rows, spec),
    ):
        if verdict.blocked:
            return verdict
    return ALLOW
