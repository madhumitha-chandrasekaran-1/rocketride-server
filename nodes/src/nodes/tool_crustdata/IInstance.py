# =============================================================================
# RocketRide Engine
# =============================================================================
# MIT License
# Copyright (c) 2026 Aparavi Software AG
#
# Permission is hereby granted, free of charge, to any person obtaining a copy
# of this software and associated documentation files (the "Software"), to deal
# in the Software without restriction, including without limitation the rights
# to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
# copies of the Software, and to permit persons to whom the Software is
# furnished to do so, subject to the following conditions:
#
# The above copyright notice and this permission notice shall be included in
# all copies or substantial portions of the Software.
#
# THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
# IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
# FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
# AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
# LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
# OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
# SOFTWARE.
# =============================================================================

"""
Crustdata tool node instance.

Exposes ``company_search`` and ``person_search`` as @tool_function methods,
giving an agent structured B2B discovery/enrichment data (firmographics,
funding, headcount, verified people profiles) via Crustdata's filter-based
search API.

UNVERIFIED SURFACE: built from Crustdata's public documentation
(https://docs.crustdata.com/), not against a live account (see #2129). The
``filters`` shape (list of ``{filter_type, type, value}``) and the
``/company/search`` / ``/person-docs/search`` endpoints are corroborated by
multiple doc pages, but the exhaustive ``filter_type`` enum and the exact
response envelope are not confirmed. Response parsing is deliberately
defensive (``_extract_records`` tries several plausible top-level keys)
rather than assuming one exact shape, so a real account is needed to
tighten this rather than to make it merely work.
"""

from __future__ import annotations

import time
from typing import Any, Dict, List

import requests

from rocketlib import IInstanceBase, tool_function, debug

from ai.common.utils import normalize_tool_input

from .IGlobal import IGlobal

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

CRUSTDATA_BASE_URL = 'https://api.crustdata.com'
COMPANY_SEARCH_URL = f'{CRUSTDATA_BASE_URL}/company/search'
PERSON_SEARCH_URL = f'{CRUSTDATA_BASE_URL}/person-docs/search'

# Every endpoint requires this version pin (docs.crustdata.com); /screener/*
# and /data_lab/* are documented as legacy predecessors of this versioned API.
CRUSTDATA_API_VERSION = '2025-11-01'

# Top-level keys a search response's record list has been observed or
# documented under. Tried in order; the first present list wins.
_RECORD_LIST_KEYS = ('results', 'data', 'companies', 'people', 'profiles')

_FILTERS_SCHEMA = {
    'type': 'array',
    'minItems': 1,
    'items': {
        'type': 'object',
        'required': ['filter_type', 'type', 'value'],
        'properties': {
            'filter_type': {
                'type': 'string',
                'description': (
                    "The Crustdata field to filter on, e.g. 'CURRENT_COMPANY', 'CURRENT_TITLE', "
                    "'REGION', 'INDUSTRY', 'HEADCOUNT' (see Crustdata's filter reference for the full list)."
                ),
            },
            'type': {
                'type': 'string',
                'description': "The filter operator, e.g. 'in', 'not in', '=', 'range'.",
            },
            'value': {
                'description': 'The filter value: a string, number, or array of strings depending on filter_type/type.',
            },
        },
    },
    'description': 'One or more Crustdata search filters, passed through verbatim to the API.',
}

_OUTPUT_SCHEMA = {
    'type': 'object',
    'properties': {
        'success': {'type': 'boolean'},
        'filters': {'type': 'array'},
        'count': {'type': 'integer'},
        'results': {'type': 'array', 'items': {'type': 'object'}},
        'error': {'type': 'string'},
    },
}


class IInstance(IInstanceBase):
    """Node instance exposing Crustdata company/people search as agent tools."""

    IGlobal: IGlobal

    @tool_function(
        input_schema={
            'type': 'object',
            'required': ['filters'],
            'properties': {
                'filters': _FILTERS_SCHEMA,
                'limit': {
                    'type': 'integer',
                    'description': 'Maximum number of results to return. Defaults to the node config value.',
                },
                'page': {
                    'type': 'integer',
                    'description': '1-based page number for pagination. Defaults to 1.',
                },
            },
        },
        output_schema=_OUTPUT_SCHEMA,
        description=(
            'Search Crustdata for companies matching one or more filters (industry, region, headcount, '
            'funding, current company, and more). Returns structured company records: firmographics, '
            'funding history, headcount, and hiring signals. Use this to find prospects or research '
            'accounts by criteria, not to look up one already-known company by name.'
        ),
    )
    def company_search(self, args):
        """Search Crustdata's company index by filter criteria."""
        return self._search(args, url=COMPANY_SEARCH_URL, tool_name='company_search')

    @tool_function(
        input_schema={
            'type': 'object',
            'required': ['filters'],
            'properties': {
                'filters': _FILTERS_SCHEMA,
                'limit': {
                    'type': 'integer',
                    'description': 'Maximum number of results to return. Defaults to the node config value.',
                },
                'page': {
                    'type': 'integer',
                    'description': '1-based page number for pagination. Defaults to 1.',
                },
            },
        },
        output_schema=_OUTPUT_SCHEMA,
        description=(
            'Search Crustdata for people matching one or more filters (current company, current title, '
            'region, and more). Returns structured profiles: name, title, work history, education, and '
            'verified contact info where available. Use this to find or enrich people by criteria.'
        ),
    )
    def person_search(self, args):
        """Search Crustdata's people index by filter criteria."""
        return self._search(args, url=PERSON_SEARCH_URL, tool_name='person_search')

    # -------------------------------------------------------------------
    # Shared request path
    # -------------------------------------------------------------------

    def _search(self, args: Dict[str, Any], *, url: str, tool_name: str) -> Dict[str, Any]:
        args = normalize_tool_input(args, tool_name=tool_name)

        filters = args.get('filters')
        if not isinstance(filters, list) or not filters:
            return {
                'success': False,
                'filters': [],
                'count': 0,
                'results': [],
                'error': f'{tool_name}: "filters" is required and must be a non-empty array',
            }

        cfg = self.IGlobal
        limit = args.get('limit', cfg.default_limit)
        if isinstance(limit, bool) or not isinstance(limit, int):
            limit = cfg.default_limit
        limit = max(1, min(100, limit))

        page = args.get('page', 1)
        if isinstance(page, bool) or not isinstance(page, int) or page < 1:
            page = 1

        payload: Dict[str, Any] = {'filters': filters, 'limit': limit, 'page': page}
        headers = _crustdata_headers(cfg.apikey)

        try:
            response = _request_with_retry(url=url, headers=headers, payload=payload)
        except RuntimeError as exc:
            return {'success': False, 'filters': filters, 'count': 0, 'results': [], 'error': str(exc)}

        records = _extract_records(response)
        return {
            'success': True,
            'filters': filters,
            'count': len(records),
            'results': records,
        }


# ---------------------------------------------------------------------------
# Helpers (pure, no network — unit-testable without mocking requests)
# ---------------------------------------------------------------------------


def _crustdata_headers(apikey: str) -> Dict[str, str]:
    return {
        'accept': 'application/json',
        'content-type': 'application/json',
        'authorization': f'Bearer {apikey}',
        'x-api-version': CRUSTDATA_API_VERSION,
    }


def _extract_records(body: Any) -> List[Dict[str, Any]]:
    """Best-effort extraction of the record list from a search response body.

    The exact response envelope is unverified (see module docstring), so this
    tries several plausible top-level keys in order rather than assuming one.
    A bare top-level list is also accepted. Non-dict items are dropped rather
    than raised on, matching the defensive pattern used elsewhere for
    upstream payloads whose exact shape isn't guaranteed (see tool_tavily's
    ``_shape_results``).
    """
    if isinstance(body, list):
        return [item for item in body if isinstance(item, dict)]
    if not isinstance(body, dict):
        return []
    for key in _RECORD_LIST_KEYS:
        value = body.get(key)
        if isinstance(value, list):
            return [item for item in value if isinstance(item, dict)]
    return []


def _request_with_retry(
    *,
    url: str,
    headers: Dict[str, str],
    payload: Dict[str, Any],
    max_retries: int = 3,
    base_delay: float = 2.0,
) -> Any:
    """Execute an HTTP POST to the Crustdata API with retry on transient errors."""
    for attempt in range(max_retries + 1):
        try:
            resp = requests.post(url, headers=headers, json=payload, timeout=30)

            if resp.status_code == 429:
                if attempt < max_retries:
                    delay = base_delay * (2**attempt)
                    debug(f'Crustdata rate limit hit (429), retrying in {delay}s (attempt {attempt + 1}/{max_retries})')
                    time.sleep(delay)
                    continue
                resp.raise_for_status()

            if 500 <= resp.status_code < 600:
                if attempt < max_retries:
                    delay = base_delay * (2**attempt)
                    debug(
                        f'Crustdata server error ({resp.status_code}), retrying in {delay}s '
                        f'(attempt {attempt + 1}/{max_retries})'
                    )
                    time.sleep(delay)
                    continue
                resp.raise_for_status()

            resp.raise_for_status()
            return resp.json()

        except requests.exceptions.Timeout:
            if attempt < max_retries:
                delay = base_delay * (2**attempt)
                debug(f'Crustdata request timeout, retrying in {delay}s (attempt {attempt + 1}/{max_retries})')
                time.sleep(delay)
                continue
            raise RuntimeError('Crustdata search: request timed out after all retries') from None

        except requests.RequestException as exc:
            status = getattr(getattr(exc, 'response', None), 'status_code', None)
            detail = f' (HTTP {status})' if status else ''
            raise RuntimeError(f'Crustdata search request failed{detail}: {type(exc).__name__}') from None

    raise RuntimeError('Crustdata search: max retries exceeded')
