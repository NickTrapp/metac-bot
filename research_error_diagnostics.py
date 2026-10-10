"""Additional closed-vocabulary diagnostics for a future separately approved test.

Importing this module is offline. It does not change the ended one-shot runners
or their artifacts, and does not grant permission for another API request.
"""
from __future__ import annotations

import prospective_forecast as pf

# Exact names of fields actually present in the fixed research request. Both
# provider dot-index and bracket-index spellings are accepted, but only index 0:
# this payload has one tool, one domain, and one include entry. Never accept an
# arbitrary path, regex match, object, string subclass, exception text, or body.
PROVIDER_PARAMS = frozenset({
    "model", "instructions", "input", "max_output_tokens", "temperature",
    "store", "service_tier", "truncation", "tools", "tool_choice",
    "max_tool_calls", "include", "include[0]", "include.0",
    "tools[0]", "tools.0", "tools[0].type", "tools.0.type",
    "tools[0].search_context_size", "tools.0.search_context_size",
    "tools[0].filters", "tools.0.filters",
    "tools[0].filters.allowed_domains", "tools.0.filters.allowed_domains",
    "tools[0].filters.allowed_domains[0]", "tools.0.filters.allowed_domains.0",
})


def failure_diagnostic(exc: Exception, *, phase: str, response_received: bool) -> dict:
    diagnostic = pf._failure_diagnostic(exc, phase=phase, response_received=response_received)
    param = pf._exception_attribute(exc, "param")
    diagnostic["provider_param"] = param if type(param) is str and param in PROVIDER_PARAMS else None
    return diagnostic
