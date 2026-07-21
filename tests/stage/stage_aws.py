#!/usr/bin/env python3
"""Persistent staging test for an already deployed TrustyClaw host.

Unlike the smoke test, this does not deploy or tear down the host. It assumes a
stage host was upgraded/recovered with stable admin and agent data volumes.
The ``all`` suite checks every integration's credentials before testing: an
unconfigured or expired integration is reported and skipped without hiding
results for configured integrations. Focused suites remain strict and fail
when their selected integration is unavailable.
"""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import sys
import tempfile
import time
from urllib.parse import quote

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT))

from tests.smoke.smoke_aws import SMOKE_RUNTIMES
from tests.stage.stage_bedrock_checks import StageBedrockChecks
from tests.stage.stage_integration_checks import StageIntegrationChecks
from tests.stage.stage_tool_checks import StageToolChecks
from tests.stage.stage_support import (
    CHEAP_EFFORT,
    CHEAP_MODELS,
    RUNTIME_LABELS as _RUNTIME_LABELS,
    STAGE_AGENT_NAME,
    STAGE_SUITES,
    TOOL_SUITES,
    StageReport,
    bedrock_credential_from_env as _bedrock_credential_from_env,
    github_app_config_from_env as _github_app_config_from_env,
    integration_label as _integration_label,
    record_check as _record_check,
    suite_tools,
    write_action_summary as _write_action_summary,
)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--result-file", required=True, help="Result JSON from the stage deploy, upgrade, recover, or start run.")
    ssh_key_group = parser.add_mutually_exclusive_group(required=True)
    ssh_key_group.add_argument("--ssh-key", help="Private SSH key path for the persistent stage operator key.")
    ssh_key_group.add_argument(
        "--ssh-key-env",
        help="Environment variable containing the private SSH key path for the persistent stage operator key.",
    )
    parser.add_argument(
        "--admin-password-env",
        help="Environment variable containing the stage admin password when the result file omits it.",
    )
    parser.add_argument(
        "--summary-file",
        type=Path,
        help="Write the integration result table here for the workflow's final Actions summary step.",
    )
    parser.add_argument(
        "--suite",
        choices=STAGE_SUITES,
        default="all",
        help=(
            "Which test suite to run. 'claude', 'codex', 'pi', 'hermes', or 'github' run that integration's "
            "checks only (plus the shared preamble); each bundled tool id runs that tool's "
            "live check; 'all' (default) checks credentials for every integration first, "
            "skips unavailable integrations, and runs every available integration "
            "independently. A focused suite still fails when its required credential is absent."
        ),
    )
    args = parser.parse_args(argv)

    ssh_key = Path(args.ssh_key) if args.ssh_key is not None else _required_env_path(args.ssh_key_env)
    suite = args.suite
    report = StageReport(suite)
    stage = StageAwsSmoke(Path(args.result_file), ssh_key, args.admin_password_env)
    selected_tools = suite_tools(suite)
    run_network_baseline = 0
    try:
        stage.open_tunnel()
        # One optional credential configures the shared Bedrock provider. The
        # POST validates it synchronously before the provider is enabled.
        stage.autoconfigure_bedrock(suite)
        # Install the GitHub App credential and sandbox write repo from CI
        # secrets when present, so the GitHub checks need no manual operator
        # setup. A no-op when the secrets are absent or GitHub is out of scope.
        stage.autoconfigure_github(suite)
        stage.autoconfigure_tools(selected_tools)
        availability = stage.integration_availability(suite)
        unavailable = {name: reason for name, reason in availability.items() if reason is not None}
        if suite != "all" and unavailable:
            reason = next(iter(unavailable.values()))
            assert reason is not None
            report.add(_integration_label(suite), "unavailable", "failed", reason)
            raise AssertionError(reason)
        for integration, reason in unavailable.items():
            assert reason is not None
            report.add(_integration_label(integration), "unavailable", "skipped", reason)

        ready_runtimes = tuple(
            runtime
            for integration, runtime in (
                ("codex", "codex"),
                ("claude", "claude_code"),
                ("pi", "pi"),
                ("hermes", "hermes"),
            )
            if availability.get(integration) is None and integration in availability
        )
        stage.recover_baseline(ready_runtimes)
        run_network_baseline = max(
            (event["seq"] for event in stage._network_events()),
            default=0,
        )

        try:
            stage.check_health()
            stage.check_ui_page()
            stage.check_admin_auth()
            stage.check_agent_file_explorer()
            stage.check_network_event_prune_race()
        except Exception as exc:
            report.add("Core host", "n/a", "failed", f"{type(exc).__name__}: {exc}")
            for integration, reason in availability.items():
                if reason is None:
                    report.add(
                        _integration_label(integration),
                        "available",
                        "skipped",
                        "not run because the shared host checks failed",
                    )
            raise
        report.add("Core host", "n/a", "passed", "health, UI, auth, files, and event pruning")

        passed_runtimes: list[str] = []
        if availability.get("codex") is None and "codex" in availability:
            def check_codex() -> None:
                stage.agent_runtime = "codex"
                stage.check_task()
                stage.check_agent_mcp_catalog("codex")
                stage.check_agent_steering()
                stage.check_agent_kill_and_thread_survival()

            if _record_check(report, "codex", check_codex, "guards, MCP catalog, tasks, steering, and kill recovery"):
                passed_runtimes.append("codex")
        if availability.get("claude") is None and "claude" in availability:
            def check_claude() -> None:
                stage.agent_runtime = "claude_code"
                stage.check_claude_auth_and_task()
                stage.check_agent_mcp_catalog("claude_code")
                stage.check_agent_steering()
                stage.check_agent_kill_and_thread_survival()

            if _record_check(report, "claude", check_claude, "guards, MCP catalog, tasks, steering, and kill recovery"):
                passed_runtimes.append("claude_code")
        if availability.get("pi") is None and "pi" in availability:
            def check_pi() -> None:
                stage.agent_runtime = "pi"
                stage.check_bedrock_auth_and_task()
                stage.check_agent_mcp_catalog("pi")
                stage.check_agent_steering()
                stage.check_agent_kill_and_thread_survival()

            if _record_check(
                report,
                "pi",
                check_pi,
                "shared credential boundary, real task, MCP catalog, session resume, steering, and kill recovery",
            ):
                passed_runtimes.append("pi")
        if availability.get("hermes") is None and "hermes" in availability:
            def check_hermes() -> None:
                stage.agent_runtime = "hermes"
                stage.check_bedrock_auth_and_task()
                stage.check_agent_mcp_catalog("hermes")
                stage.check_agent_kill_and_thread_survival(expect_steering_denied=True)

            if _record_check(
                report,
                "hermes",
                check_hermes,
                "shared credential boundary, real task, MCP catalog, session resume, steering denial, and kill recovery",
            ):
                passed_runtimes.append("hermes")

        if suite == "all":
            if {"pi", "hermes"}.issubset(passed_runtimes):
                _record_check(
                    report,
                    "bedrock_shared",
                    stage.check_bedrock_disable_stages_credential_for_reenable,
                    "one provider toggle deactivated and restored both harnesses",
                )
            else:
                report.add(
                    "Shared AWS Bedrock",
                    "unavailable",
                    "skipped",
                    "requires successful Pi and Hermes integration checks",
                )

        if suite == "all":
            if passed_runtimes == list(SMOKE_RUNTIMES):
                def check_cross_runtime() -> None:
                    stage.check_both_runtimes_active()
                    stage.check_agent_parallelism()
                    stage.check_agent_thread_recall()
                    stage.check_runtime_deactivation_stops_running_tasks()
                    stage.check_reboot_recovery()

                _record_check(
                    report,
                    "runtime_interoperability",
                    check_cross_runtime,
                    "mixed concurrency, recall, deactivation, and reboot recovery",
                )
            else:
                report.add(
                    "Runtime interoperability",
                    "unavailable",
                    "skipped",
                    "requires successful Codex, Claude Code, Pi, and Hermes integration checks",
                )

        if availability.get("github") is None and "github" in availability:
            _record_check(report, "github", stage.check_github_write_e2e, "authenticated read/write and fail-closed guards")

        for tool_id in selected_tools:
            if availability.get(tool_id) is not None:
                continue
            _record_check(
                report,
                tool_id,
                lambda tool_id=tool_id: stage.check_tool_live(tool_id),
                "deterministic live MCP coverage",
                skip_unavailable=suite == "all",
            )

        print(f"\n{stage.passed}/{stage.total} checks passed")
        print(f"suite: {suite}")
        failed = report.failed() or stage.passed != stage.total
        if failed:
            stage.print_configuration_snapshot()
            stage.print_network_events(
                "Network events during failed integration run",
                since=run_network_baseline,
            )
        return 1 if failed else 0
    except Exception as exc:  # noqa: BLE001 - report failure with network + config context
        stage.print_configuration_snapshot()
        stage.print_network_events("Network events before failure", since=run_network_baseline)
        print(f"\n[FAIL] {exc}", file=sys.stderr)
        return 1
    finally:
        _write_action_summary(report, args.summary_file)
        stage.close_tunnel()


def _required_env_path(env_name: str) -> Path:
    value = os.environ.get(env_name)
    if not value:
        raise SystemExit(f"{env_name} is not set or empty")
    return Path(value)


class StageAwsSmoke(StageToolChecks, StageBedrockChecks, StageIntegrationChecks):
    def __init__(self, result_file: Path, ssh_key: Path, admin_password_env: str | None = None) -> None:
        super().__init__()
        result = json.loads(result_file.read_text())
        if not isinstance(result, dict):
            raise AssertionError("stage result file must contain a JSON object")
        if result.get("agent_name") != STAGE_AGENT_NAME:
            raise AssertionError(f"stage result file is for {result.get('agent_name')!r}, expected {STAGE_AGENT_NAME!r}")
        if "admin_password" not in result and admin_password_env is not None:
            admin_password = os.environ.get(admin_password_env)
            if not admin_password:
                raise AssertionError(f"{admin_password_env} is not set or empty")
            result["admin_password"] = admin_password
        self.result = result
        self.ssh_key = str(ssh_key)
        self.region = str(result["region"])
        self.workdir = Path(tempfile.mkdtemp(prefix="stage-aws-"))
        self.control_socket = self.workdir / "ssh-control"
        self.thread_prefix = f"stage-{int(time.time())}-"
        self.github_app_config, self.github_secret_error = _github_app_config_from_env()
        self.stage_bedrock_credential, self.bedrock_secret_error = _bedrock_credential_from_env()

    def enforcement_policy(self) -> dict:
        policy = super().enforcement_policy()
        github = policy["network_integrations"]["github"]
        base_repos = [repo for repo in github["write_repositories"] if isinstance(repo, dict)]
        # Stage repositories (the CI-secret sandbox repo, or the operator's) go
        # first, ahead of the hardcoded public read repo, so
        # check_github_write_e2e always targets the sandbox at
        # write_repositories[0] and never a stale or public entry.
        ordered = [repo for repo in getattr(self, "stage_github_repositories", []) if isinstance(repo, dict)]
        listed = {(repo.get("owner"), repo.get("repo")) for repo in ordered}
        for repo in base_repos:
            if (repo.get("owner"), repo.get("repo")) not in listed:
                ordered.append(repo)
        github["write_repositories"] = ordered
        return policy

    def task_body(
        self,
        input_message: str,
        thread_id: str,
        *,
        runtime: str | None = None,
        model: str | None = None,
        effort: str | None = None,
    ) -> dict:
        selected_runtime = runtime or self.agent_runtime
        return super().task_body(
            input_message,
            self.thread_prefix + thread_id,
            runtime=selected_runtime,
            model=model or CHEAP_MODELS[selected_runtime],
            effort=effort or CHEAP_EFFORT,
        )

    def follow_up_body(self, input_message: str, thread_id: str) -> dict:
        return super().follow_up_body(input_message, self.thread_prefix + thread_id)

    def close_tunnel(self) -> None:
        if self.tunnel_open and self.result:
            self.tunnel_open = False
            import subprocess

            subprocess.run(
                [
                    "ssh",
                    "-S",
                    str(self.control_socket),
                    "-O",
                    "exit",
                    f"trustyclaw-operator@{self.result['public_dns']}",
                ],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                check=False,
            )

    def teardown(self) -> None:
        self.close_tunnel()

    def recover_baseline(self, runtimes: tuple[str, ...] = ()) -> None:
        self._step("stage baseline recovery")
        # GitHub write repositories must survive the baseline policy reset. When
        # the run is auto-configured from CI secrets, the sandbox repo from those
        # secrets is authoritative and fully replaces any host state, so a stale
        # or manually added entry can never become the write target. Otherwise
        # the operator-configured repos are captured from the stored policy and
        # merged into every policy this run publishes.
        if self.github_app_config is not None:
            self.stage_github_repositories = [
                {"owner": self.github_app_config["owner"], "repo": self.github_app_config["repo"]}
            ]
        else:
            stored = self._api("GET", "/v1/network/policy").get("network_controls") or {}
            integrations = stored.get("network_integrations") or {}
            github = integrations.get("github") or {}
            self.stage_github_repositories = [
                repo for repo in (github.get("write_repositories") or []) if isinstance(repo, dict)
            ]
        self._api("PUT", "/v1/network/policy", self.enforcement_policy())
        deadline = time.time() + 90
        while time.time() < deadline:
            active = self._active_tasks()
            if not active:
                break
            for task in active:
                task_id = task["task_id"]
                status = task["status"]
                if status == "running":
                    code, _ = self._api_status("POST", f"/v1/tasks/{task_id}/kill")
                elif status == "queued":
                    code, _ = self._api_status("POST", f"/v1/tasks/{task_id}/cancel")
                else:
                    continue
                if code not in {200, 202, 409, 404}:
                    raise AssertionError(f"baseline cleanup of {task_id} in {status} returned {code}")
            time.sleep(3)
        else:
            raise AssertionError(f"stage still has active tasks after cleanup: {self._active_tasks()}")
        for runtime in runtimes:
            self.require_runtime_active(runtime)
        if any(runtime in {"codex", "claude_code"} for runtime in runtimes):
            self._assert_provider_account_anchors(live_pins=True)
        active_note = (
            ", ".join(_RUNTIME_LABELS[runtime] for runtime in runtimes) + " active"
            if runtimes
            else "no provider runtime required for this suite"
        )
        self._ok(f"policy reset, active tasks cleared, {active_note}")

    @staticmethod
    def suite_runtimes(suite: str) -> tuple[str, ...]:
        """Provider runtimes the selected suite exercises (and therefore needs
        available). 'github' and tool suites need none; 'all' needs all four."""
        if suite == "codex":
            return ("codex",)
        if suite == "claude":
            return ("claude_code",)
        if suite in {"pi", "hermes"}:
            return (suite,)
        if suite == "github" or suite in TOOL_SUITES:
            return ()
        return SMOKE_RUNTIMES

    def check_agent_file_explorer(self) -> None:
        self._step("agent file explorer API on real agent home")
        directory_name = f".stage-file-explorer-{int(time.time())}"
        file_name = 'quote"file.txt'
        html_file_name = '<img src=x onerror="window.__stageFileNameXss=1">.txt'
        symlink_name = "outside-link"
        internal_symlink_name = "inside-file-link"
        dir_symlink_name = "outside-dir-link"
        file_content = f"stage file explorer content {self.thread_prefix}"
        html_file_content = '<script>window.__stageFileContentXss=1</script>\n'
        create_script = "\n".join([
            "from pathlib import Path",
            "home = Path('/mnt/trustyclaw-agent/agent-home')",
            f"directory = home / {directory_name!r}",
            "directory.mkdir(mode=0o700, exist_ok=True)",
            f"(directory / {file_name!r}).write_text({file_content!r})",
            f"(directory / {html_file_name!r}).write_text({html_file_content!r})",
            f"(directory / {symlink_name!r}).symlink_to('/etc/passwd')",
            f"(directory / {internal_symlink_name!r}).symlink_to({file_name!r})",
            f"(directory / {dir_symlink_name!r}).symlink_to('/tmp', target_is_directory=True)",
        ])
        cleanup = (
            "sudo -u trustyclaw-agent python3 - <<'PY'\n"
            "import shutil\n"
            "from pathlib import Path\n"
            f"shutil.rmtree(Path('/mnt/trustyclaw-agent/agent-home') / {directory_name!r}, ignore_errors=True)\n"
            "PY"
        )
        try:
            self._ssh_code(f"sudo -u trustyclaw-agent python3 - <<'PY'\n{create_script}\nPY")
            root = self._api("GET", "/v1/agent-files?path=/")
            if not isinstance(root.get("truncated"), bool) or "max_entries" in root:
                raise AssertionError(f"agent file list did not report expected listing metadata: {root}")
            root_names = {entry.get("name") for entry in root.get("entries", [])}
            if directory_name not in root_names:
                raise AssertionError(f"agent file root did not include hidden stage directory: {root}")

            directory_path = f"/{directory_name}"
            listed = self._api("GET", f"/v1/agent-files?path={quote(directory_path, safe='')}")
            entries = listed.get("entries", [])
            match = next((entry for entry in entries if entry.get("name") == file_name), None)
            if not match or match.get("type") != "file" or match.get("path") != f"{directory_path}/{file_name}":
                raise AssertionError(f"agent file directory did not include expected file: {listed}")
            html_match = next((entry for entry in entries if entry.get("name") == html_file_name), None)
            if not html_match or html_match.get("type") != "file" or html_match.get("path") != f"{directory_path}/{html_file_name}":
                raise AssertionError(f"agent file directory did not include expected HTML-looking file: {listed}")
            symlink_names = {symlink_name, internal_symlink_name, dir_symlink_name}
            listed_symlinks = symlink_names & {entry.get("name") for entry in entries}
            if listed_symlinks:
                raise AssertionError(f"agent file directory exposed symlinks {listed_symlinks}: {listed}")

            file_path = f"{directory_path}/{file_name}"
            read = self._api("GET", f"/v1/agent-files/read?path={quote(file_path, safe='')}")
            if read.get("content") != file_content or read.get("truncated") is not False:
                raise AssertionError(f"agent file read returned unexpected payload: {read}")
            html_file_path = f"{directory_path}/{html_file_name}"
            html_read = self._api("GET", f"/v1/agent-files/read?path={quote(html_file_path, safe='')}")
            if html_read.get("content") != html_file_content or html_read.get("truncated") is not False:
                raise AssertionError(f"agent file HTML-looking read returned unexpected payload: {html_read}")

            status, body = self._api_status("GET", "/v1/agent-files?path=..")
            if status != 400 or "escapes the agent home" not in json.dumps(body):
                raise AssertionError(f"agent file path escape returned {status}: {body}")
            for name in (symlink_name, internal_symlink_name):
                symlink_path = f"{directory_path}/{name}"
                status, body = self._api_status("GET", f"/v1/agent-files/read?path={quote(symlink_path, safe='')}")
                if status != 400 or "symlinks are not supported" not in json.dumps(body):
                    raise AssertionError(f"agent file symlink read returned {status}: {body}")
            dir_symlink_path = f"{directory_path}/{dir_symlink_name}"
            status, body = self._api_status("GET", f"/v1/agent-files?path={quote(dir_symlink_path, safe='')}")
            if status != 400 or "symlinks are not supported" not in json.dumps(body):
                raise AssertionError(f"agent file symlink list returned {status}: {body}")
        finally:
            self._ssh_code(cleanup)
        self._ok("hidden directory listed, hostile filenames read as text, and path/symlink escapes rejected")


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
