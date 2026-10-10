import contextlib
import copy
from datetime import datetime, timezone
import io
import json
from pathlib import Path
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, Mock, patch

import metaculus_end_to_end_test as e2e
import safe_smoke_test as safe


def project():
    return {"id": 900, "slug": "bot-testing-area", "type": "question_series"}


def post(post_id=101, question_id=202):
    timing = {"status": "open", "open_time": "2020-01-01T00:00:00Z",
              "scheduled_close_time": "2050-01-01T00:00:00Z",
              "actual_close_time": "2050-01-01T00:00:00Z", "actual_resolve_time": None}
    return {"id": post_id, "projects": {"default_project": project(), "question_series": [project()]},
            "curation_status": "approved", "resolved": False, "user_permission": "forecaster",
            "question": {"id": question_id, "post_id": post_id, "type": "binary", "title": "Synthetic question?",
                "description": "Synthetic background", "resolution_criteria": "Synthetic resolution",
                "fine_print": "Synthetic fine print", "resolution": None, **timing}, **timing}


def target():
    return e2e.validate_target(post(), 101, 900, datetime.now(timezone.utc))


def prior_history():
    return {"total_count": 1, "workflow_runs": [{"id": e2e.PRIOR_RUN_ID,
        "head_sha": e2e.PRIOR_COMMIT, "run_number": 1, "run_attempt": 1,
        "status": "completed", "conclusion": "success", "path": f".github/workflows/{safe.WORKFLOW}"}]}


def recovery_env():
    return {"GITHUB_REPOSITORY": safe.REPOSITORY, "GITHUB_EVENT_NAME": "workflow_dispatch",
        "GITHUB_RUN_ATTEMPT": "1", "GITHUB_RUN_NUMBER": "4", "GITHUB_RUN_ID": "999",
        "GITHUB_SHA": "a" * 40, "INITIAL_OPENAI_TEST_APPROVED": "true",
        "METACULUS_TEST_SUBMISSION_APPROVED": "true",
        "GITHUB_WORKFLOW_REF": f"{safe.REPOSITORY}/.github/workflows/{safe.END_TO_END_WORKFLOW}@refs/heads/main"}


def recovery_history():
    common = {"path": f".github/workflows/{safe.END_TO_END_WORKFLOW}", "event": "workflow_dispatch",
              "head_branch": "main", "workflow_id": 380215345, "run_attempt": 1}
    return {"total_count": 4, "workflow_runs": [
        common | {"id": 999, "head_sha": "a" * 40, "run_number": 4, "status": "in_progress", "conclusion": None},
        common | {"id": 38019568451, "head_sha": e2e.SELECTOR_COMMIT, "run_number": 3,
                  "status": "completed", "conclusion": "failure"},
        common | {"id": 38019014010, "head_sha": e2e.PREFLIGHT_COMMIT, "run_number": 2,
                  "status": "completed", "conclusion": "failure"},
        common | {"id": 38018946468, "head_sha": e2e.PREFLIGHT_COMMIT, "run_number": 1,
                  "status": "completed", "conclusion": "failure"}]}


def preflight_jobs(run_id=38018946468, job_id=114115438500):
    source = e2e.SELECTOR_COMMIT if run_id == 38019568451 else e2e.PREFLIGHT_COMMIT
    outcomes = e2e.SELECTOR_STEPS if run_id == 38019568451 else e2e.PREFLIGHT_STEPS
    return {"total_count": 1, "jobs": [{"id": job_id, "run_id": run_id, "run_attempt": 1,
        "head_sha": source, "name": "test_area_check", "status": "completed", "conclusion": "failure",
        "steps": [{"name": name, "status": "completed", "conclusion": conclusion}
                  for name, conclusion in outcomes.items()]}]}


def empty_history():
    return {"results": [{"question_id": 202, "forecasts": []}]}


def readback_data(payload, started):
    p = payload["forecasts"][0]["probability_yes"]
    forecasts = {"results": [{"question_id": 202, "forecasts": [{
        "question_id": 202, "author_id": 123, "start_time": started,
        "end_time": None, "forecast_values": [1 - p, p]}]}]}
    comments = {"results": [{"id": 456, "author": {"id": 123}, "on_post": 101,
        "is_private": True, "text": payload["comments"][0]["text"], "is_soft_deleted": False,
        "included_forecast": {"question_type": "binary", "probability_yes": p},
        "created_at": datetime.fromtimestamp(started, timezone.utc).isoformat()}]}
    return forecasts, comments


class CampaignTests(unittest.TestCase):
    def test_prior_reservation_is_retained_and_under_total(self):
        e2e.check_prior_campaign(prior_history())
        self.assertEqual(e2e.PRIOR_RESERVED_USD + safe.MAX_REQUEST_ATTEMPTS * safe.RESERVATION_PER_REQUEST_USD, 2)

    def test_previous_run_must_be_exact_untampered_success(self):
        for key, value in [("id", 999), ("head_sha", "other"), ("run_number", 2),
                           ("run_attempt", 2), ("status", "in_progress"),
                           ("conclusion", "failure"), ("path", "other.yaml")]:
            history = prior_history(); history["workflow_runs"][0][key] = value
            with self.subTest(key=key), self.assertRaises(e2e.TestBlocked):
                e2e.check_prior_campaign(history)
        with self.assertRaises(e2e.TestBlocked):
            e2e.check_prior_campaign({"total_count": 2, "workflow_runs": prior_history()["workflow_runs"]})

    def test_new_workflow_reuses_exact_one_shot_gate(self):
        env = {"GITHUB_REPOSITORY": safe.REPOSITORY, "GITHUB_EVENT_NAME": "workflow_dispatch",
            "GITHUB_RUN_ATTEMPT": "1", "GITHUB_RUN_NUMBER": "1", "GITHUB_RUN_ID": "456",
            "GITHUB_WORKFLOW_REF": f"{safe.REPOSITORY}/.github/workflows/{safe.END_TO_END_WORKFLOW}@refs/heads/main"}
        history = {"total_count": 1, "workflow_runs": [{"id": 456}]}
        safe.verify_campaign_run(env, history, safe.END_TO_END_WORKFLOW)
        with self.assertRaises(RuntimeError):
            safe.verify_campaign_run(env, history)  # Original workflow still rejects the new path.
        for key in ["GITHUB_RUN_NUMBER", "GITHUB_RUN_ATTEMPT"]:
            with self.subTest(key=key), self.assertRaises(RuntimeError):
                safe.verify_campaign_run(env | {key: "2"}, history, safe.END_TO_END_WORKFLOW)

    def test_submission_approval_is_required_before_history_requests(self):
        with patch.object(e2e, "check_paid_authorization") as gate:
            with self.assertRaises(e2e.TestBlocked):
                e2e.check_end_to_end_gate({})
            gate.assert_not_called()


class RecoveryTests(unittest.TestCase):
    def test_exact_three_reconciled_runs_and_recovery_are_allowed(self):
        e2e.verify_recovery_history(recovery_env(), recovery_history())
        self.assertEqual(e2e.PREFLIGHT_COMMIT, "199d840c3c0a3866b28b3a35f6c38f9ea4623441")
        self.assertEqual(e2e.PREFLIGHT_RUNS, ((38018946468, 1, 114115438500), (38019014010, 2, 114115645940)))
        self.assertEqual(e2e.SELECTOR_RUN, (38019568451, 3, 114117334084))
        self.assertEqual(e2e.SELECTOR_COMMIT, "31f3f2b5ab2c50f4d1ee8355c85faaf47fe6bdf5")

    def test_every_changed_prior_or_current_metadata_field_blocks(self):
        for index in range(4):
            for key, value in [("head_sha", "b" * 40), ("run_number", 5), ("run_attempt", 2),
                    ("status", "queued"), ("conclusion", "success"), ("event", "schedule"),
                    ("head_branch", "other"), ("path", "other.yaml"), ("workflow_id", 999)]:
                history = recovery_history(); history["workflow_runs"][index][key] = value
                with self.subTest(index=index, key=key), self.assertRaises(e2e.TestBlocked):
                    e2e.verify_recovery_history(recovery_env(), history)

    def test_incomplete_deleted_duplicate_extra_or_unknown_history_blocks(self):
        cases = [{}, recovery_history() | {"total_count": 3}, recovery_history() | {"total_count": 5}]
        for change in ("missing", "duplicate", "extra", "unknown", "malformed"):
            history = recovery_history(); runs = history["workflow_runs"]
            if change == "missing": runs.pop()
            elif change == "duplicate": runs[2] = runs[1]
            elif change == "extra": runs.append({"id": 998})
            elif change == "unknown": runs[2]["id"] = 998
            else: runs[2] = None
            cases.append(history)
        for history in cases:
            with self.subTest(history=history), self.assertRaises(e2e.TestBlocked):
                e2e.verify_recovery_history(recovery_env(), history)

    def test_wrong_current_run_identity_blocks(self):
        for key, value in [("GITHUB_RUN_ID", "38018946468"), ("GITHUB_RUN_ID", "bad"),
                           ("GITHUB_SHA", ""), ("GITHUB_SHA", "g" * 40)]:
            with self.subTest(key=key), self.assertRaises(e2e.TestBlocked):
                e2e.verify_recovery_history(recovery_env() | {key: value}, recovery_history())

    def test_exact_attempt_jobs_verify(self):
        e2e.verify_preflight_jobs(preflight_jobs(), 38018946468, 114115438500)

    def test_any_changed_step_outcome_or_status_blocks(self):
        for index in range(len(e2e.PREFLIGHT_STEPS)):
            for key, value in [("conclusion", "cancelled"), ("status", "in_progress")]:
                jobs = preflight_jobs(); jobs["jobs"][0]["steps"][index][key] = value
                with self.subTest(index=index, key=key), self.assertRaises(e2e.TestBlocked):
                    e2e.verify_preflight_jobs(jobs, 38018946468, 114115438500)

    def test_executed_effect_step_is_never_reconciled(self):
        for name in ["Install Poetry", "Install locked dependencies", "Verify baseline formats with fake responses",
                     "Check bot identity, submit one test forecast, and verify readback"]:
            jobs = preflight_jobs()
            for step in jobs["jobs"][0]["steps"]:
                if step["name"] == name: step["conclusion"] = "success"
            with self.subTest(name=name), self.assertRaises(e2e.TestBlocked):
                e2e.verify_preflight_jobs(jobs, 38018946468, 114115438500)

    def test_wrong_job_metadata_and_missing_duplicate_or_extra_steps_block(self):
        cases = [{}, preflight_jobs() | {"total_count": 2}]
        for key, value in [("id", 999), ("run_id", 999), ("run_attempt", 2), ("head_sha", "b" * 40),
                           ("name", "other"), ("status", "in_progress"), ("conclusion", "success")]:
            jobs = preflight_jobs(); jobs["jobs"][0][key] = value; cases.append(jobs)
        for change in ("missing", "duplicate", "extra", "malformed"):
            jobs = preflight_jobs(); steps = jobs["jobs"][0]["steps"]
            if change == "missing": steps.pop()
            elif change == "duplicate": steps[-1] = steps[-2]
            elif change == "extra": steps.append(steps[-1])
            else: steps[-1] = None
            cases.append(jobs)
        for jobs in cases:
            with self.subTest(jobs=jobs), self.assertRaises(e2e.TestBlocked):
                e2e.verify_preflight_jobs(jobs, 38018946468, 114115438500)

    def test_gate_reads_all_pinned_attempts_and_preserves_prior_reservation(self):
        with patch.object(e2e, "read_workflow_history", side_effect=[recovery_history(), prior_history()]) as history, \
             patch.object(e2e, "read_actions_json", side_effect=[preflight_jobs(run_id, job_id)
                for run_id, _, job_id in e2e.RECONCILED_RUNS]) as jobs:
            e2e.check_end_to_end_gate(recovery_env())
        self.assertEqual([call.args[1] for call in jobs.call_args_list], [
            "runs/38018946468/attempts/1/jobs?per_page=100", "runs/38019014010/attempts/1/jobs?per_page=100",
            "runs/38019568451/attempts/1/jobs?per_page=100"])
        self.assertEqual([call.args[1] for call in history.call_args_list], [safe.END_TO_END_WORKFLOW, safe.WORKFLOW])
        self.assertEqual(e2e.PRIOR_RESERVED_USD + safe.MAX_REQUEST_ATTEMPTS * safe.RESERVATION_PER_REQUEST_USD, 2)

    def test_bad_context_is_rejected_before_history_read(self):
        for key, value in [("GITHUB_RUN_NUMBER", "1"), ("GITHUB_RUN_NUMBER", "2"), ("GITHUB_RUN_NUMBER", "3"), ("GITHUB_RUN_NUMBER", "5"),
                           ("GITHUB_RUN_ATTEMPT", "2"), ("GITHUB_EVENT_NAME", "schedule"),
                           ("GITHUB_REPOSITORY", "other/repo"), ("GITHUB_WORKFLOW_REF", "other"),
                           ("INITIAL_OPENAI_TEST_APPROVED", "false"), ("METACULUS_TEST_SUBMISSION_APPROVED", "false")]:
            with patch.object(e2e, "read_workflow_history") as history:
                with self.subTest(key=key, value=value), self.assertRaises(RuntimeError):
                    e2e.check_end_to_end_gate(recovery_env() | {key: value})
                history.assert_not_called()

    def test_missing_job_evidence_stops_without_api_work(self):
        with patch.object(e2e, "read_workflow_history", return_value=recovery_history()), \
             patch.object(e2e, "read_actions_json", side_effect=RuntimeError("Unavailable")), \
             patch.object(e2e, "check_connection") as connection, patch.object(e2e, "make_forecast") as paid:
            with self.assertRaises(RuntimeError):
                e2e.run_test(recovery_env())
            connection.assert_not_called(); paid.assert_not_called()

    def test_original_smoke_gate_still_rejects_run_three_and_override(self):
        env = recovery_env() | {"GITHUB_WORKFLOW_REF": f"{safe.REPOSITORY}/.github/workflows/{safe.WORKFLOW}@refs/heads/main"}
        with self.assertRaises(RuntimeError):
            safe.verify_campaign_run(env, recovery_history())
        with self.assertRaises(RuntimeError):
            safe.verify_campaign_context(env, safe.WORKFLOW, 3)

    def test_only_exact_manually_reconciled_selector_job_is_allowed(self):
        data = preflight_jobs(38019568451, 114117334084)
        e2e.verify_preflight_jobs(data, 38019568451, 114117334084)
        for key, value in [("run_attempt", 2), ("head_sha", e2e.PREFLIGHT_COMMIT), ("id", 999)]:
            changed = copy.deepcopy(data); changed["jobs"][0][key] = value
            with self.subTest(key=key), self.assertRaises(e2e.TestBlocked):
                e2e.verify_preflight_jobs(changed, 38019568451, 114117334084)
        with self.assertRaises(e2e.TestBlocked):
            e2e.verify_preflight_jobs(data, 38019568451, 999)
        for step in data["jobs"][0]["steps"]:
            if step["name"].startswith("Check bot identity"):
                step["conclusion"] = "success"
        with self.assertRaises(e2e.TestBlocked):
            e2e.verify_preflight_jobs(data, 38019568451, 114117334084)


class TargetTests(unittest.TestCase):
    def test_schema_valid_empty_criteria_are_preserved_without_invented_rules(self):
        p = post(); p["question"]["resolution_criteria"] = ""
        t = e2e.validate_target(p, 101, 900, datetime.now(timezone.utc))
        self.assertEqual(t.resolution_criteria, "")
        self.assertEqual(t.title, "Synthetic question?")
        for value in (None, 17, {}, []):
            p["question"]["resolution_criteria"] = value
            with self.subTest(value=value), self.assertRaises(e2e.TestBlocked):
                e2e.validate_target(p, 101, 900, datetime.now(timezone.utc))
        for value in ("", "  ", None):
            p = post(); p["question"]["title"] = value
            with self.subTest(title=value), self.assertRaises(e2e.TestBlocked):
                e2e.validate_target(p, 101, 900, datetime.now(timezone.utc))

    def test_official_taxonomy_serializers_do_not_create_outside_scoring_membership(self):
        # Official projects/serializers/common.py serializes these classifications
        # alongside tournaments, keyed by type. Source-derived, not a live fixture.
        p = post()
        for index, kind in enumerate(("category", "topic", "news_category")):
            p["projects"][kind] = [{"id": 901 + index, "slug": "synthetic-label", "name": "Synthetic label", "type": kind}]
        self.assertEqual(e2e.validate_target(p, 101, 900, datetime.now(timezone.utc)).post_id, 101)

    def test_scoring_unknown_malformed_or_mislabeled_memberships_fail_closed(self):
        for kind in ("tournament", "question_series", "index", "site_main", "community", "leaderboard_tag", "personal_project", "unknown"):
            p = post(); p["projects"][kind] = [{"id": 901, "slug": "outside", "type": kind}]
            with self.subTest(kind=kind), self.assertRaises(e2e.TestBlocked):
                e2e.validate_target(p, 101, 900, datetime.now(timezone.utc))
        for entry in [None, {}, {"id": True, "slug": "label", "type": "category"},
                      {"id": 901, "slug": "", "type": "category"},
                      {"id": 901, "slug": "label", "type": "tournament"},
                      {"id": 901, "slug": "label", "type": "category", "score_type": "peer"},
                      {"id": 901, "slug": "label", "type": "category", "primary_leaderboard_id": 3}]:
            p = post(); p["projects"]["category"] = [entry]
            with self.subTest(entry=entry), self.assertRaises(e2e.TestBlocked):
                e2e.validate_target(p, 101, 900, datetime.now(timezone.utc))
        for value in ({"id": 901, "slug": "label", "type": "category"}, None):
            p = post(); p["projects"]["category"] = value
            with self.assertRaises(e2e.TestBlocked):
                e2e.validate_target(p, 101, 900, datetime.now(timezone.utc))
        p = post(); p["projects"]["unknown"] = []
        with self.assertRaises(e2e.TestBlocked):
            e2e.validate_target(p, 101, 900, datetime.now(timezone.utc))

    def test_exact_test_project_and_standalone_open_binary(self):
        self.assertEqual(e2e.validate_project(project()), 900)
        t = target()
        self.assertEqual((t.post_id, t.question_id, t.project_id), (101, 202, 900))
        self.assertEqual(t.url, "https://www.metaculus.com/questions/101/")

    def test_wrong_project_and_visible_extra_membership_are_blocked(self):
        for bad in [project() | {"slug": "live-tournament"}, project() | {"id": True},
                    project() | {"type": "tournament"}]:
            with self.assertRaises(e2e.TestBlocked):
                e2e.validate_project(bad)
        p = post(); p["projects"]["tournament"] = [{"id": 901, "slug": "live-tournament"}]
        with self.assertRaises(e2e.TestBlocked):
            e2e.validate_target(p, 101, 900, datetime.now(timezone.utc))

    def test_nonbinary_group_conditional_and_unapproved_targets_rejected(self):
        cases = []
        for key, value in [("curation_status", "draft"), ("resolved", True),
                           ("user_permission", "viewer"), ("conditional", {}),
                           ("group_of_questions", {}), ("notebook", {})]:
            cases.append(post() | {key: value})
        for key, value in [("type", "numeric"), ("resolution", "yes"),
                           ("status", "closed"), ("id", False), ("post_id", 999), ("resolution_criteria", None)]:
            p = post(); p["question"][key] = value; cases.append(p)
        for p in cases:
            with self.subTest(keys=p.keys()), self.assertRaises(e2e.TestBlocked):
                e2e.validate_target(p, 101, 900, datetime.now(timezone.utc))

    def test_timing_future_actual_close_is_valid_but_past_or_missing_is_not(self):
        target()  # API's synthesized future actual_close_time is valid.
        for object_name in ["post", "question"]:
            for key, value in [("open_time", "2050-01-01T00:00:00Z"),
                               ("open_time", "2020-01-01T00:00:00"),
                               ("scheduled_close_time", None),
                               ("actual_close_time", "2020-01-01T00:00:00Z"),
                               ("actual_resolve_time", "2020-01-01T00:00:00Z")]:
                p = post(); item = p if object_name == "post" else p["question"]; item[key] = value
                with self.subTest(object=object_name, key=key), self.assertRaises(e2e.TestBlocked):
                    e2e.validate_target(p, 101, 900, datetime.now(timezone.utc))

    def test_substantive_question_edit_changes_fingerprint(self):
        original = target(); p = post(); p["question"]["resolution_criteria"] = "Changed condition"
        edited = e2e.validate_target(p, 101, 900, datetime.now(timezone.utc))
        self.assertNotEqual(original.fingerprint, edited.fingerprint)

    def test_empty_history_must_be_explicit_and_correct_question(self):
        client = Mock(); client.get.return_value = empty_history()
        self.assertEqual(e2e.forecast_history(client, 202), [])
        for invalid in [{}, {"results": []}, {"results": [{"question_id": 999, "forecasts": []}]}]:
            client.get.return_value = invalid
            with self.assertRaises(e2e.TestBlocked):
                e2e.forecast_history(client, 202)

    def test_selection_is_ascending_and_validates_details(self):
        client = Mock()
        client.get.side_effect = [project(), {"results": [{"id": 200}, {"id": 101}]}, post(), empty_history()]
        self.assertEqual(e2e.select_target(client).post_id, 101)
        self.assertEqual(client.get.call_args_list[2].args[0], "/api/posts/101/")

    def test_diagnostics_distinguish_empty_rejected_and_previously_forecasted(self):
        bad = post(); bad["projects"]["tournament"] = [{"id": 999, "slug": "outside", "type": "tournament"}]
        scenarios = [([project(), {"results": []}], {}),
            ([project(), {"results": [{"id": 101}]}, bad], {"outside_scoring_membership": 1}),
            ([project(), {"results": [{"id": 101}]}, post(), {"results": [{"question_id": 202, "forecasts": [{}]}]}], {"previously_forecasted": 1})]
        for responses, reasons in scenarios:
            client = Mock(); client.get.side_effect = responses; output = io.StringIO()
            with contextlib.redirect_stdout(output), self.assertRaisesRegex(e2e.TestBlocked, "No eligible"):
                e2e.select_target(client)
            diagnostic = json.loads(output.getvalue())["selection_diagnostics"]
            self.assertEqual(diagnostic["rejection_counts"], reasons)
            self.assertFalse(diagnostic["selected"])
            self.assertNotIn("Synthetic question", output.getvalue())

    def test_diagnostics_never_emit_untrusted_exception_details(self):
        client = Mock(); client.get.side_effect = [project(), {"results": [{"id": 101}]}, post()]
        output = io.StringIO()
        with patch.object(e2e, "validate_target", side_effect=e2e.TestBlocked("PRIVATE KEY OR BODY")), \
             contextlib.redirect_stdout(output), self.assertRaises(e2e.TestBlocked):
            e2e.select_target(client)
        self.assertNotIn("PRIVATE", output.getvalue())
        self.assertEqual(json.loads(output.getvalue())["selection_diagnostics"]["rejection_counts"], {"unclassified_validation_failure": 1})

    def test_selector_failure_is_before_budget_paid_calls_and_write(self):
        client = Mock(); client.get.side_effect = [project(), {"results": []}]
        with patch.object(e2e, "check_end_to_end_gate"), patch.object(e2e, "check_connection", return_value=123), \
             patch.object(e2e, "MetaculusTestClient", return_value=client), patch.object(e2e, "RequestBudget") as budget, \
             patch.object(e2e, "make_forecast") as paid, contextlib.redirect_stdout(io.StringIO()):
            with self.assertRaisesRegex(e2e.TestBlocked, "No eligible"):
                e2e.run_test({"METACULUS_TOKEN": "dummy-test-token"})
        budget.assert_not_called(); paid.assert_not_called(); client.post_once.assert_not_called()

    def test_listing_size_is_bounded_before_detail_fetches(self):
        client = Mock(); client.get.side_effect = [project(), {"results": [{"id": i + 1} for i in range(101)]}]
        with self.assertRaises(e2e.TestBlocked):
            e2e.select_target(client)
        self.assertEqual(client.get.call_count, 2)


class PayloadAndWriteTests(unittest.TestCase):
    def test_exactly_one_forecast_and_private_reasoning_correct_ids(self):
        payload = e2e.make_payload(target(), 0.5, "Reason @someone", "456")
        self.assertEqual(payload["forecasts"], [{"question": 202, "probability_yes": 0.5}])
        self.assertEqual(len(payload["comments"]), 1)
        c = payload["comments"][0]
        self.assertEqual(c["on_post"], 101)
        self.assertTrue(c["is_private"])
        self.assertTrue(c["included_forecast"])
        self.assertEqual(c["text"], "Setup test run 456\n\nIntegration test only; not calibrated research.\n\nReason (at)someone")
        self.assertNotIn("acting_user", payload)

    def test_invalid_probabilities_explanations_and_markers_rejected(self):
        for p in [float("nan"), float("inf"), -1, 0, 1, True, "0.5"]:
            with self.subTest(p=p), self.assertRaises(e2e.TestBlocked):
                e2e.make_payload(target(), p, "Reason", "456")
        for reason, marker in [("", "456"), ("x" * 20_001, "456"), ("Reason", "bad\nmarker")]:
            with self.assertRaises(e2e.TestBlocked):
                e2e.make_payload(target(), 0.5, reason, marker)

    def test_reasoning_whitespace_is_normalized_before_exact_readback(self):
        payload = e2e.make_payload(target(), 0.5, "  Reason with model newline\n  ", "456")
        self.assertEqual(payload["comments"][0]["text"], "Setup test run 456\n\nIntegration test only; not calibrated research.\n\nReason with model newline")
        started = datetime.now(timezone.utc).timestamp()
        client = Mock(); client.get.side_effect = readback_data(payload, started)
        self.assertEqual(e2e.verify_readback(client, target(), payload, 123, started), (True, True))

    def test_single_atomic_write_no_retry_even_timeout_or_redirect(self):
        for response_status in [201, 302, 500, "timeout"]:
            response = Mock(status=response_status); response.read.return_value = b"{}"
            connection = Mock(); connection.getresponse.return_value = response
            if response_status == "timeout":
                connection.getresponse.side_effect = TimeoutError("PRIVATE RESPONSE")
            factory = Mock(return_value=connection)
            client = e2e.MetaculusTestClient("dummy-test-token", factory)
            payload = e2e.make_payload(target(), 0.5, "Reason", "456")
            accepted = client.post_once(payload)
            self.assertEqual(accepted, response_status == 201)
            with self.assertRaises(e2e.TestBlocked):
                client.post_once(payload)
            connection.request.assert_called_once()
            args = connection.request.call_args
            self.assertEqual(args.args, ("POST", "/api/questions/bulk-forecast-comment/"))
            self.assertEqual(json.loads(args.kwargs["body"]), payload)
            factory.assert_called_once_with("www.metaculus.com", timeout=20)
            connection.close.assert_called_once()


class ReadbackTests(unittest.TestCase):
    def test_both_verified_with_private_included_forecast_object(self):
        started = datetime.now(timezone.utc).timestamp()
        payload = e2e.make_payload(target(), 0.5, "Private reason", "456")
        client = Mock(); client.get.side_effect = readback_data(payload, started)
        self.assertEqual(e2e.verify_readback(client, target(), payload, 123, started), (True, True))
        self.assertIn("is_private=true", client.get.call_args.args[0])
        self.assertIn("author=123", client.get.call_args.args[0])

    def test_mismatched_or_missing_comment_is_partial_not_success(self):
        started = datetime.now(timezone.utc).timestamp()
        payload = e2e.make_payload(target(), 0.5, "Private reason", "456")
        for key, value in [("is_private", False), ("text", "other run"),
                           ("included_forecast", True), ("on_post", 202),
                           ("author", {"id": 999}), ("is_soft_deleted", True)]:
            f, c = readback_data(payload, started); c["results"][0][key] = value
            client = Mock(); client.get.side_effect = [f, c]
            with self.subTest(key=key):
                self.assertEqual(e2e.verify_readback(client, target(), payload, 123, started), (True, False))

    def test_wrong_forecast_duplicate_history_and_missing_readback_fail(self):
        started = datetime.now(timezone.utc).timestamp()
        payload = e2e.make_payload(target(), 0.5, "Private reason", "456")
        for mode in ["other_author", "wrong_probability", "duplicate", "missing"]:
            f, c = readback_data(payload, started); row = f["results"][0]["forecasts"][0]
            if mode == "other_author": row["author_id"] = 999
            if mode == "wrong_probability": row["forecast_values"] = [0.1, 0.9]
            if mode == "duplicate": f["results"][0]["forecasts"].append(copy.deepcopy(row))
            if mode == "missing": f = {}
            client = Mock(); client.get.side_effect = [f, c]
            with self.subTest(mode=mode):
                self.assertEqual(e2e.verify_readback(client, target(), payload, 123, started), (False, True))


class SequenceTests(unittest.TestCase):
    def test_gate_and_identity_fail_before_paid_calls(self):
        with patch.object(e2e, "check_end_to_end_gate", side_effect=e2e.TestBlocked("Blocked")), \
             patch.object(e2e, "make_forecast") as forecast:
            with self.assertRaises(e2e.TestBlocked):
                e2e.run_test({})
            forecast.assert_not_called()

    def test_changed_question_stops_before_the_write(self):
        client = Mock(); changed = post(); changed["question"]["resolution_criteria"] = "Changed"
        client.get.side_effect = [project(), changed]
        with patch.object(e2e, "check_end_to_end_gate"), patch.object(e2e, "check_connection", return_value=123), \
             patch.object(e2e, "MetaculusTestClient", return_value=client), \
             patch.object(e2e, "select_target", return_value=target()), \
             patch.object(e2e, "make_forecast", new=AsyncMock(return_value=SimpleNamespace(prediction_value=0.5, reasoning="Reason"))), \
             contextlib.redirect_stdout(io.StringIO()):
            with self.assertRaises(e2e.TestBlocked):
                e2e.run_test({"METACULUS_TOKEN": "dummy-test-token", "GITHUB_RUN_ID": "456"})
        client.post_once.assert_not_called()

    def test_lost_write_response_is_reconciled_read_only_never_resubmitted(self):
        client = Mock(); client.get.side_effect = [project(), post(), empty_history()]
        client.post_once.return_value = False
        with patch.object(e2e, "check_end_to_end_gate"), patch.object(e2e, "check_connection", return_value=123), \
             patch.object(e2e, "MetaculusTestClient", return_value=client), \
             patch.object(e2e, "select_target", return_value=target()), \
             patch.object(e2e, "make_forecast", new=AsyncMock(return_value=SimpleNamespace(prediction_value=0.5, reasoning="Reason"))), \
             patch.object(e2e, "verify_readback", return_value=(True, True)) as readback, \
             contextlib.redirect_stdout(io.StringIO()):
            result = e2e.run_test({"METACULUS_TOKEN": "dummy-test-token", "GITHUB_RUN_ID": "456"})
        client.post_once.assert_called_once()
        readback.assert_called_once()
        self.assertFalse(result["submission_response_confirmed"])
        self.assertTrue(result["forecast_verified"] and result["private_reasoning_verified"])
        self.assertNotIn("probability_yes", result)

    def test_default_cli_is_offline(self):
        with patch("sys.argv", ["metaculus_end_to_end_test.py"]), patch.object(e2e, "run_test") as run, \
             contextlib.redirect_stdout(io.StringIO()):
            e2e.main()
        run.assert_not_called()

    def test_workflow_is_manual_serialized_and_shares_original_concurrency(self):
        text = (Path(__file__).resolve().parents[1] / ".github/workflows/metaculus_end_to_end_test.yaml").read_text()
        self.assertIn("workflow_dispatch:", text)
        self.assertNotIn("schedule:", text)
        self.assertIn("group: metaculus-initial-openai-campaign", text)
        self.assertIn("cancel-in-progress: false", text)
        self.assertNotIn("contents: write", text)
        self.assertNotIn("inputs:", text)
        self.assertNotIn("confirm_one_test_forecast", text)
        self.assertEqual(text.count('INITIAL_OPENAI_TEST_APPROVED: "true"'), 2)
        self.assertEqual(text.count('METACULUS_TEST_SUBMISSION_APPROVED: "true"'), 2)


if __name__ == "__main__":
    unittest.main()
