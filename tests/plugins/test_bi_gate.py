"""bi-gate 门禁的行为测试。

每条测试对应一条门禁规则的行为，而不是实现细节 —— 规则改了这些测试应该跟着改，
但重构 evaluate 的内部顺序不应该让它们变红。
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

_MODNAME = "bi_gate_rules_under_test"


def _load_rules():
    """插件目录名带连字符，不能直接 import —— 按仓库既有做法从文件载入。

    exec 前必须先登记进 ``sys.modules``：``@dataclass`` 会回查
    ``sys.modules[cls.__module__]``，模块不在表里就会炸。
    """
    path = Path(__file__).resolve().parents[2] / "plugins" / "bi-gate" / "rules.py"
    spec = importlib.util.spec_from_file_location(_MODNAME, path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[_MODNAME] = mod
    spec.loader.exec_module(mod)
    return mod


_rules = _load_rules()

PASSED = _rules.PASSED
REJECT_BAD_PARAM = _rules.REJECT_BAD_PARAM
REJECT_NO_TIME_WINDOW = _rules.REJECT_NO_TIME_WINDOW
REJECT_SCAN = _rules.REJECT_SCAN
REJECT_UNKNOWN_METRIC = _rules.REJECT_UNKNOWN_METRIC
GATE_SOURCE = _rules.GATE_SOURCE
MetricRegistry = _rules.MetricRegistry
MetricSpec = _rules.MetricSpec
evaluate = _rules.evaluate


@pytest.fixture
def registry() -> MetricRegistry:
    return MetricRegistry(
        [
            MetricSpec(
                name="dau",
                dimensions=frozenset({"market", "channel"}),
                max_scan_rows=1_000_000,
            ),
            MetricSpec(
                name="open_interest",
                dimensions=frozenset({"symbol"}),
                requires_time_window=False,
            ),
        ]
    )


WINDOW = {"start": "2026-08-01", "end": "2026-08-21"}


class TestMetricRegistered:
    def test_registered_metric_passes(self, registry):
        assert evaluate({"metric": "dau", "time_window": WINDOW}, registry).code == PASSED

    def test_unregistered_metric_is_blocked(self, registry):
        v = evaluate({"metric": "revenue_v2", "time_window": WINDOW}, registry)
        assert v.code == REJECT_UNKNOWN_METRIC
        assert v.blocked

    def test_missing_metric_is_blocked(self, registry):
        assert evaluate({"time_window": WINDOW}, registry).code == REJECT_UNKNOWN_METRIC

    def test_reason_lists_available_metrics(self, registry):
        # 拒绝理由要能让模型自己纠正，所以得把可用指标报出来
        v = evaluate({"metric": "nope", "time_window": WINDOW}, registry)
        assert "dau" in v.reason and "open_interest" in v.reason

    def test_empty_registry_blocks_everything(self):
        # fail-closed：注册表载入失败时门禁停摆，不放行
        v = evaluate({"metric": "dau", "time_window": WINDOW}, MetricRegistry([]))
        assert v.code == REJECT_UNKNOWN_METRIC


class TestDimensions:
    def test_declared_dimension_passes(self, registry):
        args = {"metric": "dau", "dimensions": ["market"], "time_window": WINDOW}
        assert evaluate(args, registry).code == PASSED

    def test_undeclared_dimension_is_blocked(self, registry):
        args = {"metric": "dau", "dimensions": ["market", "device"], "time_window": WINDOW}
        v = evaluate(args, registry)
        assert v.code == REJECT_BAD_PARAM
        assert "device" in v.reason

    def test_omitted_dimensions_pass(self, registry):
        assert evaluate({"metric": "dau", "time_window": WINDOW}, registry).code == PASSED

    def test_non_list_dimensions_are_blocked(self, registry):
        args = {"metric": "dau", "dimensions": 42, "time_window": WINDOW}
        assert evaluate(args, registry).code == REJECT_BAD_PARAM


class TestTimeWindow:
    def test_absolute_window_passes(self, registry):
        assert evaluate({"metric": "dau", "time_window": WINDOW}, registry).code == PASSED

    def test_missing_window_is_blocked(self, registry):
        assert evaluate({"metric": "dau"}, registry).code == REJECT_NO_TIME_WINDOW

    @pytest.mark.parametrize("bad", ["最近七天", "last_7d", "2026/08/01", ""])
    def test_relative_or_malformed_window_is_blocked(self, registry, bad):
        # 相对时间必须在调用前解析成绝对区间，否则评估集无法回归
        args = {"metric": "dau", "time_window": {"start": bad, "end": "2026-08-21"}}
        assert evaluate(args, registry).code == REJECT_NO_TIME_WINDOW

    def test_reversed_window_is_blocked(self, registry):
        args = {"metric": "dau", "time_window": {"start": "2026-08-21", "end": "2026-08-01"}}
        assert evaluate(args, registry).code == REJECT_NO_TIME_WINDOW

    def test_stock_metric_needs_no_window(self, registry):
        # 存量类指标（当前持仓）没有时间窗，不该被拦
        assert evaluate({"metric": "open_interest"}, registry).code == PASSED


class TestScanBudget:
    def test_within_budget_passes(self, registry):
        args = {"metric": "dau", "time_window": WINDOW}
        assert evaluate(args, registry, estimated_rows=999_999).code == PASSED

    def test_over_budget_is_blocked(self, registry):
        args = {"metric": "dau", "time_window": WINDOW}
        v = evaluate(args, registry, estimated_rows=1_000_001)
        assert v.code == REJECT_SCAN
        assert v.detail["limit"] == 1_000_000

    def test_missing_estimate_passes(self, registry):
        # 预检拿不到预估值时不应让业务不可用
        args = {"metric": "dau", "time_window": WINDOW}
        assert evaluate(args, registry, estimated_rows=None).code == PASSED

    def test_metric_without_limit_passes(self, registry):
        args = {"metric": "open_interest"}
        assert evaluate(args, registry, estimated_rows=10**9).code == PASSED


class TestRejectionReason:
    """拒绝理由必须写明拦截来源 —— 否则模型会自行编造归因。

    依据：《评估与 Reward v0.1》§2.4，两次实测模型都把 harness 的拦截
    说成了远端服务的行为。
    """

    @pytest.mark.parametrize(
        "args",
        [
            {"metric": "nope", "time_window": WINDOW},
            {"metric": "dau"},
            {"metric": "dau", "dimensions": ["device"], "time_window": WINDOW},
        ],
    )
    def test_every_rejection_names_the_gate(self, registry, args):
        v = evaluate(args, registry)
        assert v.blocked
        assert v.reason.startswith(GATE_SOURCE)

    def test_pass_carries_no_reason(self, registry):
        assert evaluate({"metric": "dau", "time_window": WINDOW}, registry).reason is None
