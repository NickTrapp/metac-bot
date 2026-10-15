import contextlib
import io
import json
import socket
import unittest
from unittest.mock import patch

import support_agent_readiness as readiness


def valid_context():
    return {
        "GITHUB_REPOSITORY": readiness.REPOSITORY,
        "GITHUB_REF": "refs/heads/" + readiness.BRANCH,
        "GITHUB_EVENT_NAME": "push",
        "GITHUB_RUN_NUMBER": "1",
        "GITHUB_RUN_ATTEMPT": "1",
        "GITHUB_WORKFLOW_REF": f"{readiness.REPOSITORY}/.github/workflows/{readiness.WORKFLOW}@refs/heads/{readiness.BRANCH}",
    }


class NoCredentialRead(dict):
    def get(self, key, default=None):
        if key == "OPENAI_API_KEY":
            raise AssertionError("credential must not be read")
        return super().get(key, default)


class ReadinessTests(unittest.TestCase):
    def run_main(self, args, env):
        output = io.StringIO()
        with patch.object(readiness.os, "environ", env), contextlib.redirect_stdout(output):
            with patch.object(socket, "socket", side_effect=AssertionError("network forbidden")):
                with patch.object(socket, "getaddrinfo", side_effect=AssertionError("DNS forbidden")):
                    code = readiness.main(args)
        return code, output.getvalue()

    def test_default_is_offline_and_reads_no_credential(self):
        code, output = self.run_main([], NoCredentialRead())
        result = json.loads(output)
        self.assertEqual(code, 0)
        self.assertFalse(result["paid_execution_enabled"])
        self.assertEqual(result["paid_requests"], 0)
        self.assertEqual(result["new_reservation_usd"], "0.00")

    def test_execute_stops_without_credentials_or_network(self):
        code, output = self.run_main(["--execute"], NoCredentialRead())
        self.assertEqual(code, 2)
        self.assertEqual(json.loads(output), {"error": "paid_execution_not_enabled", "paid_requests": 0})

    def test_presence_is_exact_boolean_only(self):
        sentinel = "synthetic-test-value-never-printed"
        env = valid_context() | {"OPENAI_API_KEY": sentinel}
        code, output = self.run_main(["--credential-presence"], env)
        self.assertEqual(code, 0)
        self.assertEqual(json.loads(output), {"openai_api_key_present": True})
        self.assertNotIn(sentinel, output)

    def test_missing_and_empty_are_false(self):
        for extra in ({}, {"OPENAI_API_KEY": ""}):
            with self.subTest(extra=extra):
                code, output = self.run_main(["--credential-presence"], valid_context() | extra)
                self.assertEqual(code, 0)
                self.assertEqual(json.loads(output), {"openai_api_key_present": False})

    def test_every_context_field_is_required_before_secret_read(self):
        for key in valid_context():
            for replacement in (None, "unexpected"):
                env = NoCredentialRead(valid_context())
                if replacement is None:
                    del env[key]
                else:
                    env[key] = replacement
                with self.subTest(key=key, replacement=replacement):
                    code, output = self.run_main(["--credential-presence"], env)
                    self.assertEqual(code, 2)
                    self.assertEqual(json.loads(output), {"error": "credential_presence_context_blocked"})

    def test_reruns_and_later_runs_are_blocked(self):
        for key in ("GITHUB_RUN_NUMBER", "GITHUB_RUN_ATTEMPT"):
            with self.subTest(key=key):
                env = NoCredentialRead(valid_context() | {key: "2"})
                code, _ = self.run_main(["--credential-presence"], env)
                self.assertEqual(code, 2)

    def test_no_context_fails_closed(self):
        code, output = self.run_main(["--credential-presence"], NoCredentialRead())
        self.assertEqual(code, 2)
        self.assertNotIn("openai_api_key_present", output)


if __name__ == "__main__":
    unittest.main()
