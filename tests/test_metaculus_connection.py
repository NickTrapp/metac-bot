import contextlib
import io
import json
import unittest
from unittest.mock import Mock, patch

import check_metaculus_connection as check


def valid_payload():
    return {"id": 123, "is_bot": True, "is_active": True, "is_spam": False,
            "api_forecasting_access": "enabled", "email": "do-not-log@example.invalid",
            "username": "do-not-log", "metadata": {"private": "do-not-log"}}


def fake_connection(payload=None, *, status=200, content_type="application/json", body=None):
    response = Mock()
    response.status = status
    response.getheader.return_value = content_type
    response.read.return_value = body if body is not None else json.dumps(
        valid_payload() if payload is None else payload).encode()
    connection = Mock()
    connection.getresponse.return_value = response
    factory = Mock(return_value=connection)
    return factory, connection, response


class IdentityValidationTests(unittest.TestCase):
    def test_returns_only_verified_id_for_internal_readback(self):
        self.assertEqual(check.validate_identity(valid_payload()), 123)

    def test_non_objects_invalid_ids_and_missing_fields_fail_closed(self):
        invalid = [[], None, "private", {}, valid_payload() | {"id": 0},
                   valid_payload() | {"id": -1}, valid_payload() | {"id": True},
                   valid_payload() | {"id": "123"}]
        for key in ["id", "is_bot", "is_active", "is_spam", "api_forecasting_access"]:
            payload = valid_payload(); del payload[key]; invalid.append(payload)
        for payload in invalid:
            with self.subTest(payload=payload), self.assertRaises(check.ConnectionCheckError):
                check.validate_identity(payload)

    def test_status_fields_must_have_exact_safe_values(self):
        for key, value in [("is_bot", False), ("is_bot", 1), ("is_bot", "true"),
                           ("is_active", False), ("is_active", 1), ("is_spam", True),
                           ("is_spam", 0), ("api_forecasting_access", "disabled"),
                           ("api_forecasting_access", "pending"), ("api_forecasting_access", True)]:
            with self.subTest(key=key, value=value), self.assertRaises(check.ConnectionCheckError):
                check.validate_identity(valid_payload() | {key: value})


class TransportTests(unittest.TestCase):
    def test_one_fixed_https_get_no_response_data_output(self):
        factory, connection, response = fake_connection()
        output = io.StringIO()
        with contextlib.redirect_stdout(output), contextlib.redirect_stderr(output):
            result = check.check_connection("dummy-test-token", factory)
        self.assertEqual(result, 123)
        self.assertEqual(output.getvalue(), "")
        factory.assert_called_once_with("www.metaculus.com", timeout=20)
        connection.request.assert_called_once_with("GET", "/api/users/me/", headers={
            "Authorization": "Token dummy-test-token", "Accept": "application/json"})
        response.read.assert_called_once_with(check.MAX_RESPONSE_BYTES + 1)
        connection.close.assert_called_once()

    def test_redirects_and_errors_are_not_followed_or_read(self):
        for status in [301, 302, 307, 308, 401, 403, 429, 500]:
            factory, connection, response = fake_connection(status=status)
            with self.subTest(status=status), self.assertRaises(check.ConnectionCheckError):
                check.check_connection("dummy-test-token", factory)
            connection.request.assert_called_once()
            response.read.assert_not_called()
            connection.close.assert_called_once()

    def test_malformed_json_unexpected_type_and_oversize_fail_closed(self):
        cases = [dict(body=b"not-json PRIVATE BODY"), dict(payload=[]),
                 dict(content_type="text/html"), dict(body=b"x" * (check.MAX_RESPONSE_BYTES + 1))]
        for kwargs in cases:
            factory, connection, _ = fake_connection(**kwargs)
            with self.subTest(kwargs=kwargs.keys()), self.assertRaises(check.ConnectionCheckError) as error:
                check.check_connection("dummy-test-token", factory)
            self.assertNotIn("PRIVATE BODY", str(error.exception))
            connection.close.assert_called_once()

    def test_timeout_has_no_retry_or_raw_error(self):
        factory, connection, _ = fake_connection()
        connection.getresponse.side_effect = TimeoutError("PRIVATE TOKEN AND RESPONSE")
        with self.assertRaises(check.ConnectionCheckError) as error:
            check.check_connection("dummy-test-token", factory)
        self.assertNotIn("PRIVATE", str(error.exception))
        self.assertTrue(error.exception.__suppress_context__)
        connection.request.assert_called_once()
        connection.close.assert_called_once()

    def test_connection_setup_error_is_sanitized(self):
        factory = Mock(side_effect=ValueError("PRIVATE TOKEN"))
        with self.assertRaises(check.ConnectionCheckError) as error:
            check.check_connection("dummy-test-token", factory)
        self.assertNotIn("PRIVATE", str(error.exception))
        factory.assert_called_once()

    def test_invalid_credentials_are_rejected_before_connection(self):
        for token in [None, "", " ", "bad\r\nheader", "has space"]:
            factory = Mock()
            with self.subTest(token=token), self.assertRaises(check.ConnectionCheckError):
                check.check_connection(token, factory)
            factory.assert_not_called()

    def test_content_type_charset_is_accepted(self):
        factory, _, _ = fake_connection(content_type="application/json; charset=utf-8")
        check.check_connection("dummy-test-token", factory)


class EntrypointTests(unittest.TestCase):
    def test_default_cli_reads_no_credentials_and_makes_no_request(self):
        with patch("sys.argv", ["check_metaculus_connection.py"]), \
             patch.object(check.os, "environ") as env, \
             patch.object(check, "check_connection") as request, \
             contextlib.redirect_stdout(io.StringIO()) as output:
            check.main()
        self.assertFalse(any(call.args and call.args[0] == "METACULUS_TOKEN"
                             for call in env.get.call_args_list))
        request.assert_not_called()
        self.assertIn("Offline only", output.getvalue())

    def test_success_output_contains_only_fixed_status(self):
        with patch("sys.argv", ["check_metaculus_connection.py", "--check"]), \
             patch.object(check.os, "environ", {"METACULUS_TOKEN": "dummy-test-token"}), \
             patch.object(check, "check_connection") as request, \
             contextlib.redirect_stdout(io.StringIO()) as output:
            check.main()
        request.assert_called_once_with("dummy-test-token")
        self.assertNotIn("dummy-test-token", output.getvalue())
        self.assertEqual(output.getvalue().strip(),
            "Metaculus bot authentication and account-level API forecasting checks passed. No forecasts were submitted.")


if __name__ == "__main__":
    unittest.main()
