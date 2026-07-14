#!/usr/bin/env python3
"""Persistent staging test for an already deployed TrustyClaw host.

Unlike the smoke test, this does not deploy or tear down the host. It assumes a
stage host was upgraded/recovered with stable admin and agent data volumes, and
that Codex and Claude Code OAuth have already been completed once. If either
runtime is not active, this test fails with a manual-login message instead of
starting an interactive OAuth flow.
"""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import shlex
import sys
import tempfile
import time
from urllib.parse import quote

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT))

from host.constants import PROXY_PORT
from tests.smoke.smoke_aws import SMOKE_RUNTIMES, AwsSmoke


STAGE_AGENT_NAME = "trustyclaw-stage"

# Selectable test suites. A provider suite runs that provider's checks plus the
# shared preamble; "brave", "gmail", and "gcal" each run one bundled tool's live
# check; "all" additionally runs the cross-runtime checks. A bundled-tool suite
# you select explicitly fails its preflight when that tool's credential is not
# configured on the stage host (like a provider suite requires that runtime
# logged in); "all" instead self-skips whichever tools are unconfigured so it
# stays useful mid-setup.
STAGE_SUITES = ("all", "brave", "gmail", "gcal", "claude", "codex", "github")
_RUNTIME_LABELS = {"codex": "Codex", "claude_code": "Claude Code"}

# Environment variables (fed from CI secrets) that supply a GitHub App
# credential and sandbox write repo. When all are set the stage run installs
# them on the host itself, so GitHub needs no manual operator setup. When none
# are set the run falls back to operator-configured credentials.
STAGE_GITHUB_APP_ENV = {
    "write_repo": "STAGE_GITHUB_WRITE_REPO",
    "app_id": "STAGE_GITHUB_APP_ID",
    "installation_id": "STAGE_GITHUB_APP_INSTALLATION_ID",
    "private_key": "STAGE_GITHUB_APP_PRIVATE_KEY",
}
def _github_app_config_from_env() -> dict[str, str] | None:
    """Read the GitHub App credential and sandbox write repo from the stage
    secrets in the environment. Returns None when none are set (operator-config
    mode), the parsed config when all are set, and exits when only some are: a
    partially configured secret set is an operator mistake worth failing on."""
    values = {key: (os.environ.get(env) or "").strip() for key, env in STAGE_GITHUB_APP_ENV.items()}
    if not any(values.values()):
        return None
    missing = [STAGE_GITHUB_APP_ENV[key] for key, value in values.items() if not value]
    if missing:
        raise SystemExit(
            "incomplete GitHub App stage configuration: set all of "
            + ", ".join(STAGE_GITHUB_APP_ENV.values())
            + " or none; missing "
            + ", ".join(missing)
        )
    repo = values["write_repo"]
    owner, _, name = repo.partition("/")
    if not owner or not name or "/" in name:
        raise SystemExit(f"{STAGE_GITHUB_APP_ENV['write_repo']} must be 'owner/repo', got {repo!r}")
    return {
        "owner": owner,
        "repo": name,
        "app_id": values["app_id"],
        "installation_id": values["installation_id"],
        "private_key_pem": values["private_key"],
    }


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
        "--suite",
        choices=STAGE_SUITES,
        default="all",
        help=(
            "Which test suite to run. 'claude', 'codex', or 'github' run that provider's "
            "checks only (plus the shared preamble); 'brave', 'gmail', and 'gcal' each run "
            "one bundled tool's live check; 'all' (default) also runs the cross-runtime "
            "checks. Every suite first verifies the operator configuration it needs is "
            "present, failing fast if not."
        ),
    )
    args = parser.parse_args(argv)

    ssh_key = Path(args.ssh_key) if args.ssh_key is not None else _required_env_path(args.ssh_key_env)
    stage = StageAwsSmoke(Path(args.result_file), ssh_key, args.admin_password_env)
    suite = args.suite
    run_codex = suite in ("codex", "all")
    run_claude = suite in ("claude", "all")
    run_github = suite in ("github", "all")
    run_brave = suite in ("brave", "all")
    run_gmail = suite in ("gmail", "all")
    run_gcal = suite in ("gcal", "all")
    run_cross = suite == "all"
    try:
        stage.open_tunnel()
        # Install the GitHub App credential and sandbox write repo from CI
        # secrets when present, so the GitHub checks need no manual operator
        # setup. A no-op when the secrets are absent or GitHub is out of scope.
        stage.autoconfigure_github(suite)
        # Fail fast, before any long-running check, if the configuration this
        # suite depends on is still not present on the host.
        stage.verify_operator_configuration(suite)
        stage.recover_baseline(suite)
        stage.check_health()
        stage.check_ui_page()
        stage.check_admin_auth()
        stage.check_agent_file_explorer()
        if run_cross:
            stage.check_both_runtimes_active()
        if run_brave or run_gmail or run_gcal:
            stage.check_tools_live(brave=run_brave, gmail=run_gmail, gcal=run_gcal)
        if run_codex:
            stage.agent_runtime = "codex"
            stage.check_task()
        if run_claude:
            stage.agent_runtime = "claude_code"
            stage.check_claude_auth_and_task()
        if run_cross:
            stage.check_agent_parallelism()
        for runtime in stage.suite_runtimes(suite):
            stage.agent_runtime = runtime
            stage.check_agent_steering()
            stage.check_agent_kill_and_thread_survival()
        if run_cross:
            stage.check_agent_thread_recall()
            stage.check_runtime_deactivation_stops_running_tasks()
            stage.check_reboot_recovery()
            stage.check_network_event_prune_race()
        if run_github:
            stage.check_github_write_e2e()
        print(f"\n{stage.passed}/{stage.total} checks passed (suite: {suite})")
        return 0 if stage.passed == stage.total else 1
    except Exception as exc:  # noqa: BLE001 - report failure with network + config context
        stage.print_configuration_snapshot()
        stage.print_network_events("Network events before failure", since=0)
        print(f"\n[FAIL] {exc}", file=sys.stderr)
        return 1
    finally:
        stage.close_tunnel()


def _required_env_path(env_name: str) -> Path:
    value = os.environ.get(env_name)
    if not value:
        raise SystemExit(f"{env_name} is not set or empty")
    return Path(value)


class StageAwsSmoke(AwsSmoke):
    def __init__(self, result_file: Path, ssh_key: Path, admin_password_env: str | None = None) -> None:
        super().__init__()
        self.result = json.loads(result_file.read_text())
        if self.result.get("agent_name") != STAGE_AGENT_NAME:
            raise AssertionError(f"stage result file is for {self.result.get('agent_name')!r}, expected {STAGE_AGENT_NAME!r}")
        if "admin_password" not in self.result and admin_password_env is not None:
            admin_password = os.environ.get(admin_password_env)
            if not admin_password:
                raise AssertionError(f"{admin_password_env} is not set or empty")
            self.result["admin_password"] = admin_password
        self.ssh_key = str(ssh_key)
        self.region = str(self.result["region"])
        self.workdir = Path(tempfile.mkdtemp(prefix="stage-aws-"))
        self.control_socket = self.workdir / "ssh-control"
        self.thread_prefix = f"stage-{int(time.time())}-"
        # Parsed once so a malformed secret set fails before the tunnel opens.
        self.github_app_config = _github_app_config_from_env()

    def enforcement_policy(self) -> dict:
        policy = super().enforcement_policy()
        github = policy["managed_network_integrations"]["github"]
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
        return super().task_body(
            input_message,
            self.thread_prefix + thread_id,
            runtime=runtime,
            model=model,
            effort=effort,
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

    def recover_baseline(self, suite: str = "all") -> None:
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
            integrations = stored.get("managed_network_integrations") or {}
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
        runtimes = self.suite_runtimes(suite)
        for runtime in runtimes:
            self.require_runtime_active(runtime)
        if runtimes:
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
        logged in). 'github' needs neither; 'all' needs both."""
        if suite == "codex":
            return ("codex",)
        if suite == "claude":
            return ("claude_code",)
        if suite in ("github", "brave", "gmail", "gcal"):
            return ()
        return SMOKE_RUNTIMES

    def autoconfigure_github(self, suite: str) -> None:
        """When the stage secrets supply a GitHub App credential and sandbox
        write repo, install them on the host so the GitHub checks need no manual
        operator setup: store the App credential, and add the write repo to the
        stored policy (exactly the state an operator would have configured). A
        no-op when the secrets are absent or the suite excludes GitHub."""
        if suite not in ("github", "all"):
            return
        config = self.github_app_config
        if config is None:
            return
        repo = {"owner": config["owner"], "repo": config["repo"]}
        self._step(f"configure GitHub App credential and write repo {config['owner']}/{config['repo']} from stage secrets")
        self._api(
            "PUT",
            "/v1/network-tools/github-credential",
            {
                "mode": "app",
                "app_id": config["app_id"],
                "installation_id": config["installation_id"],
                "private_key_pem": config["private_key_pem"],
            },
        )
        # Fully manage the GitHub integration from the secret: the sandbox repo
        # is the only configured write repo, replacing whatever was on the host,
        # so the preflight and the write e2e can never see a stale entry. The GET
        # response wraps the controls; the PUT takes them directly. Other
        # integrations and domain rules are preserved.
        controls = self._api("GET", "/v1/network/policy").get("network_controls") or {}
        integrations = dict(controls.get("managed_network_integrations") or {})
        integrations["github"] = {"enabled": True, "write_repositories": [repo]}
        controls["managed_network_integrations"] = integrations
        self._api("PUT", "/v1/network/policy", controls)
        self._ok(f"GitHub App credential stored and {config['owner']}/{config['repo']} set as the sole write repo")

    def verify_operator_configuration(self, suite: str) -> None:
        """Preflight: confirm the one-time operator configuration this suite
        needs is present before any long-running check runs. Every missing item
        is collected and reported together, so one run surfaces the full list
        instead of one item per rerun. Read-only: it inspects runtime status and
        the stored policy, and changes nothing."""
        self._step(f"operator configuration preflight (suite: {suite})")
        failures: list[str] = []
        for runtime in self.suite_runtimes(suite):
            status = self._wait_for_runtime_status(
                {"active", "awaiting_login", "deactivated", "error"}, runtime=runtime, timeout=180
            )
            if status == "active":
                print(f"  [ok] {_RUNTIME_LABELS[runtime]} runtime is active", flush=True)
            else:
                failures.append(
                    f"{_RUNTIME_LABELS[runtime]} runtime is {status!r}; open the stage admin UI, "
                    "complete its OAuth login, then rerun (one-time stage-host configuration)"
                )
        if suite in ("github", "all"):
            failures.extend(self._github_config_failures())
        # A bundled-tool suite you select explicitly must have its credential
        # configured on the stage host, the same way a provider suite requires
        # that runtime logged in; it fails here rather than silently skipping.
        if suite in ("brave", "gmail", "gcal"):
            failures.extend(self._tool_credential_failures(suite))
        if failures:
            listing = "".join(f"\n  - {item}" for item in failures)
            raise AssertionError(
                f"stage host is missing configuration required for the {suite!r} suite:{listing}"
            )
        self._ok(f"required operator configuration present for the {suite!r} suite")

    def _tool_credential_failures(self, suite: str) -> list[str]:
        """Preflight credential check for a single bundled-tool suite. Returns a
        remediation string when the tool's credential is not configured on the
        stage host, or prints an [ok] line when it is. Read-only."""
        if suite == "brave":
            if os.environ.get("TRUSTYCLAW_STAGE_BRAVE_API_KEY", ""):
                print("  [ok] Brave API key secret is set", flush=True)
                return []
            return ["Brave: set the TRUSTYCLAW_STAGE_BRAVE_API_KEY repository secret, then rerun"]
        tool_id = "gmail" if suite == "gmail" else "google_calendar"
        tools = {entry["tool_id"]: entry for entry in self._api("GET", "/v1/tools")["tools"]}
        entry = tools.get(tool_id) or {}
        if entry.get("enabled") and (entry.get("connection_status") or {}).get("connected"):
            print(f"  [ok] {tool_id} is connected on the stage host", flush=True)
            return []
        return [
            f"{tool_id}: connect the stage Google account in the admin UI Tools tab, then rerun "
            "(one-time stage-host configuration)"
        ]

    def _github_config_failures(self) -> list[str]:
        """GitHub configuration checks for the preflight. Returns remediation
        strings for whatever is missing; prints an [ok] line for each item that
        is present. A non-empty write-repository list implies the integration is
        enabled (policy validation rejects write repos while disabled), so the
        two operator-provided pieces to confirm are the credential and at least
        one sandbox write repository."""
        failures: list[str] = []
        stored = self._api("GET", "/v1/network/policy").get("network_controls") or {}
        github = ((stored.get("managed_network_integrations") or {}).get("github")) or {}
        write_repos = [repo for repo in (github.get("write_repositories") or []) if isinstance(repo, dict)]
        metadata = self._api("GET", "/v1/network-tools/github-credential")
        if metadata.get("configured") is True:
            validation = (metadata.get("validation") or {}).get("status")
            print(
                f"  [ok] GitHub credential configured (mode={metadata.get('mode')}, validation={validation})",
                flush=True,
            )
        else:
            failures.append(
                "no GitHub credential is configured; set the STAGE_GITHUB_* stage secrets "
                "to auto-configure, or store a write-capable PAT or App credential in the "
                "admin UI (Internet Access and Tools)"
            )
        if write_repos:
            listed = ", ".join(f"{repo.get('owner')}/{repo.get('repo')}" for repo in write_repos)
            print(f"  [ok] GitHub write repositories in policy: {listed}", flush=True)
        else:
            failures.append(
                "the network policy lists no GitHub write repository; set the STAGE_GITHUB_* "
                "stage secrets to auto-configure, or add a dedicated sandbox write repo in "
                "the admin UI (Internet Access and Tools)"
            )
        return failures

    def print_configuration_snapshot(self) -> None:
        """Best-effort dump of the operator-facing configuration: runtime
        statuses, the managed-integration enable flags with the GitHub write
        repos, and the GitHub credential state. Printed on any failure so a red
        run shows what was (and was not) configured without another round trip."""
        print("  configuration snapshot:", flush=True)
        try:
            status = self._api("GET", "/v1/agent-runtime/status")
            for runtime in SMOKE_RUNTIMES:
                try:
                    record = self.runtime_status_record(status, runtime)
                except AssertionError:
                    print(f"    runtime {runtime}: <not present>", flush=True)
                    continue
                detail = record.get("error_message")
                extra = f", error_message={detail!r}" if detail else ""
                print(f"    runtime {runtime}: {record.get('status')}{extra}", flush=True)
        except Exception as exc:  # noqa: BLE001 - best-effort debug output
            print(f"    runtimes: could not read status: {type(exc).__name__}: {exc}", flush=True)
        try:
            policy = self._api("GET", "/v1/network/policy")
            controls = policy.get("network_controls") or {}
            integrations = controls.get("managed_network_integrations") or {}
            enabled = {
                name: (value.get("enabled") if isinstance(value, dict) else None)
                for name, value in integrations.items()
            }
            github = integrations.get("github") or {}
            repos = [
                f"{repo.get('owner')}/{repo.get('repo')}"
                for repo in (github.get("write_repositories") or [])
                if isinstance(repo, dict)
            ]
            print(f"    policy updated_at: {policy.get('updated_at')}", flush=True)
            print(f"    managed integrations enabled: {enabled or '<none>'}", flush=True)
            print(f"    github write repositories: {repos or '<none>'}", flush=True)
        except Exception as exc:  # noqa: BLE001 - best-effort debug output
            print(f"    network policy: could not read: {type(exc).__name__}: {exc}", flush=True)
        try:
            credential = self._api("GET", "/v1/network-tools/github-credential")
            validation = (credential.get("validation") or {}).get("status")
            print(
                f"    github credential configured: {credential.get('configured')} "
                f"(mode={credential.get('mode')}, validation={validation})",
                flush=True,
            )
        except Exception as exc:  # noqa: BLE001 - best-effort debug output
            print(f"    github credential: could not read: {type(exc).__name__}: {exc}", flush=True)

    def require_runtime_active(self, runtime: str) -> None:
        status = self._wait_for_runtime_status({"active", "awaiting_login", "deactivated", "error"}, runtime=runtime, timeout=180)
        if status != "active":
            raise AssertionError(
                f"{runtime} runtime is {status}; manually open the stage admin UI, complete OAuth, then rerun stage"
            )

    def _shim_call(self, request: dict) -> dict:
        """One MCP request through the tools shim, run exactly as an agent
        harness would: as trustyclaw-agent against the tools socket."""
        line = json.dumps(request)
        output = self._ssh_code(
            f"printf '%s\\n' {shlex.quote(line)} | "
            "sudo -u trustyclaw-agent env PYTHONPATH=/opt/trustyclaw-host "
            "python3 -m host.runtime.tools_mcp_shim"
        )
        try:
            return json.loads(output)
        except json.JSONDecodeError as exc:
            raise AssertionError(f"tools shim returned non-JSON: {output!r}") from exc

    def _shim_tool_result(self, name: str, arguments: dict) -> dict:
        response = self._shim_call(
            {"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {"name": name, "arguments": arguments}}
        )
        result = response.get("result") or {}
        text = (result.get("content") or [{}])[0].get("text", "")
        if result.get("isError"):
            raise AssertionError(f"{name} failed: {text}")
        try:
            return json.loads(text)
        except json.JSONDecodeError as exc:
            raise AssertionError(f"{name} returned non-JSON result text: {text!r}") from exc

    def _autoconfigure_google_client(self) -> None:
        """Set the shared Google OAuth client from stage secrets when present,
        so the operator's one-time setup is reduced to the Connect step (the
        OAuth sign-in still cannot be automated). A no-op without the secrets."""
        client_id = os.environ.get("TRUSTYCLAW_STAGE_GOOGLE_OAUTH_CLIENT_ID", "")
        client_secret = os.environ.get("TRUSTYCLAW_STAGE_GOOGLE_OAUTH_CLIENT_SECRET", "")
        if bool(client_id) != bool(client_secret):
            raise AssertionError(
                "set both TRUSTYCLAW_STAGE_GOOGLE_OAUTH_CLIENT_ID and "
                "TRUSTYCLAW_STAGE_GOOGLE_OAUTH_CLIENT_SECRET, or neither"
            )
        if not (client_id and client_secret):
            return
        # Config is scoped per tool, so set the shared Google client under each
        # Google tool that will use it.
        for tool_id in ("gmail", "google_calendar"):
            self._api("PUT", f"/v1/tools/{tool_id}/config", {"key": "GOOGLE_OAUTH_CLIENT_ID", "value": client_id})
            self._api("PUT", f"/v1/tools/{tool_id}/config", {"key": "GOOGLE_OAUTH_CLIENT_SECRET", "value": client_secret})
            try:
                self._api("POST", f"/v1/tools/{tool_id}/enable", {})
            except Exception:
                # Already enabled, or awaiting connect; enablement is idempotent.
                pass

    def check_tools_live(self, brave: bool = True, gmail: bool = True, gcal: bool = True) -> None:
        """Bundled tools against real third-party services. The 'brave', 'gmail',
        and 'gcal' suites each select one tool; 'all' runs every tool. Under a
        dedicated tool suite the preflight has already required that tool's
        credential (the Brave API key secret, or a connected Google account for
        Gmail and Calendar), so it runs. Under 'all' a tool whose credential is
        absent self-skips here, so stage stays useful mid-setup; there is no
        separate per-tool opt-in switch."""
        self._step("bundled tools against live third-party APIs")
        # A per-run nonce keeps each run's draft/event title unique.
        unique_title = f"TrustyClaw stage check {os.urandom(4).hex()}"
        details: list[str] = []

        if brave:
            brave_key = os.environ.get("TRUSTYCLAW_STAGE_BRAVE_API_KEY", "")
            if brave_key:
                self._api("PUT", "/v1/tools/brave_search/config", {"key": "BRAVE_SEARCH_API_KEY", "value": brave_key})
                self._api("POST", "/v1/tools/brave_search/enable", {})
                search = self._shim_tool_result("brave_search_search_web", {"query": "TrustyClaw agent host"})
                if not search.get("results"):
                    raise AssertionError(f"live Brave search returned no results: {search}")
                details.append(f"brave_search returned {len(search['results'])} results")
            else:
                print("  [skip] Brave: set the TRUSTYCLAW_STAGE_BRAVE_API_KEY secret to run the live search check", flush=True)

        # Push the Google client config from stage secrets if present (a no-op
        # otherwise), then run Gmail/Calendar only when the stage account is
        # actually connected. Only fetch the listing when a Google tool is in scope.
        tools: dict[str, dict] = {}
        if gmail or gcal:
            self._autoconfigure_google_client()
            listing = self._api("GET", "/v1/tools")
            tools = {entry["tool_id"]: entry for entry in listing["tools"]}

        gmail_entry = tools.get("gmail") if gmail else None
        if gmail_entry and gmail_entry["enabled"] and gmail_entry["connection_status"].get("connected"):
            # Reads: search, labels, drafts.
            messages = self._shim_tool_result("gmail_search_messages", {"query": "in:anywhere"})
            labels = self._shim_tool_result("gmail_list_labels", {})
            self._shim_tool_result("gmail_list_drafts", {})
            # Write-approval round trip: create a draft, approve it, then delete
            # the draft (also approval-gated) so the check leaves nothing behind
            # and never sends a real email.
            pending = self._shim_tool_result(
                "gmail_draft_action",
                {
                    "action": "create",
                    "to": "stage@example.com",
                    "subject": unique_title,
                    "blocks": [{"type": "paragraph", "text": "Stage draft round trip; safe to ignore."}],
                },
            )
            approval_id = pending.get("approval_id")
            if not approval_id:
                raise AssertionError(f"gmail draft create did not queue an approval: {pending}")
            created = self._api("POST", f"/v1/tools/gmail/approvals/{approval_id}/approve", {})
            if created["approval"]["status"] != "executed":
                raise AssertionError(f"approved gmail draft create did not execute: {created}")
            # The approved-create message carries the new draft id, so cleanup can
            # target exactly the object this run created.
            draft_id = self._created_id_from_message(created.get("result"))
            if not draft_id:
                raise AssertionError(f"approved gmail draft create did not report a draft id: {created}")
            cleanup = self._shim_tool_result("gmail_draft_action", {"action": "delete", "draft_id": draft_id})
            cleanup_decision = self._api("POST", f"/v1/tools/gmail/approvals/{cleanup['approval_id']}/approve", {})
            if cleanup_decision["approval"]["status"] != "executed":
                raise AssertionError(f"approved gmail draft delete did not execute: {cleanup_decision}")
            details.append(
                f"gmail search returned {len(messages.get('messages', []))} messages, "
                f"{len(labels.get('labels', []))} labels, draft round trip created and deleted {draft_id}"
            )
        elif gmail:
            print("  [skip] Gmail: connect the stage Google account in the admin UI Tools tab to run this check", flush=True)

        calendar = tools.get("google_calendar") if gcal else None
        if calendar and calendar["enabled"] and calendar["connection_status"].get("connected"):
            events = self._shim_tool_result("google_calendar_read_events", {})
            if events.get("status") != "success_executed":
                raise AssertionError(f"calendar read failed: {events}")

            # Full single-use approval round trip against the real API:
            # propose an event, approve it, then propose and approve deletion.
            start = time.strftime("%Y-%m-%dT%H:%M:%S+00:00", time.gmtime(time.time() + 86400))
            end = time.strftime("%Y-%m-%dT%H:%M:%S+00:00", time.gmtime(time.time() + 90000))
            pending = self._shim_tool_result(
                "google_calendar_event_change",
                {"operation": "create", "summary": unique_title, "start_time": start, "end_time": end},
            )
            approval_id = pending.get("approval_id")
            if not approval_id:
                raise AssertionError(f"calendar write did not queue an approval: {pending}")
            decision = self._api("POST", f"/v1/tools/google_calendar/approvals/{approval_id}/approve", {})
            if decision["approval"]["status"] != "executed":
                raise AssertionError(f"approved calendar create did not execute: {decision}")
            # The approved-create message carries the new event id.
            event_id = self._created_id_from_message(decision.get("result"))
            if not event_id:
                raise AssertionError(f"approved calendar create did not report an event id: {decision}")
            checked = self._shim_tool_result(
                "check_tool_approval", {"approval_id": approval_id}
            )
            if checked["approval_status"] != "executed":
                raise AssertionError(f"check_tool_approval disagreed: {checked}")

            cleanup = self._shim_tool_result(
                "google_calendar_event_change", {"operation": "delete", "event_id": event_id}
            )
            cleanup_decision = self._api("POST", f"/v1/tools/google_calendar/approvals/{cleanup['approval_id']}/approve", {})
            if cleanup_decision["approval"]["status"] != "executed":
                raise AssertionError(f"approved calendar delete did not execute: {cleanup_decision}")
            details.append(f"calendar approval round trip created and deleted event {event_id}")
        elif gcal:
            print("  [skip] Calendar: connect the stage Google account in the admin UI Tools tab to run this check", flush=True)

        # Whenever any live tool ran, the dedicated tool audit log must have
        # recorded it (calls and approval decisions), and it must page.
        if details:
            events = self._api("GET", "/v1/tools/events?limit=5")["events"]
            if not events:
                raise AssertionError("tool events log is empty after live tool activity")
            seqs = [event["seq"] for event in events]
            if seqs != sorted(seqs, reverse=True):
                raise AssertionError(f"tool events are not newest-first: {seqs}")
            details.append(f"tool audit log recorded {len(events)}+ events")

        self._ok("; ".join(details) if details else "all live tool checks skipped (no stage tool credential present)")

    @staticmethod
    def _created_id_from_message(result: object) -> str:
        """The created object's id from an approved-execution result. Approved
        executions surface a user-visible message ending in the id (for example
        "Created Gmail draft <id>." or "Created Google Calendar event <id>."), so
        the id is the last whitespace token with the trailing period stripped."""
        message = result.get("message") if isinstance(result, dict) else None
        if not isinstance(message, str) or not message.strip():
            return ""
        return message.strip().rstrip(".").split()[-1]

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

    def check_task(self) -> None:
        self._step("Codex account guard + real web-search task")
        # Publish the full enforcement policy (not just the provider bundle) so a
        # provider-only run keeps the GitHub integration and its write
        # repositories in the stored policy instead of erasing them.
        self._api("PUT", "/v1/network/policy", self.enforcement_policy())
        self.require_runtime_active("codex")
        for method in ("POST", "GET"):
            code, _ = self._api_status(method, "/v1/agent-runtime/codex-oauth-login")
            if code != 409:
                raise AssertionError(f"{method} codex-oauth-login while active returned {code}, expected 409")
        account = self._agent_account("codex")
        if account.get("status") != "active":
            raise AssertionError(f"GET account while active did not report active: {account}")
        account_id = account.get("account_id")
        if not account_id:
            raise AssertionError(f"GET account while active did not include account_id: {account}")
        self._assert_provider_metadata("codex", account)

        proxy = f"http://127.0.0.1:{PROXY_PORT}"
        url = "https://chatgpt.com/backend-api/codex/responses"
        live = '{"tools":[{"type":"web_search","external_web_access":true}]}'
        cached = '{"tools":[{"type":"web_search","external_web_access":false}]}'

        def post_openai(payload: str, account_header: str | None = account_id) -> str:
            header = "" if account_header is None else f" -H {shlex.quote(f'ChatGPT-Account-Id: {account_header}')}"
            return self._ssh_code(
                f"sudo -u trustyclaw-agent env HTTPS_PROXY={proxy} "
                f"curl -s --max-time 20 -X POST -H 'Content-Type: application/json' "
                f"{header} --data {shlex.quote(payload)} {shlex.quote(url)}"
            )

        missing_account_response = post_openai(cached, account_header=None)
        wrong_account_response = post_openai(cached, account_header=f"{account_id}-wrong")
        live_response = post_openai(live)
        cached_response = post_openai(cached)
        if "OpenAI account id header is required" not in missing_account_response:
            raise AssertionError(f"missing account header was not blocked; proxy returned {missing_account_response!r}")
        if "not the configured account" not in wrong_account_response:
            raise AssertionError(f"wrong account header was not blocked; proxy returned {wrong_account_response!r}")
        if "live web search is disabled" not in live_response:
            raise AssertionError(f"live web search payload was not blocked; proxy returned {live_response!r}")
        if "live web search is disabled" in cached_response:
            raise AssertionError("cached web search payload was incorrectly blocked")

        baseline_seq = max((event["seq"] for event in self._network_events()), default=0)
        prompt = "Use your web search tool to check today's date, then reply with the word DONE."
        task = self._api(
            "POST",
            "/v1/tasks",
            self.task_body(prompt, "codex-web", model="gpt-5.6-sol", effort="ultra"),
        )
        if (task.get("model"), task.get("effort")) != ("gpt-5.6-sol", "ultra"):
            raise AssertionError(f"Codex task did not retain the selected session options: {task}")
        current = self._wait_for_task(task["task_id"], timeout=240)
        events = self._network_events(since=baseline_seq)
        chatgpt = [event for event in events if event["host"].endswith("chatgpt.com")]
        denied = [event for event in chatgpt if event["decision"] == "denied"]
        fatal = [event for event in denied if event["path"].startswith("/backend-api/codex/responses")]
        if current["status"] != "completed":
            raise AssertionError(f"task did not complete: {current}; denied chatgpt.com events: {denied}")
        if fatal:
            raise AssertionError(f"the guard denied agent ChatGPT turn traffic: {fatal}")
        if not any(event["decision"] == "allowed" for event in chatgpt):
            raise AssertionError(f"no allowed chatgpt.com traffic was observed for the task: {events}")
        self._ok("web search task completed; account and external URL request guards held")

    def check_claude_auth_and_task(self) -> None:
        self._step("Claude account guard + real task")
        # Publish the full enforcement policy (not just the provider bundle) so a
        # provider-only run keeps the GitHub integration and its write
        # repositories in the stored policy instead of erasing them.
        self._api("PUT", "/v1/network/policy", self.enforcement_policy())
        self.require_runtime_active("claude_code")
        for method in ("POST", "GET"):
            code, _ = self._api_status(method, "/v1/agent-runtime/claude-oauth-login")
            if code != 409:
                raise AssertionError(f"{method} claude-oauth-login while active returned {code}, expected 409")
        account = self._agent_account("claude_code")
        if (
            account.get("status") != "active"
            or account.get("provider") != "claude"
            or not account.get("account_id")
            or "email" not in account
        ):
            raise AssertionError(f"GET account while Claude is active returned unexpected shape: {account}")
        self._assert_provider_metadata("claude_code", account)

        proxy = f"http://127.0.0.1:{PROXY_PORT}"
        url = "https://api.anthropic.com/v1/messages"
        payload = '{"model":"claude-sonnet-4-5","max_tokens":8,"messages":[{"role":"user","content":"hello"}]}'
        missing = self._ssh_code(
            f"sudo -u trustyclaw-agent env HTTPS_PROXY={proxy} "
            f"curl -s --max-time 20 -X POST -H 'Content-Type: application/json' "
            f"--data {shlex.quote(payload)} {shlex.quote(url)}"
        )
        wrong = self._ssh_code(
            f"sudo -u trustyclaw-agent env HTTPS_PROXY={proxy} "
            f"curl -s --max-time 20 -X POST -H 'Content-Type: application/json' "
            f"-H 'Authorization: Bearer stage-wrong-token' "
            f"--data {shlex.quote(payload)} {shlex.quote(url)}"
        )
        if "Claude bearer token is required" not in missing:
            raise AssertionError(f"missing Claude bearer was not blocked; proxy returned {missing!r}")
        if "Claude bearer token does not match" not in wrong:
            raise AssertionError(f"wrong Claude bearer was not blocked; proxy returned {wrong!r}")

        task_baseline_seq = max((event["seq"] for event in self._network_events()), default=0)
        prompt = "Reply with exactly the word CLAUDE_STAGE_OK and nothing else."
        task = self._api(
            "POST",
            "/v1/tasks",
            self.task_body(prompt, "claude", model="fable", effort="ultracode"),
        )
        if (task.get("model"), task.get("effort")) != ("fable", "ultracode"):
            raise AssertionError(f"Claude task did not retain the selected session options: {task}")
        done = self._wait_for_task(task["task_id"], timeout=240)
        if done["status"] != "completed":
            raise AssertionError(f"Claude task ended {done['status']}: {self._task_failure_detail(task['task_id'])}")
        if "CLAUDE_STAGE_OK" not in (done.get("output_message") or ""):
            raise AssertionError(f"Claude task output did not contain expected token: {done.get('output_message')!r}")
        events = self._network_events(since=task_baseline_seq)
        anthropic = [event for event in events if event["host"] == "api.anthropic.com"]
        if not any(event["decision"] == "allowed" for event in anthropic):
            raise AssertionError(f"Claude task completed without an allowed api.anthropic.com event: {events}")
        fatal = [
            event for event in anthropic
            if event["decision"] == "denied" and event["path"].startswith("/v1/messages")
        ]
        if fatal:
            raise AssertionError(f"Claude task had denied message traffic: {fatal}")

        follow_up_prompt = (
            "Earlier in this Claude conversation you replied with one uppercase token. "
            "Reply with exactly that token again and nothing else."
        )
        follow_up = self._api(
            "POST",
            "/v1/tasks",
            self.follow_up_body(follow_up_prompt, "claude"),
        )
        follow_up_done = self._wait_for_task(follow_up["task_id"], timeout=240)
        if follow_up_done["status"] != "completed":
            raise AssertionError(
                f"Claude follow-up ended {follow_up_done['status']}: "
                f"{self._task_failure_detail(follow_up['task_id'])}"
            )
        if "CLAUDE_STAGE_OK" not in (follow_up_done.get("output_message") or ""):
            raise AssertionError(
                "Claude follow-up did not resume the persisted session context: "
                f"{follow_up_done.get('output_message')!r}"
            )
        self._ok("Claude account guard passed; real task completed and resumed through the proxy")

    def check_github_write_e2e(self) -> None:
        """End-to-end exercise of the authenticated GitHub paths with a real,
        operator-installed write credential: clone, a real branch push
        (receive-pack), authenticated gh API read and write, and the write
        denial on an unlisted repo. Like the provider OAuth logins, this
        requires one-time stage-host configuration and fails with instructions
        until it is done: a write-capable credential stored and at least one
        sandbox write repository in the policy (real branches are pushed and
        deleted there). The pushed branch is deleted through the API."""
        write_repos = [
            repo for repo in getattr(self, "stage_github_repositories", []) if isinstance(repo, dict)
        ]
        if not write_repos:
            raise AssertionError(
                "the stage host's network policy lists no GitHub write repository; "
                "add a dedicated sandbox write repo in the admin UI (Internet Access "
                "and Tools), then rerun — like the provider logins, this is one-time "
                "stage-host configuration"
            )
        metadata = self._api("GET", "/v1/network-tools/github-credential")
        if metadata.get("configured") is not True:
            raise AssertionError(
                "no GitHub credential is configured on the stage host; store a write-capable "
                "PAT or App credential in the admin UI (Internet Access and Tools), then rerun "
                "— like the provider logins, this is one-time stage-host configuration"
            )
        write_repo = f"{write_repos[0]['owner']}/{write_repos[0]['repo']}"
        self._step(f"github write e2e against {write_repo} (operator-installed credential)")
        # Self-contained regardless of suite or ordering: the provider checks
        # reset the policy to a GitHub-less bundle, so publish the GitHub-enabled
        # enforcement policy (with the stage write repositories) before
        # exercising the authenticated paths.
        self._api("PUT", "/v1/network/policy", self.enforcement_policy())
        proxy = f"http://127.0.0.1:{PROXY_PORT}"
        env = f"sudo -u trustyclaw-agent env HTTPS_PROXY={proxy} https_proxy={proxy}"
        branch = f"stage-e2e-{time.time_ns()}"
        workdir = f"/tmp/trustyclaw-stage-github-{time.time_ns()}"
        baseline_seq = max((event["seq"] for event in self._network_events()), default=0)
        try:
            cloned = self._ssh_code(
                f"{env} git clone --depth 1 https://github.com/{write_repo} {workdir} "
                ">/dev/null 2>&1 && echo cloned"
            ).strip()
            if cloned != "cloned":
                raise AssertionError(f"authenticated clone of {write_repo} through the proxy failed")
            pushed = self._ssh_code(
                f"{env} sh -c 'cd {workdir} && git config user.email stage@trustyclaw.invalid && "
                f"git config user.name trustyclaw-stage && echo {branch} > STAGE_E2E.txt && "
                f"git add STAGE_E2E.txt && git commit -q -m stage-e2e && "
                f"git push -q origin HEAD:refs/heads/{branch} && echo pushed' 2>/dev/null"
            ).strip()
            if pushed != "pushed":
                raise AssertionError(f"git push of {branch} to {write_repo} failed")
            # Authenticated API read through the gh shim proves the pushed
            # branch is real on GitHub, not just accepted by the proxy.
            seen = self._ssh_code(
                f"{env} gh api repos/{write_repo}/branches/{branch} --jq .name 2>/dev/null"
            ).strip()
            if seen != branch:
                raise AssertionError(f"pushed branch not visible via gh api: {seen!r}")
            # Authenticated API write: delete the ref (also the cleanup). Capture
            # the outcome so a real write failure (e.g. a missing permission)
            # reads distinctly from the branch merely lingering below.
            delete_result = self._ssh_code(
                f"{env} sh -c 'gh api -X DELETE repos/{write_repo}/git/refs/heads/{branch} 2>&1 "
                "&& echo DELETE_OK || echo DELETE_FAILED'"
            ).strip()
            if not delete_result.endswith("DELETE_OK"):
                raise AssertionError(f"gh api DELETE of {branch} on {write_repo} failed: {delete_result!r}")
            # GitHub's branches REST endpoint can briefly still report a ref that
            # was just deleted through the git data plane, so confirm the branch
            # is gone with a few short retries instead of a single racy read.
            deleted = "present"
            for _ in range(6):
                deleted = self._ssh_code(
                    f"{env} sh -c 'gh api repos/{write_repo}/branches/{branch} >/dev/null 2>&1 "
                    "&& echo present || echo deleted'"
                ).strip()
                if deleted == "deleted":
                    break
                time.sleep(2)
            if deleted != "deleted":
                raise AssertionError(f"gh api DELETE did not remove {branch} from {write_repo} after retries")
            # The same credential must not be able to push to an unlisted repo:
            # the proxy denies receive-pack before GitHub sees it.
            denied = self._ssh_code(
                f"{env} git -C {workdir} push -q https://github.com/torvalds/linux "
                f"HEAD:refs/heads/{branch} >/dev/null 2>&1 && echo pushed || echo denied"
            ).strip()
            if denied != "denied":
                raise AssertionError("push to an unlisted repo was not denied by the proxy")
            self._check_github_dot_github_approval_e2e(write_repo, env, baseline_seq)
        except Exception:
            self._print_denied_github_events(baseline_seq)
            raise
        finally:
            self._ssh_code(f"sudo rm -rf {workdir}")
        self._ok(
            f"clone + push + gh api read/write on {write_repo}, branch {branch} cleaned up, "
            "unlisted push denied, .github approval queued and approved"
        )

    def _check_github_dot_github_approval_e2e(self, write_repo: str, env: str, baseline_seq: int) -> None:
        """Real stage coverage for the .github approval gate: enable the toggle,
        prove REST bypasses are denied, queue a .github-changing push, approve it,
        confirm it lands on GitHub, then delete the branch through git."""
        owner, repo = write_repo.split("/", 1)
        branch = f"stage-dotgithub-{time.time_ns()}"
        ref = f"refs/heads/{branch}"
        workdir = f"/tmp/trustyclaw-stage-dotgithub-{time.time_ns()}"
        self._api("PUT", "/v1/network/policy", self.enforcement_policy())
        branch_landed = False
        pending_id = None
        try:
            seed_script = f"""
rm -rf {shlex.quote(workdir)}
git clone --depth 1 https://github.com/{shlex.quote(write_repo)} {shlex.quote(workdir)} >/dev/null 2>&1 || {{ echo CLONE_FAILED; exit 0; }}
cd {shlex.quote(workdir)} || {{ echo CD_FAILED; exit 0; }}
git config user.email stage@trustyclaw.invalid
git config user.name trustyclaw-stage
git checkout -q -b {shlex.quote(branch)}
echo {shlex.quote(branch)} > STAGE_DOTGITHUB_BASE.txt
git add STAGE_DOTGITHUB_BASE.txt
git commit -q -m stage-dotgithub-base
git push -q origin HEAD:{shlex.quote(ref)} >/dev/null 2>&1 && echo SEED_PUSHED || echo SEED_FAILED
"""
            seed_output = self._ssh_code(f"{env} sh -c {shlex.quote(seed_script)}").strip()
            if seed_output != "SEED_PUSHED":
                raise AssertionError(f"setup branch for .github approval push failed: {seed_output!r}")
            branch_landed = True

            approval_policy = self.enforcement_policy()
            github = dict(approval_policy["managed_network_integrations"]["github"])
            github["require_dot_github_approval"] = True
            approval_policy["managed_network_integrations"]["github"] = github
            self._api("PUT", "/v1/network/policy", approval_policy)

            rest_code = self._ssh_code(
                f"{env} curl -s -o /dev/null -w '%{{http_code}}' --max-time 20 "
                f"-X PUT -d '{{\"message\":\"stage\",\"content\":\"eA==\"}}' "
                f"https://api.github.com/repos/{write_repo}/contents/.github/CODEOWNERS || true"
            ).strip()
            if rest_code != "403":
                raise AssertionError(f".github REST contents write should be denied by approval gate, got {rest_code!r}")

            script = f"""
cd {shlex.quote(workdir)} || {{ echo CD_FAILED; exit 0; }}
mkdir -p .github
printf 'stage * @trustyclaw-stage\\n' > .github/CODEOWNERS
git add .github/CODEOWNERS
git commit -q -m stage-dotgithub-approval
git push origin HEAD:{shlex.quote(ref)} > /tmp/trustyclaw-stage-dotgithub-push.out 2>&1
status=$?
cat /tmp/trustyclaw-stage-dotgithub-push.out
echo PUSH_STATUS:$status
"""
            push_output = self._ssh_code(f"{env} sh -c {shlex.quote(script)}")
            if "CD_FAILED" in push_output:
                raise AssertionError(f"setup for .github approval push failed: {push_output!r}")
            if "PUSH_STATUS:0" in push_output:
                raise AssertionError(".github-changing push succeeded instead of being queued for approval")
            if "queued for approval" not in push_output:
                raise AssertionError(f".github-changing push did not report approval queue: {push_output!r}")

            pending = None
            deadline = time.time() + 30
            while time.time() < deadline:
                pushes = self._api("GET", "/v1/network-tools/github-pending-pushes").get("pending_pushes") or []
                for candidate in pushes:
                    updates = candidate.get("ref_updates") or []
                    if (
                        candidate.get("owner") == owner.lower()
                        and candidate.get("repo") == repo.lower()
                        and candidate.get("status") == "pending"
                        and any(update.get("ref") == ref for update in updates if isinstance(update, dict))
                    ):
                        pending = candidate
                        break
                if pending is not None:
                    break
                time.sleep(2)
            if pending is None:
                raise AssertionError(f"no pending .github push found for {ref}")
            pending_id = pending["id"]
            changed_paths = pending.get("changed_paths") or []
            if ".github/CODEOWNERS" not in changed_paths:
                raise AssertionError(f"pending push did not record .github/CODEOWNERS: {pending}")

            approved = self._api("POST", f"/v1/network-tools/github-pending-pushes/{pending_id}/approve")
            if (approved.get("pending_push") or {}).get("status") != "approved":
                raise AssertionError(f"approval did not mark push approved: {approved}")
            pending_id = None
            seen = self._ssh_code(
                f"{env} gh api repos/{write_repo}/branches/{branch} --jq .name 2>/dev/null"
            ).strip()
            if seen != branch:
                raise AssertionError(f"approved .github branch not visible via gh api: {seen!r}")

            deleted = self._ssh_code(
                f"{env} git -C {workdir} push -q origin :{shlex.quote(ref)} >/dev/null 2>&1 "
                "&& echo deleted || echo delete_failed"
            ).strip()
            if deleted != "deleted":
                raise AssertionError(f"approved branch cleanup via git delete failed: {deleted!r}")
            branch_landed = False
            reasons = {
                event.get("reason")
                for event in self._network_events(since=baseline_seq)
                if event.get("host") in {"github.com", "api.github.com"}
            }
            for reason in ("github_dot_github_rest_write_denied", "github_push_queued_for_approval"):
                if reason not in reasons:
                    raise AssertionError(f"missing network event reason {reason!r} after approval e2e: {sorted(reasons)}")
        finally:
            if pending_id:
                try:
                    self._api("POST", f"/v1/network-tools/github-pending-pushes/{pending_id}/reject")
                except Exception:
                    pass
            try:
                self._api("PUT", "/v1/network/policy", self.enforcement_policy())
            except Exception:
                pass
            if branch_landed:
                self._ssh_code(
                    f"{env} gh api -X DELETE repos/{write_repo}/git/refs/heads/{branch} >/dev/null 2>&1 || true"
                )
            self._ssh_code(f"sudo rm -rf {workdir} /tmp/trustyclaw-stage-dotgithub-push.out")

    def _print_denied_github_events(self, since: int) -> None:
        """Dump denied GitHub network events since ``since`` with their reasons.
        The reason string is what separates a policy that failed to load or
        expand ('network policy unavailable: ...') from a host that simply is
        not allowed ('host is not in the allowed network policy')."""
        github_hosts = {
            "github.com", "api.github.com", "uploads.github.com",
            "codeload.github.com", "raw.githubusercontent.com",
        }
        try:
            events = [
                event for event in self._network_events(since=since)
                if event.get("host") in github_hosts and event.get("decision") == "denied"
            ]
        except Exception as exc:  # noqa: BLE001 - best-effort debug output
            print(f"  denied GitHub events: could not read: {type(exc).__name__}: {exc}", flush=True)
            return
        if not events:
            print("  denied GitHub events during write e2e: <none>", flush=True)
            return
        print(f"  denied GitHub events during write e2e ({len(events)}):", flush=True)
        for event in events:
            print(
                f"    seq={event.get('seq')} {event.get('method')} "
                f"{event.get('host')}{event.get('path')} reason={event.get('reason')!r}",
                flush=True,
            )


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
