"""Focused tests for the persistent stage matrix orchestration."""

from __future__ import annotations

from http import HTTPStatus
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from host.runtime.tools.tools_host import BUNDLED_TOOLS
from tests.stage.stage_tool_checks import _result_shape, _safe_arguments
from tests.stage.stage_aws import StageAwsSmoke
from tests.stage.stage_support import (
    CHEAP_EFFORT,
    CHEAP_MODELS,
    CredentialUnavailable,
    STAGE_BEDROCK_ENV,
    STAGE_GITHUB_APP_ENV,
    TOOL_SUITES,
    StageReport,
    agent_catalog_tool_ids as _agent_catalog_tool_ids,
    bedrock_credential_from_env as _bedrock_credential_from_env,
    github_app_config_from_env as _github_app_config_from_env,
    record_check as _record_check,
    selected_integrations as _selected_integrations,
    write_action_summary as _write_action_summary,
)


class StageOrchestrationTests(unittest.TestCase):
    def test_stage_harness_import_does_not_require_playwright(self) -> None:
        script = """
import builtins

real_import = builtins.__import__

def import_without_playwright(name, *args, **kwargs):
    if name == "playwright" or name.startswith("playwright."):
        raise ModuleNotFoundError("playwright intentionally unavailable")
    return real_import(name, *args, **kwargs)

builtins.__import__ = import_without_playwright
import tests.stage.stage_aws
"""
        result = subprocess.run(
            [sys.executable, "-c", script],
            cwd=Path(__file__).resolve().parents[1],
            capture_output=True,
            text=True,
            check=False,
        )
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_all_selects_every_runtime_github_and_bundled_tool(self) -> None:
        selected = _selected_integrations("all")
        self.assertEqual(selected[:5], ("codex", "claude", "pi", "hermes", "github"))
        self.assertEqual(selected[5:], TOOL_SUITES)
        self.assertEqual(set(TOOL_SUITES), set(BUNDLED_TOOLS))

    def test_agent_catalog_parser_requires_unique_string_tool_ids(self) -> None:
        output = '```json\n{"tools":["twitter","gmail"]}\n```'
        self.assertEqual(_agent_catalog_tool_ids(output), ("gmail", "twitter"))
        with self.assertRaisesRegex(AssertionError, "repeated tool ids"):
            _agent_catalog_tool_ids('{"tools":["gmail","gmail"]}')
        with self.assertRaisesRegex(AssertionError, "string tools list"):
            _agent_catalog_tool_ids('{"tools":[1]}')

    def test_tool_diagnostics_redact_credentials_but_keep_domain_ids(self) -> None:
        safe = _safe_arguments(
            {
                "api_key": "top-secret",
                "access_token": "also-secret",
                "token_id": "12345",
                "query": "public query",
            }
        )
        self.assertEqual(
            safe,
            {
                "api_key": "<redacted>",
                "access_token": "<redacted>",
                "token_id": "12345",
                "query": "public query",
            },
        )

    def test_tool_result_diagnostics_report_shape_without_values(self) -> None:
        self.assertEqual(
            _result_shape(
                {
                    "status": "success_executed",
                    "message": "provider detail",
                    "reels": [{"url": "https://example.com/private-result"}],
                    "metadata": {"cursor": "secret-ish-provider-value"},
                }
            ),
            "message,metadata{1},reels[1],status",
        )

    def test_brave_stage_retries_one_provider_5xx(self) -> None:
        stage = StageAwsSmoke.__new__(StageAwsSmoke)
        with patch.object(
            stage,
            "_successful_tool_call",
            side_effect=[AssertionError("Brave Search API returned HTTP 500."), {}],
        ) as call:
            detail = stage._check_brave_live()
        self.assertEqual(detail, "live search completed")
        self.assertEqual(call.call_count, 2)

    def test_brave_stage_does_not_retry_non_5xx_failures(self) -> None:
        stage = StageAwsSmoke.__new__(StageAwsSmoke)
        with patch.object(
            stage,
            "_successful_tool_call",
            side_effect=AssertionError("Brave Search API rate limit was reached."),
        ) as call:
            with self.assertRaisesRegex(AssertionError, "rate limit"):
                stage._check_brave_live()
        self.assertEqual(call.call_count, 1)

    def test_instagram_stage_prefers_trending_reel_for_dependent_reads(self) -> None:
        stage = StageAwsSmoke.__new__(StageAwsSmoke)
        calls: list[tuple[str, dict]] = []

        def successful(name: str, arguments: dict) -> dict:
            calls.append((name, arguments))
            if name == "instagram_discovery_get_trending_reels":
                return {
                    "reels": [
                        {
                            "url": "https://www.instagram.com/reel/Fresh/",
                            "audio_id": "123",
                        }
                    ]
                }
            if name == "instagram_discovery_search_reels":
                return {"reels": [{"url": "https://www.instagram.com/reel/Stale/"}]}
            return {}

        with patch.object(stage, "_successful_tool_call", side_effect=successful):
            detail = stage._check_instagram_discovery_live()

        self.assertIn("2 provider-result-derived read(s)", detail)
        self.assertIn(
            (
                "instagram_discovery_get_reel_details",
                {"url": "https://www.instagram.com/reel/Fresh/"},
            ),
            calls,
        )

    def test_runway_stage_uses_uuid_shaped_missing_task(self) -> None:
        stage = StageAwsSmoke.__new__(StageAwsSmoke)
        with patch.object(
            stage,
            "_shim_tool_response",
            return_value=({"isError": True}, "Runway task was not found."),
        ) as call:
            detail = stage._check_runway_live()
        self.assertEqual(
            call.call_args.args,
            ("runway_get_task", {"task_id": "00000000-0000-4000-8000-000000000000"}),
        )
        self.assertIn("without generation spend", detail)

    def test_github_stage_secrets_are_optional_but_partial_sets_are_unavailable(self) -> None:
        with patch.dict(os.environ, {}, clear=True):
            self.assertEqual(_github_app_config_from_env(), (None, None))

        env = {STAGE_GITHUB_APP_ENV["app_id"]: "123"}
        with patch.dict(os.environ, env, clear=True):
            config, error = _github_app_config_from_env()
        self.assertIsNone(config)
        self.assertIn(STAGE_GITHUB_APP_ENV["private_key"], error or "")

    def test_complete_github_stage_secrets_are_parsed(self) -> None:
        env = {
            STAGE_GITHUB_APP_ENV["write_repo"]: "infiloop2/trustyclaw-stage",
            STAGE_GITHUB_APP_ENV["app_id"]: "123",
            STAGE_GITHUB_APP_ENV["installation_id"]: "456",
            STAGE_GITHUB_APP_ENV["private_key"]: "key",
        }
        with patch.dict(os.environ, env, clear=True):
            config, error = _github_app_config_from_env()
        self.assertIsNone(error)
        self.assertEqual(
            config,
            {
                "owner": "infiloop2",
                "repo": "trustyclaw-stage",
                "app_id": "123",
                "installation_id": "456",
                "private_key_pem": "key",
            },
        )

    def test_bedrock_stage_secret_is_one_optional_pair(self) -> None:
        with patch.dict(os.environ, {}, clear=True):
            self.assertEqual(_bedrock_credential_from_env(), (None, None))
        with patch.dict(os.environ, {STAGE_BEDROCK_ENV[0]: "AKIASTAGE"}, clear=True):
            credential, error = _bedrock_credential_from_env()
        self.assertIsNone(credential)
        self.assertIn(STAGE_BEDROCK_ENV[1], error or "")
        with patch.dict(
            os.environ,
            {
                STAGE_BEDROCK_ENV[0]: "AKIASTAGE",
                STAGE_BEDROCK_ENV[1]: "stage-secret",
            },
            clear=True,
        ):
            credential, error = _bedrock_credential_from_env()
        self.assertIsNone(error)
        self.assertEqual(credential, ("AKIASTAGE", "stage-secret"))

    def test_bedrock_autoconfiguration_validates_once_then_enables_both_harnesses(self) -> None:
        stage = StageAwsSmoke.__new__(StageAwsSmoke)
        stage.total = 0
        stage.passed = 0
        stage.stage_bedrock_credential = ("AKIASTAGE", "stage-secret")
        stage.bedrock_secret_error = None
        calls: list[tuple[str, str, dict | None]] = []

        def fake_api(method: str, path: str, body: dict | None = None) -> dict:
            calls.append((method, path, body))
            if (method, path) == ("POST", "/v1/agent-runtime/bedrock-credentials"):
                return {"status": "accepted"}
            if (method, path) == ("GET", "/v1/network/policy"):
                return {
                    "network_controls": {
                        "network_integrations": {"github": {"enabled": True}}
                    }
                }
            if (method, path) == ("PUT", "/v1/network/policy"):
                return {}
            raise AssertionError((method, path, body))

        with (
            patch.object(stage, "_api", side_effect=fake_api),
            patch.object(stage, "_wait_for_runtime_status", return_value="active") as wait,
        ):
            stage.autoconfigure_bedrock("all")

        self.assertEqual(calls[0][:2], ("POST", "/v1/agent-runtime/bedrock-credentials"))
        self.assertEqual(
            calls[0][2],
            {
                "access_key_id": "AKIASTAGE",
                "secret_access_key": "stage-secret",
                "region": "us-east-1",
            },
        )
        self.assertEqual(calls[1][:2], ("GET", "/v1/network/policy"))
        policy = calls[2][2]
        assert policy is not None
        self.assertEqual(
            policy["network_integrations"]["bedrock"],
            {"enabled": True},
        )
        self.assertIn("github", policy["network_integrations"])
        self.assertEqual(
            [call.kwargs["runtime"] for call in wait.call_args_list],
            ["pi", "hermes"],
        )

    def test_bedrock_autoconfiguration_reports_rejected_candidate_to_preflight(self) -> None:
        stage = StageAwsSmoke.__new__(StageAwsSmoke)
        stage.total = 0
        stage.passed = 0
        stage.stage_bedrock_credential = ("AKIASTAGE", "invalid-secret")
        stage.bedrock_secret_error = None
        with patch.object(stage, "_api", side_effect=RuntimeError("STS denied")):
            stage.autoconfigure_bedrock("pi")
        self.assertIn("STS denied", stage.bedrock_secret_error or "")

    def test_all_preflight_returns_each_unavailable_integration_without_raising(self) -> None:
        stage = StageAwsSmoke.__new__(StageAwsSmoke)
        stage.total = 0
        stage.passed = 0
        stage.bedrock_secret_error = None
        with (
            patch.object(
                stage,
                "_wait_for_runtime_status",
                side_effect=["active", "awaiting_login", "active", "active"],
            ) as wait,
            patch.object(stage, "_github_config_failures", return_value=["credential validation is 'error'"]),
            patch.object(stage, "_tool_credential_failures", side_effect=lambda tool: [] if tool == "polymarket" else [f"{tool}: missing"]),
        ):
            availability = stage.integration_availability("all")

        self.assertIsNone(availability["codex"])
        self.assertIn("awaiting_login", availability["claude"] or "")
        self.assertIsNone(availability["pi"])
        self.assertIsNone(availability["hermes"])
        self.assertIn("validation", availability["github"] or "")
        self.assertIsNone(availability["polymarket"])
        self.assertEqual(stage.passed, stage.total)
        self.assertTrue(
            all(
                "awaiting_login" in call.args[0]
                for call in wait.call_args_list
            )
        )

    def test_stage_task_body_always_defaults_to_the_cheapest_model_and_effort(self) -> None:
        self.assertEqual(
            CHEAP_MODELS,
            {
                "codex": "gpt-5.6-luna",
                "claude_code": "sonnet",
                "pi": "qwen.qwen3-coder-next",
                "hermes": "qwen.qwen3-coder-next",
            },
        )
        self.assertEqual(CHEAP_EFFORT, "high")
        stage = StageAwsSmoke.__new__(StageAwsSmoke)
        stage.thread_prefix = "stage-test-"
        stage.agent_runtime = "codex"
        codex = stage.task_body("test", "codex")
        claude = stage.task_body("test", "claude", runtime="claude_code")
        pi = stage.task_body("test", "pi", runtime="pi")
        hermes = stage.task_body("test", "hermes", runtime="hermes")
        self.assertEqual((codex["model"], codex["effort"]), (CHEAP_MODELS["codex"], CHEAP_EFFORT))
        self.assertEqual(
            (claude["model"], claude["effort"]),
            (CHEAP_MODELS["claude_code"], CHEAP_EFFORT),
        )
        self.assertEqual((pi["model"], pi["effort"]), (CHEAP_MODELS["pi"], CHEAP_EFFORT))
        self.assertEqual(
            (hermes["model"], hermes["effort"]),
            (CHEAP_MODELS["hermes"], CHEAP_EFFORT),
        )

    def test_hermes_stage_reuses_kill_task_to_prove_nested_steering_denial(self) -> None:
        stage = StageAwsSmoke.__new__(StageAwsSmoke)
        stage.total = 0
        stage.passed = 0
        stage.agent_runtime = "hermes"
        expected_error = (
            "Hermes tasks do not support steering; create a new task on the same thread_id"
        )
        with (
            patch.object(stage, "task_body", return_value={"input_message": "slow"}),
            patch.object(stage, "follow_up_body", return_value={"input_message": "follow"}),
            patch.object(
                stage,
                "_api",
                side_effect=[{"task_id": "task_1"}, {"task_id": "task_2"}],
            ),
            patch.object(stage, "_wait_for_task_status", return_value={"status": "running"}),
            patch.object(
                stage,
                "_api_status",
                side_effect=[
                    (HTTPStatus.CONFLICT, {"error": {"message": expected_error}}),
                    (HTTPStatus.OK, {"status": "accepted"}),
                ],
            ) as api_status,
            patch.object(
                stage,
                "_wait_for_task",
                side_effect=[
                    {"status": "cancelled"},
                    {"status": "completed", "output_message": "SURVIVED"},
                ],
            ),
            patch.object(stage, "_ssh_code", return_value="not-found") as ssh_code,
        ):
            stage.check_agent_kill_and_thread_survival(expect_steering_denied=True)

        self.assertEqual(api_status.call_args_list[0].args[1], "/v1/tasks/task_1/steer")
        self.assertEqual(api_status.call_args_list[1].args[1], "/v1/tasks/task_1/kill")
        # The kill check asserts the thread's scope unit is gone from systemd.
        self.assertIn("trustyclaw-agent-thread-smoke-kill-hermes.scope", ssh_code.call_args.args[0])
        self.assertEqual((stage.passed, stage.total), (1, 1))

    def test_report_distinguishes_failure_from_unavailable_skip(self) -> None:
        report = StageReport("all")
        self.assertTrue(_record_check(report, "codex", lambda: None, "ok"))

        def unavailable() -> None:
            raise CredentialUnavailable("expired")

        self.assertFalse(_record_check(report, "gmail", unavailable, "unused"))
        self.assertFalse(
            _record_check(
                report,
                "twitter",
                unavailable,
                "unused",
                skip_unavailable=False,
            )
        )

        def failed() -> None:
            raise AssertionError("provider broke")

        self.assertFalse(_record_check(report, "github", failed, "unused"))
        self.assertTrue(report.failed())
        markdown = report.markdown()
        self.assertIn("| Gmail | unavailable | skipped | expired |", markdown)
        self.assertIn("| X (Twitter) | unavailable | failed | expired |", markdown)
        self.assertIn("| GitHub | available | failed | AssertionError: provider broke |", markdown)

    def test_live_rejected_credential_is_reclassified_as_unavailable(self) -> None:
        stage = StageAwsSmoke.__new__(StageAwsSmoke)
        response = {
            "result": {
                "isError": True,
                "content": [{"text": "Brave Search API rejected the configured API key."}],
            }
        }
        with patch.object(stage, "_shim_call", return_value=response):
            with self.assertRaisesRegex(CredentialUnavailable, "rejected the configured API key"):
                stage._shim_tool_result("brave_search_search_web", {"query": "TrustyClaw"})

    def test_action_summary_appends_markdown(self) -> None:
        report = StageReport("all")
        report.add("Tool | escaped", "available", "passed", "line one\nline | two")
        with tempfile.TemporaryDirectory() as directory:
            summary = Path(directory) / "summary.md"
            summary.write_text("existing\n", encoding="utf-8")
            with patch.dict(os.environ, {"GITHUB_STEP_SUMMARY": str(summary)}):
                _write_action_summary(report)
            text = summary.read_text(encoding="utf-8")
        self.assertTrue(text.startswith("existing\n## Stage integration results"))
        self.assertIn("Tool \\| escaped", text)
        self.assertIn("line one line \\| two", text)

    def test_explicit_summary_file_is_ready_for_the_final_workflow_step(self) -> None:
        report = StageReport("all")
        report.add("Codex", "available", "passed", "ok")
        with tempfile.TemporaryDirectory() as directory:
            summary = Path(directory) / "integration-summary.md"
            _write_action_summary(report, summary)
            text = summary.read_text(encoding="utf-8")
        self.assertTrue(text.startswith("## Stage integration results"))


if __name__ == "__main__":
    unittest.main()
