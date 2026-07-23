from __future__ import annotations

import json
from pathlib import Path
import re
import tempfile
import unittest
from unittest.mock import patch

from host.runtime.tools.tools_host import BUNDLED_TOOLS, validate_against_schema
from tests.smoke.smoke_aws import SMOKE_TOOL_CALLS, AwsSmoke
from tests.stage.stage_aws import StageAwsSmoke, _required_env_path
from tests.stage.stage_support import (
    STAGE_SUITES,
    TOOL_SUITES,
    github_app_config_from_env as _github_app_config_from_env,
    suite_tools,
)


class AwsSmokeTeardownTests(unittest.TestCase):
    def test_precredential_bedrock_probe_runs_real_hermes_launcher(self) -> None:
        smoke = AwsSmoke()
        smoke.total = 0
        smoke.passed = 0
        commands: list[str] = []
        event_reads = 0

        def fake_ssh(command: str) -> str:
            commands.append(command)
            if "SELECT count(*) FROM bedrock_credentials" in command:
                return "0"
            return "expected credential failure"

        def fake_events(since: int = 0) -> list[dict]:
            nonlocal event_reads
            event_reads += 1
            if event_reads == 1:
                return []
            if event_reads == 2:
                return [
                    {
                        "seq": 1,
                        "host": "bedrock-runtime.us-east-1.amazonaws.com",
                        "path": "/model/qwen.qwen3-coder-next/converse",
                        "decision": "denied",
                        "reason_code": "bedrock_credentials_unavailable",
                    }
                ]
            if event_reads == 3:
                return [
                    {
                        "seq": 1,
                        "host": "bedrock-runtime.us-east-1.amazonaws.com",
                        "path": "/model/qwen.qwen3-coder-next/converse",
                        "decision": "denied",
                        "reason_code": "bedrock_credentials_unavailable",
                    }
                ]
            return [
                {
                    "seq": 2,
                    "host": "bedrock-runtime.us-east-1.amazonaws.com",
                    "path": "/model/qwen.qwen3-coder-next/converse",
                    "decision": "denied",
                    "reason_code": "bedrock_credentials_unavailable",
                }
            ]

        with (
            patch.object(smoke, "_api", return_value={}),
            patch.object(smoke, "_ssh_code", side_effect=fake_ssh),
            patch.object(smoke, "_network_events", side_effect=fake_events),
        ):
            smoke.check_precredential_bedrock_harness_launchers()

        joined = "\n".join(commands)
        self.assertIn("sudo -u trustyclaw-admin", joined)
        self.assertEqual(joined.count("--model qwen.qwen3-coder-next"), 1)
        self.assertIn("/usr/local/lib/trustyclaw-host/run-hermes", joined)
        self.assertIn("--model qwen.qwen3-coder-next", joined)
        self.assertEqual(smoke.passed, 1)

    def test_fresh_smoke_uses_strict_deploy_command_and_stdout_result(self) -> None:
        smoke = AwsSmoke()
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            smoke.workdir = tmp_path
            smoke.public_key = "ssh-ed25519 AAAATEST operator@example"
            smoke.ssh_key = str(tmp_path / "operator_key")
            calls: list[list[str]] = []

            class _Proc:
                stdout = json.dumps(
                    {
                        "agent_name": "trustyclaw-smoke",
                        "instance_id": "i-smoke",
                        "region": "us-east-1",
                        "public_dns": "smoke.example.com",
                    }
                )

            def fake_run(args: list[str], **kwargs: object) -> object:
                calls.append(args)
                if kwargs.get("cwd") != tmp_path:
                    raise AssertionError(f"fresh smoke deploy used unexpected cwd: {kwargs.get('cwd')!r}")
                env = kwargs.get("env")
                assert isinstance(env, dict) and env.get("AWS_REGION") == "us-east-1"
                return _Proc()

            with (
                patch.object(smoke, "_destroy_tagged_smoke_resources"),
                patch("tests.smoke.smoke_aws.subprocess.run", side_effect=fake_run),
            ):
                smoke.deploy()

        self.assertEqual(calls[0][1:3], ["-m", "host.cli.deploy"])
        self.assertIn("--agent-name", calls[0])
        self.assertEqual(calls[0][calls[0].index("--agent-name") + 1], "trustyclaw-smoke")
        self.assertIn("--operator-ssh-public-key", calls[0])
        self.assertIn("--admin-password-sha256", calls[0])
        self.assertNotIn("--config", calls[0])
        self.assertNotIn("--result-file", calls[0])
        assert smoke.result is not None
        self.assertEqual(smoke.result["instance_id"], "i-smoke")
        # The harness injects its own generated password for admin API auth.
        self.assertIn("admin_password", smoke.result)

    def test_teardown_destroys_tagged_resources_without_deploy_result(self) -> None:
        smoke = AwsSmoke()
        calls: list[tuple[str, ...]] = []
        instances_terminated = False
        volumes_deleted = set()
        security_group_deleted = False

        def fake_aws(*args: str) -> dict:
            nonlocal instances_terminated, security_group_deleted
            calls.append(args)
            if args[:2] == ("ec2", "describe-instances"):
                states = next((arg for arg in args if arg.startswith("Name=instance-state-name,Values=")), "")
                if not instances_terminated and "shutting-down" not in states:
                    return {"Reservations": [{"Instances": [{"InstanceId": "i-smoke"}]}]}
                return {"Reservations": []}
            if args[:2] == ("ec2", "terminate-instances"):
                instances_terminated = True
                return {}
            if args[:2] == ("ec2", "describe-volumes"):
                volumes = [
                    {"VolumeId": "vol-root", "State": "available"},
                    {"VolumeId": "vol-admin", "State": "available"},
                    {"VolumeId": "vol-agent", "State": "available"},
                ]
                return {"Volumes": [volume for volume in volumes if volume["VolumeId"] not in volumes_deleted]}
            if args[:2] == ("ec2", "delete-volume"):
                volumes_deleted.add(args[args.index("--volume-id") + 1])
                return {}
            if args[:2] == ("ec2", "describe-security-groups"):
                if security_group_deleted:
                    return {"SecurityGroups": []}
                return {"SecurityGroups": [{"GroupId": "sg-smoke"}]}
            if args[:2] == ("ec2", "delete-security-group"):
                security_group_deleted = True
                return {}
            if args[:3] == ("ec2", "wait", "instance-terminated"):
                return {}
            if args[:3] == ("ec2", "wait", "volume-available"):
                return {}
            if args[:3] == ("ec2", "wait", "volume-deleted"):
                return {}
            raise AssertionError(f"unexpected AWS call: {args}")

        smoke._aws = fake_aws  # type: ignore[method-assign]
        smoke.teardown()

        self.assertIn(("ec2", "terminate-instances", "--instance-ids", "i-smoke"), calls)
        self.assertEqual(volumes_deleted, {"vol-root", "vol-admin", "vol-agent"})
        self.assertTrue(security_group_deleted)


class StageAwsSmokeTests(unittest.TestCase):
    def test_stage_rejects_non_stage_result_file(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            result_path = tmp_path / "wrong.json"
            result_path.write_text(
                json.dumps(
                    {
                        "agent_name": "trustyclaw-smoke",
                        "region": "us-east-1",
                        "public_dns": "smoke.example.com",
                        "admin_password": "stable-admin",
                    }
                )
            )
            ssh_key = tmp_path / "stage_operator"
            ssh_key.write_text("private key")

            with self.assertRaisesRegex(AssertionError, "expected 'trustyclaw-stage'"):
                StageAwsSmoke(result_path, ssh_key)

    def test_stage_upgrade_result_requires_admin_password_env(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            result_path = tmp_path / "trustyclaw-stage.json"
            result_path.write_text(
                json.dumps(
                    {
                        "agent_name": "trustyclaw-stage",
                        "region": "us-east-1",
                        "public_dns": "stage.example.com",
                    }
                )
            )
            ssh_key = tmp_path / "stage_operator"
            ssh_key.write_text("private key")

            with (
                patch.dict("os.environ", {}, clear=True),
                self.assertRaisesRegex(AssertionError, "STAGE_ADMIN_PASSWORD is not set or empty"),
            ):
                StageAwsSmoke(result_path, ssh_key, "STAGE_ADMIN_PASSWORD")

    def test_stage_uses_admin_password_env_when_upgrade_result_omits_password(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            result_path = tmp_path / "trustyclaw-stage.json"
            result_path.write_text(
                json.dumps(
                    {
                        "agent_name": "trustyclaw-stage",
                        "region": "us-east-1",
                        "public_dns": "stage.example.com",
                    }
                )
            )
            ssh_key = tmp_path / "stage_operator"
            ssh_key.write_text("private key")
            with patch.dict("os.environ", {"STAGE_ADMIN_PASSWORD": "stable-admin"}):
                smoke = StageAwsSmoke(result_path, ssh_key, "STAGE_ADMIN_PASSWORD")

        self.assertEqual(smoke.result["admin_password"], "stable-admin")

    def test_stage_accepts_start_result_with_admin_password_env(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            result_path = tmp_path / "trustyclaw-stage.json"
            result_path.write_text(
                json.dumps(
                    {
                        "agent_name": "trustyclaw-stage",
                        "operation": "start",
                        "state": "running",
                        "region": "us-east-1",
                        "public_dns": "stage.example.com",
                    }
                )
            )
            ssh_key = tmp_path / "stage_operator"
            ssh_key.write_text("private key")
            with patch.dict("os.environ", {"STAGE_ADMIN_PASSWORD": "stable-admin"}):
                smoke = StageAwsSmoke(result_path, ssh_key, "STAGE_ADMIN_PASSWORD")

        self.assertEqual(smoke.result["admin_password"], "stable-admin")
        self.assertEqual(smoke.result["operation"], "start")

    def test_stage_ssh_key_path_can_come_from_env(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            ssh_key = Path(tmp) / "stage_operator"
            ssh_key.write_text("private key")
            with patch.dict("os.environ", {"STAGE_SSH_KEY": str(ssh_key)}):
                self.assertEqual(_required_env_path("STAGE_SSH_KEY"), ssh_key)

    def test_stage_suite_runtimes_scope_each_suite(self) -> None:
        self.assertEqual(StageAwsSmoke.suite_runtimes("codex"), ("codex",))
        self.assertEqual(StageAwsSmoke.suite_runtimes("claude"), ("claude_code",))
        self.assertEqual(StageAwsSmoke.suite_runtimes("hermes"), ("hermes",))
        self.assertEqual(StageAwsSmoke.suite_runtimes("github"), ())
        self.assertEqual(StageAwsSmoke.suite_runtimes("brave_search"), ())
        self.assertEqual(StageAwsSmoke.suite_runtimes("gmail"), ())
        self.assertEqual(StageAwsSmoke.suite_runtimes("google_calendar"), ())
        self.assertEqual(
            StageAwsSmoke.suite_runtimes("all"),
            ("codex", "claude_code", "hermes"),
        )
        self.assertTrue(set(TOOL_SUITES).issubset(STAGE_SUITES))
        self.assertEqual(suite_tools("all"), TOOL_SUITES)
        self.assertEqual(suite_tools("linkedin"), ("linkedin",))
        self.assertEqual(suite_tools("github"), ())

    def test_stage_autoconfiguration_never_touches_oauth_tools(self) -> None:
        smoke = object.__new__(StageAwsSmoke)
        configured: set[tuple[str, str]] = set()
        calls: list[tuple[str, str, dict | None]] = []

        def fake_api(method: str, path: str, body: dict | None = None) -> dict:
            calls.append((method, path, body))
            if method == "PUT" and body is not None:
                configured.add((path.split("/")[3], body["key"]))
                return {}
            if method == "GET" and path == "/v1/tools":
                return {
                    "tools": [
                        {
                            "tool_id": tool_id,
                            "config": [
                                {"key": requirement.key, "set": (tool_id, requirement.key) in configured}
                                for requirement in BUNDLED_TOOLS[tool_id].manifest.config
                            ],
                        }
                        for tool_id in ("brave_search", "gmail")
                    ]
                }
            if method == "POST":
                return {}
            raise AssertionError((method, path, body))

        smoke._api = fake_api  # type: ignore[method-assign]
        environment = {
            "TRUSTYCLAW_STAGE_BRAVE_SEARCH_API_KEY": "brave-key",
            "TRUSTYCLAW_STAGE_GOOGLE_OAUTH_CLIENT_ID": "must-not-be-read",
            "TRUSTYCLAW_STAGE_GOOGLE_OAUTH_CLIENT_SECRET": "must-not-be-read",
        }
        with patch.dict("os.environ", environment, clear=False):
            smoke.autoconfigure_tools(("brave_search", "gmail"))

        self.assertIn(
            ("PUT", "/v1/tools/brave_search/config", {"key": "BRAVE_SEARCH_API_KEY", "value": "brave-key"}),
            calls,
        )
        self.assertIn(("POST", "/v1/tools/brave_search/enable", {}), calls)
        self.assertFalse(any("/gmail/" in path for _, path, _ in calls))

    def test_stage_oauth_preflight_points_only_to_persistent_host_setup(self) -> None:
        smoke = object.__new__(StageAwsSmoke)
        smoke._api = lambda *_args, **_kwargs: {  # type: ignore[method-assign]
            "tools": [
                {
                    "tool_id": "gmail",
                    "enabled": False,
                    "config": [
                        {"key": "GOOGLE_OAUTH_CLIENT_ID", "set": False},
                        {"key": "GOOGLE_OAUTH_CLIENT_SECRET", "set": False},
                    ],
                    "connection_status": {"connected": False},
                }
            ]
        }
        failures = smoke._tool_credential_failures("gmail")
        self.assertEqual(len(failures), 1)
        self.assertIn("stage admin UI", failures[0])
        self.assertIn("connect its stage account once", failures[0])
        self.assertIn("enable the tool", failures[0])
        self.assertNotIn("TRUSTYCLAW_STAGE_GOOGLE", failures[0])

    def test_fresh_smoke_has_valid_input_for_every_bundled_action(self) -> None:
        covered = {
            f"{tool_id}_{action_id}"
            for tool_id, calls in SMOKE_TOOL_CALLS.items()
            for action_id, _arguments in calls
        }
        covered.update(
            {
                "polymarket_get_market",
                "polymarket_get_order_book",
                "polymarket_price_history",
            }
        )
        declared = {
            f"{tool_id}_{action.id}"
            for tool_id, tool in BUNDLED_TOOLS.items()
            for action in tool.manifest.actions
        }
        self.assertEqual(covered, declared)
        for tool_id, calls in SMOKE_TOOL_CALLS.items():
            for action_id, arguments in calls:
                spec = BUNDLED_TOOLS[tool_id].manifest.action(action_id)
                self.assertIsNotNone(spec)
                assert spec is not None
                self.assertEqual(
                    validate_against_schema(arguments, spec.input_schema),
                    "",
                    f"invalid smoke input for {tool_id}_{action_id}",
                )
        for action_id, arguments in (
            ("get_market", {"market_id": "1"}),
            ("get_order_book", {"token_id": "1"}),
            ("price_history", {"token_id": "1", "interval": "1d"}),
        ):
            spec = BUNDLED_TOOLS["polymarket"].manifest.action(action_id)
            assert spec is not None
            self.assertEqual(
                validate_against_schema(arguments, spec.input_schema),
                "",
                f"invalid smoke input for polymarket_{action_id}",
            )

    def test_github_app_config_from_env_parses_or_requires_all(self) -> None:
        keys = (
            "STAGE_GITHUB_WRITE_REPO",
            "STAGE_GITHUB_APP_ID",
            "STAGE_GITHUB_APP_INSTALLATION_ID",
            "STAGE_GITHUB_APP_PRIVATE_KEY",
        )
        with patch.dict("os.environ", {key: "" for key in keys}, clear=False):
            self.assertEqual(_github_app_config_from_env(), (None, None))
        full = {
            "STAGE_GITHUB_WRITE_REPO": "infiloop2/sandbox",
            "STAGE_GITHUB_APP_ID": "123",
            "STAGE_GITHUB_APP_INSTALLATION_ID": "456",
            "STAGE_GITHUB_APP_PRIVATE_KEY": "-----BEGIN KEY-----\nx\n-----END KEY-----",
        }
        with patch.dict("os.environ", full, clear=False):
            config, error = _github_app_config_from_env()
        self.assertIsNone(error)
        assert config is not None
        self.assertEqual(config["owner"], "infiloop2")
        self.assertEqual(config["repo"], "sandbox")
        self.assertEqual(config["app_id"], "123")
        self.assertEqual(config["installation_id"], "456")
        with patch.dict("os.environ", {**full, "STAGE_GITHUB_APP_ID": ""}, clear=False):
            partial, partial_error = _github_app_config_from_env()
        self.assertIsNone(partial)
        self.assertIn("STAGE_GITHUB_APP_ID", partial_error or "")

    def test_stage_enforcement_policy_lists_stage_repo_first(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            result_path = Path(tmp) / "trustyclaw-stage.json"
            result_path.write_text(
                json.dumps({"agent_name": "trustyclaw-stage", "region": "us-east-1", "public_dns": "stage.example.com"})
            )
            ssh_key = Path(tmp) / "stage_operator"
            ssh_key.write_text("private key")
            smoke = StageAwsSmoke(result_path, ssh_key)
        smoke.stage_github_repositories = [{"owner": "sandbox-owner", "repo": "sandbox"}]
        repos = smoke.enforcement_policy()["network_integrations"]["github"]["write_repositories"]
        self.assertEqual(repos[0], {"owner": "sandbox-owner", "repo": "sandbox"})
        self.assertIn({"owner": "infiloop2", "repo": "trustyclaw"}, repos)


class WorkflowSmokeTests(unittest.TestCase):
    def test_stage_workflows_use_first_class_power_cli(self) -> None:
        stage = Path(".github/workflows/trustyclaw-stage.yml").read_text()
        stage_start = Path(".github/workflows/trustyclaw-stage-start.yml").read_text()
        stage_stop = Path(".github/workflows/trustyclaw-stage-stop.yml").read_text()

        self.assertIn("python3 -m host.cli.start", stage)
        self.assertIn("python3 -m host.cli.stop", stage)
        self.assertIn("same_version_failure=${same_version_failure}", stage)
        self.assertIn(
            "steps.upgrade_stage.outcome == 'failure' && steps.upgrade_stage.outputs.same_version_failure == 'true'",
            stage,
        )
        self.assertIn("steps.upgrade_stage.outcome == 'failure' && steps.start_current.outcome != 'success'", stage)
        self.assertIn("python3 -m host.cli.start", stage_start)
        self.assertIn("python3 -m host.cli.stop", stage_stop)
        # The CLI takes flags and prints its result to stdout; the workflows
        # write no config files and redirect stdout to the step artifact.
        for workflow in (stage, stage_start, stage_stop):
            self.assertNotIn("--config", workflow)
            self.assertNotIn("config.json", workflow)
        self.assertIn("--agent-name trustyclaw-stage", stage_start)
        self.assertIn("--agent-name trustyclaw-stage", stage_stop)
        self.assertIn("--operator-ssh-public-key", stage)
        self.assertIn("> trustyclaw-stage.json", stage)
        removed_action = "start-stage" + "-instance"
        self.assertNotIn(removed_action, stage)
        self.assertNotIn(removed_action, stage_start)
        self.assertNotIn(removed_action, stage_stop)

    def test_stage_workflow_exposes_only_enable_only_tool_secrets(self) -> None:
        stage = Path(".github/workflows/trustyclaw-stage.yml").read_text()
        for option in ("all", *TOOL_SUITES, "claude", "codex", "github"):
            self.assertIn(f"- {option}", stage)
        self.assertIn("--suite", stage)
        self.assertIn("--summary-file stage-integration-summary.md", stage)
        self.assertIn('cat stage-integration-summary.md >> "${GITHUB_STEP_SUMMARY}"', stage)
        for env_name in (
            "STAGE_GITHUB_WRITE_REPO",
            "STAGE_GITHUB_APP_ID",
            "STAGE_GITHUB_APP_INSTALLATION_ID",
            "STAGE_GITHUB_APP_PRIVATE_KEY",
        ):
            self.assertIn(env_name, stage)
        for env_name in (
            "STAGE_BEDROCK_AWS_ACCESS_KEY_ID",
            "STAGE_BEDROCK_AWS_SECRET_ACCESS_KEY",
            "TRUSTYCLAW_STAGE_BEDROCK_AWS_ACCESS_KEY_ID",
            "TRUSTYCLAW_STAGE_BEDROCK_AWS_SECRET_ACCESS_KEY",
        ):
            self.assertIn(env_name, stage)
        for tool_id in TOOL_SUITES:
            for requirement in BUNDLED_TOOLS[tool_id].manifest.config:
                env_name = f"TRUSTYCLAW_STAGE_{requirement.key}"
                mapping = f"{env_name}: ${{{{ secrets.{env_name} }}}}"
                if BUNDLED_TOOLS[tool_id].manifest.connection == "oauth":
                    self.assertNotIn(mapping, stage)
                else:
                    self.assertIn(mapping, stage)

    def test_fresh_smoke_workflow_uses_fresh_smoke_script(self) -> None:
        smoke = Path(".github/workflows/trustyclaw-smoke.yml").read_text()

        self.assertIn("playwright==1.60.0", smoke)
        self.assertIn("playwright==1.60.0", Path("tests/requirements.txt").read_text())
        self.assertIn('"${RUNNER_TEMP}/trustyclaw-smoke-venv/bin/python" tests/smoke/smoke_aws.py', smoke)
        self.assertLess(smoke.index("playwright==1.60.0"), smoke.index("AWS_ACCESS_KEY_ID"))
        self.assertIn("context trustyclaw-smoke", smoke)
        self.assertIn("github.event_name == 'workflow_dispatch'", smoke)
        self.assertIn("Fresh AWS smoke is already running; wait for the previous smoke to complete.", smoke)
        self.assertIn("for status in queued in_progress", smoke)
        self.assertIn("group: trustyclaw-smoke", smoke)
