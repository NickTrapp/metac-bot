"""Public-safe field names only; no SDK, provider request, or raw exception text."""
import json
import unittest

import prospective_forecast as pf
import research_diagnostic_check as check
import research_error_diagnostics as diagnostics
from test_prospective_diagnostics import BadRequestError, SECRET
from test_prospective_forecast import NOW


class ParameterDiagnosticTests(unittest.TestCase):
    def diagnostic(self, error):
        return diagnostics.failure_diagnostic(error, phase="sdk_request", response_received=False)

    def test_all_allowed_exact_parameters_are_preserved(self):
        for param in diagnostics.PROVIDER_PARAMS:
            with self.subTest(param=param):
                error = BadRequestError()
                error.param = param
                self.assertEqual(self.diagnostic(error)["provider_param"], param)

    def test_allowlist_top_level_names_are_actual_payload_fields(self):
        payload = pf.research_request(check.QUESTION, pf.question_input(check.QUESTION, NOW.isoformat()))
        names = {p for p in diagnostics.PROVIDER_PARAMS if "." not in p and "[" not in p}
        self.assertEqual(names, set(payload))
        self.assertEqual(len(payload["tools"]), 1)
        self.assertEqual(len(payload["tools"][0]["filters"]["allowed_domains"]), 1)
        self.assertEqual(len(payload["include"]), 1)

    def test_every_allowed_path_resolves_to_an_actual_payload_field(self):
        payload = pf.research_request(check.QUESTION, pf.question_input(check.QUESTION, NOW.isoformat()))
        for path in diagnostics.PROVIDER_PARAMS:
            with self.subTest(path=path):
                value = payload
                for part in path.replace("[0]", ".0").split("."):
                    value = value[int(part)] if part == "0" else value[part]

    def test_unknown_params_and_non_exact_strings_remain_null(self):
        class StringSubclass(str):
            pass
        for param in (None, SECRET, "tools[1]", "tools[0].filters.secret", "model\n",
                      "tools[0].filters.allowed_domains[99]", StringSubclass("model"),
                      {"param": "model", "message": SECRET}, 0, True):
            with self.subTest(param=type(param).__name__):
                error = BadRequestError()
                error.param = param
                result = self.diagnostic(error)
                self.assertIsNone(result["provider_param"])
                self.assertNotIn(SECRET, json.dumps(result))

    def test_missing_param_stays_null_without_reading_forbidden_details(self):
        error = BadRequestError()
        result = self.diagnostic(error)
        self.assertEqual(result, {"phase": "sdk_request", "response_received": False,
            "exception_class": "BadRequestError", "http_status": 400,
            "provider_code": "unsupported_parameter", "provider_param": None})

    def test_hostile_param_getter_cannot_replace_failure(self):
        class HostileError(Exception):
            @property
            def param(self):
                raise ValueError(SECRET)

            def __str__(self):
                raise AssertionError("No exception stringification")

            def __repr__(self):
                raise AssertionError("No exception representation")

        result = self.diagnostic(HostileError())
        self.assertIsNone(result["provider_param"])
        self.assertNotIn(SECRET, json.dumps(result))

    def test_original_diagnostic_is_unchanged(self):
        error = BadRequestError()
        error.param = "model"
        original = pf._failure_diagnostic(error, phase="sdk_request", response_received=False)
        expanded = self.diagnostic(error)
        self.assertNotIn("provider_param", original)
        self.assertEqual(original, {k: v for k, v in expanded.items() if k != "provider_param"})


if __name__ == "__main__":
    unittest.main()
