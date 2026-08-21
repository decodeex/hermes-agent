"""bi-gate 端到端冒烟：验证 hook 在真实派发路径里确实拦得住。

单测只证明判定函数算得对，证明不了「Hermes 真的会调用它、block 真的会让
工具体不执行」。这两件事只能驱动真实的 ``handle_function_call`` 来验。

硬证据是 ``_CALLS`` 计数器：被拦的调用它不能加一。
"""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

import pytest


REPO = Path(__file__).resolve().parents[2]
PLUGIN_DIR = REPO / "plugins" / "bi-gate"

#: 假 query_metric 的执行次数。工具体跑了才加一 —— 这是「有没有被真正拦住」的唯一硬证据。
_CALLS: list[dict] = []


def _load_plugin():
    """按仓库既有做法从文件载入连字符目录的插件。"""
    ns = "hermes_plugins"
    if ns not in sys.modules:
        import types

        mod = types.ModuleType(ns)
        mod.__path__ = []
        sys.modules[ns] = mod
    name = f"{ns}.bi_gate"
    spec = importlib.util.spec_from_file_location(
        name, PLUGIN_DIR / "__init__.py", submodule_search_locations=[str(PLUGIN_DIR)]
    )
    mod = importlib.util.module_from_spec(spec)
    mod.__package__ = name
    mod.__path__ = [str(PLUGIN_DIR)]
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture
def gate(tmp_path, monkeypatch):
    """载入 bi-gate，并指向一份只有一个指标的注册表。"""
    registry = tmp_path / "registry.json"
    registry.write_text(
        json.dumps(
            {
                "metrics": [
                    {
                        "name": "dau",
                        "dimensions": ["market"],
                        "requires_time_window": True,
                    }
                ]
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setenv("BI_GATE_REGISTRY", str(registry))
    plugin = _load_plugin()
    plugin.reload_registry()
    return plugin


@pytest.fixture
def dispatch(gate, monkeypatch):
    """把 bi-gate 的 hook 接进真实的 pre_tool_call 派发，并注册一个假 query_metric。

    返回可直接调用的 ``handle_function_call``。
    """
    _CALLS.clear()

    import model_tools
    from hermes_cli import plugins as plugins_mod

    # ── 让真实的 hook 派发只看到 bi-gate ──────────────────────────
    # 直接替换 invoke_hook，避免依赖 PluginManager 的发现流程（那会连带加载
    # 一堆可选插件）。派发路径本身仍是仓库自己的 _dispatch_pre_tool_call_hooks。
    def _invoke_hook(hook_name, **kwargs):
        if hook_name != "pre_tool_call":
            return []
        out = gate._on_pre_tool_call(**kwargs)
        return [out] if out is not None else []

    monkeypatch.setattr(plugins_mod, "invoke_hook", _invoke_hook)

    # ── 注册一个假的 query_metric ────────────────────────────────
    from tools import registry as tool_registry

    def _fake_query_metric(args, **_kwargs):
        # registry 以 handler(args, **kwargs) 调用，args 是参数字典
        _CALLS.append(args)
        return json.dumps({"rows": [{"dau": 12345}]})

    _register_tool(tool_registry, "query_metric", _fake_query_metric)
    return model_tools.handle_function_call


def _register_tool(tool_registry, name: str, handler) -> None:
    """把一个假工具塞进真实注册表（覆盖同名项，测试结束由 registry 自己承载）。"""
    tool_registry.registry.register(
        name=name,
        toolset="bi_gate_test",
        schema={
            "name": name,
            "description": "test double",
            "parameters": {"type": "object", "properties": {}},
        },
        handler=handler,
        override=True,
    )


GOOD = {"metric": "dau", "time_window": {"start": "2026-08-01", "end": "2026-08-21"}}


class TestBlockedCallsDoNotExecute:
    """被拦的调用，工具体一次都不能跑。"""

    @pytest.mark.parametrize(
        "args, expect_in_message",
        [
            ({"metric": "revenue_v2", "time_window": GOOD["time_window"]}, "不在受控事实层"),
            ({"metric": "dau"}, "必须带时间窗"),
            ({"metric": "dau", "time_window": {"start": "最近七天", "end": "2026-08-21"}}, "绝对时间"),
            (
                {"metric": "dau", "dimensions": ["device"], "time_window": GOOD["time_window"]},
                "不支持维度",
            ),
        ],
    )
    def test_body_never_runs(self, dispatch, args, expect_in_message):
        before = len(_CALLS)
        result = dispatch("query_metric", args)
        assert len(_CALLS) == before, "工具体被执行了 —— 门禁没拦住"
        assert expect_in_message in str(result)

    def test_block_message_names_the_gate(self, dispatch):
        result = dispatch("query_metric", {"metric": "nope", "time_window": GOOD["time_window"]})
        assert "bi-gate" in str(result), "拒绝理由必须写明拦截来源"


class TestAllowedCallsGoThrough:
    def test_valid_call_reaches_the_tool(self, dispatch):
        result = dispatch("query_metric", GOOD)
        assert len(_CALLS) == 1, "合法调用没能到达工具体"
        assert "12345" in str(result)

    def test_other_tools_are_untouched(self, dispatch, monkeypatch):
        """门禁只管 query_metric，别的工具一律不碰。"""
        from tools import registry as tool_registry

        seen = []
        _register_tool(
            tool_registry, "bi_gate_probe_tool", lambda args, **_kw: seen.append(args) or "ok"
        )
        dispatch("bi_gate_probe_tool", {"path": "/tmp/x"})
        assert len(seen) == 1


class TestFailureModes:
    """门禁自身出问题时的行为 —— 这些是真实故障，不是假想。"""

    def test_empty_registry_blocks_everything(self, dispatch, gate, monkeypatch, tmp_path):
        """注册表载入失败按空表处理，应当全拦（fail-closed）。"""
        monkeypatch.setenv("BI_GATE_REGISTRY", str(tmp_path / "does-not-exist.json"))
        gate.reload_registry()
        before = len(_CALLS)
        dispatch("query_metric", GOOD)
        assert len(_CALLS) == before, "注册表缺失时仍然放行了 —— 不是 fail-closed"

    def test_hook_exception_currently_fails_open(self, dispatch, gate, monkeypatch):
        """hook 抛异常时调用会被放行。

        这是 Hermes 的既有行为：model_tools 里 pre_tool_call 的派发包在
        ``except Exception`` 里，只记 debug 日志然后继续执行。也就是说
        **门禁插件自己崩了，门禁就静默消失**。

        这条测试把该行为钉住。如果哪天上游改成 fail-closed，它会变红，
        那时应当把断言反过来 —— 而不是默默接受。
        """

        def _boom(**_kwargs):
            raise RuntimeError("gate exploded")

        monkeypatch.setattr(gate, "_on_pre_tool_call", _boom)
        before = len(_CALLS)
        dispatch("query_metric", {"metric": "revenue_v2"})  # 本该被拦
        assert len(_CALLS) == before + 1, (
            "行为变了：hook 抛异常时调用没有被放行。若上游改成 fail-closed，"
            "这是好事，请更新本测试的断言。"
        )
