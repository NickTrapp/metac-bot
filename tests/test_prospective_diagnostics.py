"""Negative paths use synthetic data and fake clients; no SDK/key/network."""
import copy
import json
import unittest
from unittest.mock import AsyncMock, Mock

import prospective_forecast as pf
import researched_forecast as rf
from test_prospective_forecast import NOW, SOURCE, evidence, forecast, manifest
from test_researched_forecast import response, sdk_response


SECRET = "MALICIOUS-EXCEPTION-MESSAGE sk-secret https://example.org/?token=secret"


class BadRequestError(Exception):
    """Offline stand-in for the SDK's qualified exception class, not an import."""
    __module__ = "openai._exceptions"

    def __init__(self, status_code=400, code="unsupported_parameter"):
        super().__init__(SECRET)
        self.status_code = status_code
        self.code = code

    def __str__(self):
        raise AssertionError("Diagnostics must not stringify exceptions")

    def __repr__(self):
        raise AssertionError("Diagnostics must not repr exceptions")

    @property
    def body(self):
        raise AssertionError("Diagnostics must not read bodies")

    @property
    def response(self):
        raise AssertionError("Diagnostics must not read responses/headers")

    @property
    def request(self):
        raise AssertionError("Diagnostics must not read requests")

    @property
    def message(self):
        raise AssertionError("Diagnostics must not read messages")


class DiagnosticTests(unittest.IsolatedAsyncioTestCase):
    async def run_failed_research(self, failure):
        create = AsyncMock(side_effect=[forecast(), failure] + [
            item for _ in range(2)
            for item in (forecast(), evidence(), forecast(sources=[SOURCE]))])
        client = Mock()
        client.with_options.return_value.responses.create = create
        snapshots = []
        result = await pf.run_pilot(manifest(), client, reserve=Mock(),
            checkpoint=lambda r: snapshots.append(copy.deepcopy(r)),
            clock=lambda: NOW, monotonic=lambda: 0)
        attempt = result["records"][0]["researched"]["requests"][0]
        self.assertEqual(create.await_count, 8)  # Failed research skips its forecast, never retries.
        self.assertEqual(result["request_attempts"], 8)
        self.assertEqual(result["search_allowance_consumed"], 6)
        self.assertEqual(len(result["records"]), 3)
        self.assertEqual(result["records"][0]["researched"]["status"], "failed")
        self.assertEqual(result["records"][0]["researched"]["operational_probability_yes"], .5)
        client.with_options.assert_called_once_with(base_url=rf.API_BASE, max_retries=0, timeout=90)
        self.assertNotIn(SECRET, pf.canonical(result))
        self.assertNotIn(SECRET, pf.canonical(snapshots))
        self.assertEqual(set(attempt["failure_diagnostic"]), {
            "phase", "response_received", "exception_class", "http_status", "provider_code"})
        self.assertTrue(any(s["records"][0]["researched"]["requests"]
            and s["records"][0]["researched"]["requests"][0]["failure_diagnostic"]
            == attempt["failure_diagnostic"] for s in snapshots))
        return result, attempt

    async def test_local_request_exception_is_distinct_and_has_no_response(self):
        result, attempt = await self.run_failed_research(NameError(SECRET))
        self.assertEqual(attempt["failure_diagnostic"], {
            "phase": "sdk_request", "response_received": False,
            "exception_class": "NameError", "http_status": None, "provider_code": None})
        self.assertIsNone(attempt["response_id"])
        self.assertEqual(attempt["response_usage_metadata"], {"input_tokens": None, "output_tokens": None})
        self.assertIsNone(attempt["usage_observation"])
        self.assertFalse(result["usage_complete"])

    async def test_http_400_exception_keeps_only_allowlisted_fields(self):
        result, attempt = await self.run_failed_research(BadRequestError())
        self.assertEqual(attempt["failure_diagnostic"], {
            "phase": "sdk_request", "response_received": False,
            "exception_class": "BadRequestError", "http_status": 400,
            "provider_code": "unsupported_parameter"})
        self.assertIsNone(attempt["response_id"])
        self.assertIsNone(attempt["usage_observation"])
        self.assertFalse(result["usage_complete"])

    async def test_malformed_response_keeps_metadata_without_fabricating_usage(self):
        malformed = response("synthetic", research=True)
        malformed.update(id="resp_metadata_survives", output=None)
        result, attempt = await self.run_failed_research(sdk_response(malformed))
        self.assertEqual(attempt["failure_diagnostic"], {
            "phase": "response_validate", "response_received": True,
            "exception_class": "ResearchBlocked", "http_status": None, "provider_code": None})
        self.assertEqual(attempt["response_id"], "resp_metadata_survives")
        self.assertEqual(attempt["response_usage_metadata"], {"input_tokens": 100, "output_tokens": 100})
        self.assertIsNone(attempt["usage_observation"])
        self.assertFalse(result["usage_complete"])
        accepted = [request["usage_observation"] for record in result["records"]
                    for arm in ("baseline", "researched") for request in record[arm]["requests"]
                    if request["usage_observation"] is not None]
        self.assertEqual(len(accepted), 7)
        self.assertEqual(pf.Decimal(result["known_estimated_cost_usd"]),
                         sum(pf.Decimal(item["estimated_cost_usd"]) for item in accepted))

    async def test_source_validation_failure_has_distinct_phase_and_keeps_known_usage(self):
        invalid = response("synthetic source", research=True)
        invalid["output"][0]["action"]["sources"][0]["url"] = "https://outside.example/source"
        result, attempt = await self.run_failed_research(sdk_response(invalid))
        self.assertEqual(attempt["failure_diagnostic"], {
            "phase": "source_validate", "response_received": True,
            "exception_class": "ResearchBlocked", "http_status": None, "provider_code": None})
        self.assertEqual(attempt["response_id"], "resp_offline_fixture")
        self.assertEqual(attempt["response_usage_metadata"], {"input_tokens": 100, "output_tokens": 100})
        self.assertIsNotNone(attempt["usage_observation"])
        self.assertTrue(result["usage_complete"])

    async def test_metadata_failure_records_that_sdk_returned(self):
        malformed = response("synthetic", research=True)
        malformed["usage"] = "malformed usage"
        result, attempt = await self.run_failed_research(sdk_response(malformed))
        self.assertEqual(attempt["failure_diagnostic"], {
            "phase": "response_metadata", "response_received": True,
            "exception_class": "AttributeError", "http_status": None, "provider_code": None})
        self.assertEqual(attempt["response_id"], "resp_offline_fixture")
        self.assertEqual(attempt["response_usage_metadata"], {"input_tokens": None, "output_tokens": None})
        self.assertIsNone(attempt["usage_observation"])
        self.assertFalse(result["usage_complete"])

    async def test_unknown_exception_class_and_code_are_null_and_message_is_absent(self):
        UnknownException = type("UntrustedSecretClassName", (Exception,), {})
        exc = UnknownException(SECRET)
        exc.code = SECRET
        result, attempt = await self.run_failed_research(exc)
        self.assertIsNone(attempt["failure_diagnostic"]["exception_class"])
        self.assertIsNone(attempt["failure_diagnostic"]["provider_code"])
        self.assertNotIn("UntrustedSecretClassName", pf.canonical(result))


class MetadataAllowlistTests(unittest.TestCase):
    def test_id_shape_and_exact_integer_counts_are_validated_independently(self):
        cases = [
            ("resp_valid_123", 0, 42, "resp_valid_123", 0, 42),
            ("resp_valid", True, -1, "resp_valid", None, None),
            ("resp_valid", 1.0, "2", "resp_valid", None, None),
            (SECRET, 100, None, None, 100, None),
            ("resp_" + "a" * 193, None, 0, None, None, 0),
            ("resp_bad\n", 1, 2, None, 1, 2),
            (None, 1, 2, None, 1, 2),
        ]
        for ident, inp, out, expected_id, expected_inp, expected_out in cases:
            with self.subTest(ident=ident, input_tokens=inp, output_tokens=out):
                attempt = {"response_id": None,
                           "response_usage_metadata": {"input_tokens": None, "output_tokens": None}}
                pf._capture_response_metadata(sdk_response({"id": ident,
                    "usage": {"input_tokens": inp, "output_tokens": out}}), attempt)
                self.assertEqual(attempt, {"response_id": expected_id,
                    "response_usage_metadata": {"input_tokens": expected_inp, "output_tokens": expected_out}})
                self.assertNotIn(SECRET, json.dumps(attempt))

    def test_http_status_must_be_exact_integer_in_range_and_code_exact_allowlist(self):
        class IntegerSubclass(int):
            pass
        class StringSubclass(str):
            pass
        for status in (None, True, 400.0, "400", 99, 600, IntegerSubclass(400)):
            with self.subTest(status=status):
                diagnostic = pf._failure_diagnostic(BadRequestError(status, SECRET),
                    phase="sdk_request", response_received=False)
                self.assertIsNone(diagnostic["http_status"])
                self.assertIsNone(diagnostic["provider_code"])
                self.assertNotIn(SECRET, json.dumps(diagnostic))
        for status in (100, 400, 599):
            self.assertEqual(pf._failure_diagnostic(BadRequestError(status),
                phase="sdk_request", response_received=False)["http_status"], status)
        for code in (None, {"message": SECRET}, StringSubclass("unsupported_parameter")):
            self.assertIsNone(pf._failure_diagnostic(BadRequestError(code=code),
                phase="sdk_request", response_received=False)["provider_code"])

    def test_exception_attribute_getters_cannot_replace_or_leak_original_failure(self):
        class HostileException(Exception):
            @property
            def status_code(self):
                raise ValueError(SECRET)

            @property
            def code(self):
                raise ValueError(SECRET)

        diagnostic = pf._failure_diagnostic(HostileException(SECRET),
            phase="sdk_request", response_received=False)
        self.assertEqual(diagnostic, {"phase": "sdk_request", "response_received": False,
            "exception_class": None, "http_status": None, "provider_code": None})

    def test_local_exception_allowlist_and_sdk_qualified_name_restriction(self):
        for exception_type in (rf.ResearchBlocked, NameError, TypeError, AttributeError,
                               ValueError, TimeoutError):
            with self.subTest(exception_type=exception_type):
                self.assertEqual(pf._failure_diagnostic(exception_type(SECRET),
                    phase="sdk_request", response_received=False)["exception_class"], exception_type.__name__)
        impostor = type("BadRequestError", (Exception,), {"__module__": "untrusted"})
        self.assertIsNone(pf._failure_diagnostic(impostor(SECRET),
            phase="sdk_request", response_received=False)["exception_class"])


if __name__ == "__main__":
    unittest.main()
