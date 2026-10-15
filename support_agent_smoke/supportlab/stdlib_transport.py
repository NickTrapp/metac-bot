"""Explicit no-install Responses transport; same projected ToolBackend contract.

This is a reviewed transport adaptation, not the OpenAI SDK. Fixed official
host, TLS verification, no proxies, redirects, retries, or hosted tools.
"""
import http.client
import json
import math
import os
import ssl

HOST = "api.openai.com"
MAX_REQUEST_BYTES = 200_000
MAX_RESPONSE_BYTES = 1_000_000


class TransportError(Exception):
    """Fixed non-sensitive failure, never a response body or header."""


class StdlibResponsesBackend:
    is_paid = True
    label = "openai-responses-stdlib-https"

    def __init__(self, timeout_seconds=120, connection_factory=None):
        if (type(timeout_seconds) not in (int, float)
                or not math.isfinite(timeout_seconds) or not 0 < timeout_seconds <= 120):
            raise ValueError("invalid_transport_timeout")
        self.timeout_seconds = timeout_seconds
        # Injection is only for offline transport tests.
        self._connection_factory = connection_factory or http.client.HTTPSConnection

    def _post(self, path, payload):
        if path not in ("/v1/responses", "/v1/responses/input_tokens"):
            raise TransportError("endpoint_not_allowed")
        body = json.dumps(payload, ensure_ascii=False, allow_nan=False,
                          separators=(",", ":")).encode("utf-8")
        if len(body) > MAX_REQUEST_BYTES:
            raise TransportError("request_too_large")
        key = os.environ.get("OPENAI_API_KEY")
        if not key:
            raise TransportError("credential_unavailable")
        connection = self._connection_factory(HOST, timeout=self.timeout_seconds,
                                              context=ssl.create_default_context())
        try:
            connection.request("POST", path, body=body,
                               headers={"Authorization": "Bearer " + key,
                                        "Content-Type": "application/json", "Accept": "application/json"})
            response = connection.getresponse()
            # http.client never follows redirects. Never read an error body.
            if response.status != 200:
                raise TransportError("provider_http_failure")
            if response.getheader("Content-Type", "").split(";", 1)[0].strip().lower() != "application/json":
                raise TransportError("provider_content_type_invalid")
            raw = response.read(MAX_RESPONSE_BYTES + 1)
            if len(raw) > MAX_RESPONSE_BYTES:
                raise TransportError("provider_response_too_large")
            try:
                data = json.loads(raw, parse_constant=lambda _: (_ for _ in ()).throw(ValueError()))
            except (ValueError, UnicodeError):
                raise TransportError("provider_json_invalid") from None
            if type(data) is not dict:
                raise TransportError("provider_envelope_invalid")
            return data
        except TransportError:
            raise
        except (TimeoutError, OSError, http.client.HTTPException):
            raise TransportError("provider_transport_failed") from None
        finally:
            connection.close()

    def count_input_tokens(self, request):
        args = {key: request[key] for key in
                ("model", "input", "instructions", "tools", "tool_choice") if key in request}
        data = self._post("/v1/responses/input_tokens", args)
        count = data.get("input_tokens")
        if type(count) is not int or count < 0:
            raise TransportError("provider_token_count_invalid")
        return count

    def request(self, request):
        data = self._post("/v1/responses", request)
        output = data.get("output")
        if type(output) is not list:
            raise TransportError("provider_output_invalid")
        projected = []
        for item in output:
            if type(item) is not dict or not isinstance(item.get("type"), str):
                raise TransportError("provider_output_item_invalid")
            if item["type"] == "function_call":
                projected.append({key: item.get(key) for key in ("type", "name", "arguments", "call_id")})
        usage = data.get("usage")
        normalized = None
        if usage is not None:
            if type(usage) is not dict:
                raise TransportError("provider_usage_invalid")
            inputs, outputs = usage.get("input_tokens_details"), usage.get("output_tokens_details")
            if inputs is not None and type(inputs) is not dict or outputs is not None and type(outputs) is not dict:
                raise TransportError("provider_usage_details_invalid")
            normalized = {"input_tokens": usage.get("input_tokens"), "output_tokens": usage.get("output_tokens"),
                          "input_tokens_details": {"cached_tokens": (inputs or {}).get("cached_tokens", 0)},
                          "output_tokens_details": {"reasoning_tokens": (outputs or {}).get("reasoning_tokens", 0)}}
        return {"id": data.get("id"), "model": data.get("model"), "status": data.get("status"),
                "output": projected, "usage": normalized}
