# =============================================================================
# RocketRide Engine
# =============================================================================
# MIT License
# Copyright (c) 2026 Aparavi Software AG
# =============================================================================

"""Regression tests for the hierarchical manager's result synthesis.

A Manager wired to three subagents was returning a single subagent's raw,
unedited task output as its final answer -- identifiable by that subagent's
own persona leaking through -- instead of a synthesis of all three. Reordering
the wired subagents surfaced a *different* subagent's raw output, with no
correlation to position.

Root cause: ``CrewManager._run``'s result-extraction step walked
``result.tasks_output`` (one raw entry per delegate Task) and returned the
first non-empty, ReAct-stripped candidate found scanning *backwards* -- i.e.
whichever single delegate's output happened to survive stripping last in
iteration order. Nothing anywhere combined multiple ``tasks_output`` entries,
and no manager-authored synthesis step existed to extract instead.

These tests exercise ``_synthesize_delegate_findings`` -- the extracted method
that now performs that synthesis pass -- directly, in isolation from CrewAI's
real hierarchical delegation loop. Scripting that loop (the manager's LLM
would have to emit precisely-formatted ReAct delegation actions for CrewAI to
actually invoke ``DelegateWorkTool``) is exactly what ``test_manager_tool_scoping.py``
already opted out of for its own scope; same trade-off applied here.

Import setup mirrors ``test_llm_contract.py``: ``crewai_manager.manager`` pulls
``rocketlib``, ``crewai`` and several ``ai.common`` submodules that need
pywin32/the compiled engine to import for real, so this stubs those seams
around the import. The stubs are scoped and torn down afterwards so a full
``builder nodes:test`` run, where the real modules are present, is unaffected.
"""

from __future__ import annotations

import importlib
import sys
import types
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any, List
from unittest.mock import MagicMock

import pytest

# Point at nodes/src/nodes, not nodes/src: importing through the `nodes` package
# would execute nodes/src/nodes/__init__.py, which pulls the engine-only `depends`.
_NODES_DIR = Path(__file__).resolve().parents[2] / 'src' / 'nodes'
if str(_NODES_DIR) not in sys.path:
    sys.path.insert(0, str(_NODES_DIR))

_STUB_MODULE_NAMES = (
    'rocketlib',
    'crewai',
    'ai',
    'ai.common',
    'ai.common.agent',
    'ai.common.agent._internal',
    'ai.common.agent._internal.host',
    'ai.common.agent.types',
    'ai.common.schema',
    'ai.common.utils',
    'ai.common.config',
)


def _build_stubs() -> dict:
    mod_rocketlib = types.ModuleType('rocketlib')
    mod_rocketlib.ToolDescriptor = dict
    mod_rocketlib.debug = lambda *a, **k: None
    # Package __init__.py also pulls in IInstance.py / IGlobal.py (sibling
    # node-facing classes, unrelated to _synthesize_delegate_findings), which
    # need these additional rocketlib/ai.common seams to import.
    mod_rocketlib.IInstanceBase = object
    mod_rocketlib.IGlobalBase = object
    mod_rocketlib.OPEN_MODE = types.SimpleNamespace(CONFIG='CONFIG')
    mod_rocketlib.tool_function = lambda **kwargs: lambda fn: fn

    # No `crewai.crew` / `crewai.tools` submodules: the import-time compatibility
    # patches in crewai_base wrap their imports in try/except and no-op without them.
    mod_crewai = types.ModuleType('crewai')
    mod_crewai.BaseLLM = type('_StubBaseLLM', (), {'__init__': lambda self, **k: None})

    mod_ai = types.ModuleType('ai')
    mod_ai_common = types.ModuleType('ai.common')

    mod_agent = types.ModuleType('ai.common.agent')

    class AgentBase:
        pass

    class AgentContext:
        pass

    mod_agent.AgentBase = AgentBase
    mod_agent.AgentContext = AgentContext

    mod_agent_internal = types.ModuleType('ai.common.agent._internal')
    mod_agent_internal_host = types.ModuleType('ai.common.agent._internal.host')
    mod_agent_internal_host.AgentHostServices = object

    mod_agent_types = types.ModuleType('ai.common.agent.types')
    mod_agent_types.AgentRunResult = object
    mod_agent_types.AGENT_TOOL_INPUT_SCHEMA = {}
    mod_agent_types.AGENT_TOOL_OUTPUT_SCHEMA = {}

    mod_schema = types.ModuleType('ai.common.schema')
    mod_schema.Question = object

    mod_utils = types.ModuleType('ai.common.utils')
    mod_utils.safe_str = lambda v: '' if v is None else str(v)

    mod_config = types.ModuleType('ai.common.config')
    mod_config.Config = type('Config', (), {'getNodeConfig': staticmethod(lambda *a, **k: {})})

    return {
        'rocketlib': mod_rocketlib,
        'crewai': mod_crewai,
        'ai': mod_ai,
        'ai.common': mod_ai_common,
        'ai.common.agent': mod_agent,
        'ai.common.agent._internal': mod_agent_internal,
        'ai.common.agent._internal.host': mod_agent_internal_host,
        'ai.common.agent.types': mod_agent_types,
        'ai.common.schema': mod_schema,
        'ai.common.utils': mod_utils,
        'ai.common.config': mod_config,
    }


@contextmanager
def _scoped_stubs() -> Iterator[None]:
    original = {name: sys.modules.get(name) for name in _STUB_MODULE_NAMES}
    sys.modules.update(_build_stubs())
    try:
        yield
    finally:
        for name, module in original.items():
            if module is None:
                sys.modules.pop(name, None)
            else:
                sys.modules[name] = module


with _scoped_stubs():
    manager_module = importlib.import_module('agent_crewai.crewai_manager.manager')

CrewManager = manager_module.CrewManager


@pytest.fixture(autouse=True)
def _stubbed_crewai():
    with _scoped_stubs():
        yield


def _make_manager(call_llm_return: str = '') -> Any:
    """A CrewManager instance with __init__ bypassed and call_llm stubbed.

    __init__ resolves node config via the engine's Config/iGlobal seams,
    which this test has no need to stand up -- _synthesize_delegate_findings
    only touches `self.call_llm`.
    """
    manager = object.__new__(CrewManager)
    manager.call_llm = MagicMock(return_value=call_llm_return)
    return manager


def _task_out(raw: str) -> Any:
    return MagicMock(raw=raw)


def _sub_task(role: str) -> Any:
    task = MagicMock()
    task.agent.role = role
    return task


class TestSynthesizeDelegateFindings:
    def test_combines_all_delegate_findings_not_just_one(self):
        """The exact bug: with 3 subagents, all 3 must feed the synthesis call."""
        manager = _make_manager(call_llm_return='The synthesized answer.')
        sub_tasks = [_sub_task('Research Subagent'), _sub_task('Writer Subagent'), _sub_task('Critic Subagent')]
        tasks_out = [
            _task_out('Final Answer: research finding'),
            _task_out('Final Answer: draft finding'),
            _task_out('Final Answer: critique finding'),
        ]

        result = manager._synthesize_delegate_findings(
            context=MagicMock(),
            tasks_out=tasks_out,
            sub_tasks=sub_tasks,
            manager_backstory='You are the manager.',
            manager_goal='Coordinate the team.',
        )

        assert result == 'The synthesized answer.'
        prompt_sent = manager.call_llm.call_args.args[1]
        assert 'research finding' in prompt_sent
        assert 'draft finding' in prompt_sent
        assert 'critique finding' in prompt_sent
        assert 'Research Subagent' in prompt_sent
        assert 'Writer Subagent' in prompt_sent
        assert 'Critic Subagent' in prompt_sent

    def test_result_is_the_synthesis_not_any_single_finding_verbatim(self):
        """Guards the reported symptom directly: a subagent's raw text must
        never be byte-identical to the returned final answer.
        """
        manager = _make_manager(call_llm_return='Combined: all three checks passed.')
        sub_tasks = [_sub_task('A'), _sub_task('B')]
        tasks_out = [_task_out('Final Answer: A said X'), _task_out('Final Answer: B said Y')]

        result = manager._synthesize_delegate_findings(
            context=MagicMock(),
            tasks_out=tasks_out,
            sub_tasks=sub_tasks,
            manager_backstory='backstory',
            manager_goal='goal',
        )

        assert result not in ('A said X', 'B said Y')

    def test_order_of_wired_subagents_does_not_change_which_findings_are_used(self):
        """Reordering inputs must not drop a finding -- only relabel it."""
        manager_forward = _make_manager(call_llm_return='synthesized')
        manager_reversed = _make_manager(call_llm_return='synthesized')

        sub_tasks = [_sub_task('First'), _sub_task('Second')]
        tasks_out = [_task_out('Final Answer: one'), _task_out('Final Answer: two')]

        manager_forward._synthesize_delegate_findings(
            context=MagicMock(), tasks_out=tasks_out, sub_tasks=sub_tasks, manager_backstory='b', manager_goal='g'
        )
        manager_reversed._synthesize_delegate_findings(
            context=MagicMock(),
            tasks_out=list(reversed(tasks_out)),
            sub_tasks=list(reversed(sub_tasks)),
            manager_backstory='b',
            manager_goal='g',
        )

        forward_prompt = manager_forward.call_llm.call_args.args[1]
        reversed_prompt = manager_reversed.call_llm.call_args.args[1]
        for prompt in (forward_prompt, reversed_prompt):
            assert 'one' in prompt
            assert 'two' in prompt

    def test_skips_delegates_with_empty_or_unstrippable_output(self):
        manager = _make_manager(call_llm_return='synthesized')
        sub_tasks = [_sub_task('Empty'), _sub_task('Real')]
        tasks_out = [_task_out(''), _task_out('Final Answer: the only real finding')]

        result = manager._synthesize_delegate_findings(
            context=MagicMock(), tasks_out=tasks_out, sub_tasks=sub_tasks, manager_backstory='b', manager_goal='g'
        )

        assert result == 'synthesized'
        prompt_sent = manager.call_llm.call_args.args[1]
        assert 'the only real finding' in prompt_sent

    def test_no_usable_delegate_output_returns_empty_without_calling_llm(self):
        """Lets the caller fall back to result.raw instead of synthesizing nothing."""
        manager = _make_manager(call_llm_return='should not be used')
        sub_tasks = [_sub_task('A')]
        tasks_out = [_task_out('')]

        result = manager._synthesize_delegate_findings(
            context=MagicMock(), tasks_out=tasks_out, sub_tasks=sub_tasks, manager_backstory='b', manager_goal='g'
        )

        assert result == ''
        manager.call_llm.assert_not_called()

    def test_mismatched_lengths_do_not_crash(self):
        """More task outputs than known sub_tasks (or vice versa) must not raise."""
        manager = _make_manager(call_llm_return='synthesized')
        sub_tasks: List[Any] = [_sub_task('Only one known')]
        tasks_out = [_task_out('Final Answer: one'), _task_out('Final Answer: two')]

        result = manager._synthesize_delegate_findings(
            context=MagicMock(), tasks_out=tasks_out, sub_tasks=sub_tasks, manager_backstory='b', manager_goal='g'
        )

        assert result == 'synthesized'
