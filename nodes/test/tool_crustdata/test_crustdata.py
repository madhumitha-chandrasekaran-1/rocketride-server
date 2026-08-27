# =============================================================================
# RocketRide Engine
# =============================================================================
# MIT License
# Copyright (c) 2026 Aparavi Software AG
# =============================================================================

"""
Unit tests for tool_crustdata (no network, no engine runtime).

Bootstrap mirrors test_pipedrive.py: inject lightweight stubs for the engine
runtime modules ONLY if absent, import the module under test, then drop the
stubs so they never leak into a shared pytest session. `requests` is real —
only its `.post` call is mocked per test, so the retry/error-mapping logic in
`_request_with_retry` runs against real exception types.
"""

from __future__ import annotations

import sys
import types
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from unittest.mock import Mock, patch

import pytest
import requests

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / 'src' / 'nodes'))

_STUB_MODULE_NAMES = ('rocketlib', 'ai', 'ai.common', 'ai.common.config', 'ai.common.utils')


def _install_stubs() -> None:
    mod_rl = types.ModuleType('rocketlib')

    def mock_tool_function(*args, **kwargs):
        def decorator(fn):
            fn.__tool_meta__ = kwargs
            return fn

        return decorator

    mod_rl.tool_function = mock_tool_function

    class IInstanceBase:
        pass

    class IGlobalBase:
        pass

    mod_rl.IInstanceBase = IInstanceBase
    mod_rl.IGlobalBase = IGlobalBase
    mod_rl.OPEN_MODE = Mock()
    mod_rl.debug = Mock()
    mod_rl.warning = Mock()
    mod_rl.error = Mock()
    sys.modules['rocketlib'] = mod_rl

    sys.modules['ai'] = types.ModuleType('ai')
    sys.modules['ai.common'] = types.ModuleType('ai.common')

    mod_config = types.ModuleType('ai.common.config')

    class Config:
        pass

    mod_config.Config = Config
    sys.modules['ai.common.config'] = mod_config

    mod_utils = types.ModuleType('ai.common.utils')

    def normalize_tool_input(value, **kwargs):
        return value if isinstance(value, dict) else {}

    mod_utils.normalize_tool_input = normalize_tool_input
    sys.modules['ai.common.utils'] = mod_utils


@contextmanager
def _scoped_stubs() -> Iterator[None]:
    original = {name: sys.modules.get(name) for name in _STUB_MODULE_NAMES}
    _install_stubs()
    try:
        yield
    finally:
        for name, module in original.items():
            if module is None:
                sys.modules.pop(name, None)
            else:
                sys.modules[name] = module


with _scoped_stubs():
    from tool_crustdata.IInstance import (
        COMPANY_SEARCH_URL,
        CRUSTDATA_API_VERSION,
        PERSON_SEARCH_URL,
        IInstance,
        _crustdata_headers,
        _extract_records,
    )


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _resp(status=200, *, json_data=None):
    resp = Mock(spec=requests.Response)
    resp.status_code = status
    resp.ok = 200 <= status < 300
    if json_data is None:
        resp.json.side_effect = ValueError('no json')
    else:
        resp.json.return_value = json_data
    if not resp.ok:
        resp.raise_for_status.side_effect = requests.HTTPError(response=resp)
    else:
        resp.raise_for_status.side_effect = None
    return resp


def _instance(apikey='test-key', default_limit=10):
    """Build an IInstance without running the engine lifecycle."""
    inst = IInstance.__new__(IInstance)
    glob = Mock()
    glob.apikey = apikey
    glob.default_limit = default_limit
    inst.IGlobal = glob
    return inst


_A_FILTER = [{'filter_type': 'CURRENT_COMPANY', 'type': 'in', 'value': ['Acme Corp']}]


# ---------------------------------------------------------------------------
# _extract_records — the defensive response-envelope parsing
# ---------------------------------------------------------------------------


class TestExtractRecords:
    @pytest.mark.parametrize('key', ['results', 'data', 'companies', 'people', 'profiles'])
    def test_finds_the_record_list_under_any_known_key(self, key):
        body = {key: [{'name': 'Acme'}, {'name': 'Globex'}]}
        assert _extract_records(body) == [{'name': 'Acme'}, {'name': 'Globex'}]

    def test_accepts_a_bare_top_level_list(self):
        assert _extract_records([{'name': 'Acme'}]) == [{'name': 'Acme'}]

    def test_drops_non_dict_items_rather_than_raising(self):
        body = {'results': ['oops', None, 42, {'name': 'Acme'}]}
        assert _extract_records(body) == [{'name': 'Acme'}]

    def test_unrecognized_shape_returns_empty_not_an_error(self):
        assert _extract_records({'totally_unexpected_key': [{'name': 'Acme'}]}) == []
        assert _extract_records('not even a dict or list') == []
        assert _extract_records(None) == []

    def test_first_matching_key_wins(self):
        """Results is checked before data, per _RECORD_LIST_KEYS order."""
        body = {'data': [{'name': 'wrong'}], 'results': [{'name': 'right'}]}
        assert _extract_records(body) == [{'name': 'right'}]


# ---------------------------------------------------------------------------
# _crustdata_headers
# ---------------------------------------------------------------------------


def test_headers_carry_bearer_auth_and_the_pinned_api_version():
    headers = _crustdata_headers('sk-live-abc123')
    assert headers['authorization'] == 'Bearer sk-live-abc123'
    assert headers['x-api-version'] == CRUSTDATA_API_VERSION
    assert headers['content-type'] == 'application/json'


# ---------------------------------------------------------------------------
# company_search / person_search — request construction and error handling
# ---------------------------------------------------------------------------


class TestSearchValidation:
    def test_missing_filters_is_rejected_before_any_request(self):
        inst = _instance()
        out = inst.company_search({})
        assert out['success'] is False
        assert out['results'] == []
        assert 'filters' in out['error']

    def test_empty_filters_list_is_rejected(self):
        inst = _instance()
        out = inst.person_search({'filters': []})
        assert out['success'] is False
        assert 'filters' in out['error']

    def test_non_list_filters_is_rejected(self):
        inst = _instance()
        out = inst.company_search({'filters': 'not-a-list'})
        assert out['success'] is False


class TestSearchRequests:
    @patch('tool_crustdata.IInstance.requests.post')
    def test_company_search_hits_the_company_endpoint_with_the_given_filters(self, mock_post):
        mock_post.return_value = _resp(200, json_data={'results': [{'name': 'Acme'}]})
        inst = _instance()

        out = inst.company_search({'filters': _A_FILTER})

        assert out == {'success': True, 'filters': _A_FILTER, 'count': 1, 'results': [{'name': 'Acme'}]}
        call_kwargs = mock_post.call_args
        assert call_kwargs.args[0] == COMPANY_SEARCH_URL
        assert call_kwargs.kwargs['json'] == {'filters': _A_FILTER, 'limit': 10, 'page': 1}
        assert call_kwargs.kwargs['headers']['authorization'] == 'Bearer test-key'

    @patch('tool_crustdata.IInstance.requests.post')
    def test_person_search_hits_the_person_endpoint(self, mock_post):
        mock_post.return_value = _resp(200, json_data={'data': []})
        inst = _instance()

        out = inst.person_search({'filters': _A_FILTER})

        assert out['success'] is True
        assert mock_post.call_args.args[0] == PERSON_SEARCH_URL

    @patch('tool_crustdata.IInstance.requests.post')
    def test_limit_is_clamped_to_the_documented_range(self, mock_post):
        mock_post.return_value = _resp(200, json_data={'results': []})
        inst = _instance()

        inst.company_search({'filters': _A_FILTER, 'limit': 5000})
        assert mock_post.call_args.kwargs['json']['limit'] == 100

        inst.company_search({'filters': _A_FILTER, 'limit': 0})
        assert mock_post.call_args.kwargs['json']['limit'] == 1

    @patch('tool_crustdata.IInstance.requests.post')
    def test_bool_limit_does_not_become_1_or_0(self, mock_post):
        """Bool is a subclass of int in Python; {'limit': True} must not silently become 1."""
        mock_post.return_value = _resp(200, json_data={'results': []})
        inst = _instance(default_limit=25)

        inst.company_search({'filters': _A_FILTER, 'limit': True})
        assert mock_post.call_args.kwargs['json']['limit'] == 25

    @patch('tool_crustdata.IInstance.time.sleep', return_value=None)
    @patch('tool_crustdata.IInstance.requests.post')
    def test_retries_on_429_then_succeeds(self, mock_post, _sleep):
        mock_post.side_effect = [_resp(429), _resp(200, json_data={'results': [{'name': 'Acme'}]})]
        inst = _instance()

        out = inst.company_search({'filters': _A_FILTER})

        assert out['success'] is True
        assert mock_post.call_count == 2

    @patch('tool_crustdata.IInstance.time.sleep', return_value=None)
    @patch('tool_crustdata.IInstance.requests.post')
    def test_retries_on_5xx_then_gives_up_after_max_retries(self, mock_post, _sleep):
        mock_post.return_value = _resp(503)
        inst = _instance()

        out = inst.company_search({'filters': _A_FILTER})

        assert out['success'] is False
        assert mock_post.call_count == 4  # initial attempt + 3 retries

    @patch('tool_crustdata.IInstance.requests.post')
    def test_timeout_is_reported_as_a_structured_error_not_raised(self, mock_post):
        mock_post.side_effect = requests.exceptions.Timeout('timed out')
        inst = _instance()

        out = inst.company_search({'filters': _A_FILTER})

        assert out['success'] is False
        assert 'timed out' in out['error']

    @patch('tool_crustdata.IInstance.requests.post')
    def test_connection_error_is_reported_as_a_structured_error(self, mock_post):
        mock_post.side_effect = requests.exceptions.ConnectionError('dns failure')
        inst = _instance()

        out = inst.person_search({'filters': _A_FILTER})

        assert out['success'] is False
        assert out['results'] == []
