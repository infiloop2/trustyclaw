"""Provider and GitHub checks for the persistent stage harness."""

from __future__ import annotations

import shlex
import time
from typing import TYPE_CHECKING

from host.constants import PROXY_PORT
from tests.smoke.smoke_aws import SMOKE_RUNTIMES, AwsSmoke
from tests.stage.stage_support import (
    CHEAP_EFFORT,
    CHEAP_MODELS,
    diagnostic_ref,
    integration_label as _integration_label,
    selected_integrations as _selected_integrations,
)


class StageIntegrationChecks(AwsSmoke):
    """Provider and GitHub checks layered on the shared smoke primitives."""

    if TYPE_CHECKING:
        github_app_config: dict[str, str] | None
        github_secret_error: str | None
        bedrock_secret_error: str | None
        stage_github_repositories: list[dict]

        def enforcement_policy(self) -> dict: ...
        def task_body(
            self,
            input_message: str,
            thread_id: str,
            *,
            runtime: str | None = None,
            model: str | None = None,
            effort: str | None = None,
        ) -> dict: ...
        def _tool_credential_failures(self, tool_id: str) -> list[str]: ...

    @staticmethod
    def _print_agent_task(runtime: str, purpose: str, task: dict, phase: str) -> None:
        """Print task identity and state without logging prompts or output."""
        print(
            f"    [agent task {phase}] runtime={runtime} purpose={purpose} "
            f"task_id={task.get('task_id')} status={task.get('status')} "
            f"model={task.get('model')} effort={task.get('effort')}",
            flush=True,
        )

    @staticmethod
    def _account_shape(account: dict) -> dict[str, object]:
        """Provider-account diagnostics without identity values."""
        return {
            "status": account.get("status"),
            "provider": account.get("provider"),
            "has_account_id": bool(account.get("account_id")),
            "has_email": "email" in account,
            "keys": sorted(account),
        }

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
        integrations = dict(controls.get("network_integrations") or {})
        integrations["github"] = {"enabled": True, "write_repositories": [repo]}
        controls["network_integrations"] = integrations
        self._api("PUT", "/v1/network/policy", controls)
        self._ok(f"GitHub App credential stored and {config['owner']}/{config['repo']} set as the sole write repo")

    def integration_availability(self, suite: str) -> dict[str, str | None]:
        """Check every selected credential before any integration test runs.

        ``None`` means ready. A string is an operator-facing reason the
        integration is unavailable. The all-suite runner records those as
        skips and continues; focused suites turn their one unavailable result
        into a failure so they remain useful for setup and debugging.
        """
        self._step(f"integration credential preflight (suite: {suite})")
        results: dict[str, str | None] = {}
        for integration in _selected_integrations(suite):
            failures: list[str]
            if integration in {"codex", "claude", "hermes"}:
                runtime = "claude_code" if integration == "claude" else integration
                status = self._wait_for_runtime_status(
                    {"active", "awaiting_login", "deactivated", "error"},
                    runtime=runtime,
                    timeout=180,
                )
                if runtime == "hermes":
                    failures = []
                    if self.bedrock_secret_error:
                        failures.append(self.bedrock_secret_error)
                    if status != "active":
                        failures.append(
                            f"runtime is {status!r}; set both STAGE_BEDROCK_AWS_* secrets "
                            "or connect the AWS Bedrock credential in the stage admin UI"
                        )
                else:
                    failures = [] if status == "active" else [
                        f"runtime is {status!r}; open the stage admin UI and complete OAuth"
                    ]
            elif integration == "github":
                failures = self._github_config_failures()
            else:
                failures = self._tool_credential_failures(integration)
            reason = "; ".join(failures) if failures else None
            results[integration] = reason
            if reason is None:
                print(f"  [available] {_integration_label(integration)}", flush=True)
            else:
                print(f"  [unavailable] {_integration_label(integration)}: {reason}", flush=True)
        ready = sum(reason is None for reason in results.values())
        self._ok(f"credential preflight completed: {ready} available, {len(results) - ready} unavailable")
        return results

    def _github_config_failures(self) -> list[str]:
        """GitHub configuration checks for the preflight. Returns remediation
        strings for whatever is missing; prints an [ok] line for each item that
        is present. A non-empty write-repository list implies the integration is
        enabled (policy validation rejects write repos while disabled), so the
        two operator-provided pieces to confirm are the credential and at least
        one sandbox write repository."""
        failures: list[str] = []
        if self.github_secret_error:
            failures.append(self.github_secret_error)
        stored = self._api("GET", "/v1/network/policy").get("network_controls") or {}
        github = ((stored.get("network_integrations") or {}).get("github")) or {}
        write_repos = [repo for repo in (github.get("write_repositories") or []) if isinstance(repo, dict)]
        metadata = self._api("GET", "/v1/network-tools/github-credential")
        if metadata.get("configured") is True:
            validation = (metadata.get("validation") or {}).get("status")
            if validation == "not_checked" and write_repos:
                deadline = time.time() + 30
                while time.time() < deadline and validation == "not_checked":
                    time.sleep(2)
                    metadata = self._api("GET", "/v1/network-tools/github-credential")
                    validation = (metadata.get("validation") or {}).get("status")
            if validation == "ok":
                print(
                    f"  [ok] GitHub credential configured (mode={metadata.get('mode')}, validation=ok)",
                    flush=True,
                )
            else:
                message = (metadata.get("validation") or {}).get("message")
                failures.append(
                    f"GitHub credential validation is {validation!r}"
                    + (f": {message}" if message else "")
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
            integrations = controls.get("network_integrations") or {}
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
        status = self._wait_for_runtime_status(
            {"active", "awaiting_login", "deactivated", "error"},
            runtime=runtime,
            timeout=180,
        )
        if status != "active":
            if runtime == "hermes":
                raise AssertionError(
                    f"{runtime} runtime is {status}; connect the AWS Bedrock credential, then rerun stage"
                )
            raise AssertionError(
                f"{runtime} runtime is {status}; manually open the stage admin UI, complete OAuth, then rerun stage"
            )
        print(f"    [provider status] runtime={runtime} status=active", flush=True)

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
            raise AssertionError(
                f"GET account while active did not report active: {self._account_shape(account)}"
            )
        account_id = account.get("account_id")
        if not account_id:
            raise AssertionError(
                f"GET account while active did not include account_id: {self._account_shape(account)}"
            )
        self._assert_provider_metadata("codex", account)
        print("    [provider account] runtime=codex status=active metadata=valid", flush=True)

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
        if "openai_account_header_required" not in missing_account_response:
            raise AssertionError(f"missing account header was not blocked; proxy returned {missing_account_response!r}")
        if "openai_account_mismatch" not in wrong_account_response:
            raise AssertionError(f"wrong account header was not blocked; proxy returned {wrong_account_response!r}")
        if "openai_web_tool_denied" not in live_response:
            raise AssertionError(f"live web search payload was not blocked; proxy returned {live_response!r}")
        if "openai_web_tool_denied" in cached_response:
            raise AssertionError("cached web search payload was incorrectly blocked")
        print(
            "    [provider guards] runtime=codex missing-account=denied "
            "wrong-account=denied live-web=denied cached-web=allowed",
            flush=True,
        )

        baseline_seq = max((event["seq"] for event in self._network_events()), default=0)
        prompt = "Use your web search tool to check today's date, then reply with the word DONE."
        task = self._api(
            "POST",
            "/v1/tasks",
            self.task_body(prompt, "codex-web"),
        )
        if (task.get("model"), task.get("effort")) != (CHEAP_MODELS["codex"], CHEAP_EFFORT):
            raise AssertionError(f"Codex task did not retain the selected session options: {task}")
        self._print_agent_task("codex", "web-search", task, "started")
        current = self._wait_for_task(task["task_id"], timeout=240)
        self._print_agent_task("codex", "web-search", current, "finished")
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
        print(
            f"    [provider network] runtime=codex chatgpt_events={len(chatgpt)} "
            f"allowed={sum(event['decision'] == 'allowed' for event in chatgpt)} "
            f"denied={len(denied)} fatal_denials={len(fatal)}",
            flush=True,
        )
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
            raise AssertionError(
                "GET account while Claude is active returned unexpected shape: "
                f"{self._account_shape(account)}"
            )
        self._assert_provider_metadata("claude_code", account)
        print("    [provider account] runtime=claude_code status=active metadata=valid", flush=True)

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
        if "anthropic_token_required" not in missing:
            raise AssertionError(f"missing Claude bearer was not blocked; proxy returned {missing!r}")
        if "anthropic_token_mismatch" not in wrong:
            raise AssertionError(f"wrong Claude bearer was not blocked; proxy returned {wrong!r}")
        print(
            "    [provider guards] runtime=claude_code missing-token=denied wrong-token=denied",
            flush=True,
        )

        task_baseline_seq = max((event["seq"] for event in self._network_events()), default=0)
        prompt = "Reply with exactly the word CLAUDE_STAGE_OK and nothing else."
        task = self._api(
            "POST",
            "/v1/tasks",
            self.task_body(prompt, "claude"),
        )
        if (task.get("model"), task.get("effort")) != (CHEAP_MODELS["claude_code"], CHEAP_EFFORT):
            raise AssertionError(f"Claude task did not retain the selected session options: {task}")
        self._print_agent_task("claude_code", "session", task, "started")
        done = self._wait_for_task(task["task_id"], timeout=240)
        self._print_agent_task("claude_code", "session", done, "finished")
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
        print(
            f"    [provider network] runtime=claude_code anthropic_events={len(anthropic)} "
            f"allowed={sum(event['decision'] == 'allowed' for event in anthropic)} "
            f"fatal_denials={len(fatal)}",
            flush=True,
        )

        follow_up_prompt = (
            "Earlier in this Claude conversation you replied with one uppercase token. "
            "Reply with exactly that token again and nothing else."
        )
        follow_up = self._api(
            "POST",
            "/v1/tasks",
            self.follow_up_body(follow_up_prompt, "claude"),
        )
        self._print_agent_task("claude_code", "session-follow-up", follow_up, "started")
        follow_up_done = self._wait_for_task(follow_up["task_id"], timeout=240)
        self._print_agent_task("claude_code", "session-follow-up", follow_up_done, "finished")
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
                "and Tools), then rerun; like the provider logins, this is one-time "
                "stage-host configuration"
            )
        metadata = self._api("GET", "/v1/network-tools/github-credential")
        if metadata.get("configured") is not True:
            raise AssertionError(
                "no GitHub credential is configured on the stage host; store a write-capable "
                "PAT or App credential in the admin UI (Internet Access and Tools), then rerun; "
                "like the provider logins, this is one-time stage-host configuration"
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
            print(f"    [github clone] repo={write_repo} result=success", flush=True)
            pushed = self._ssh_code(
                f"{env} sh -c 'cd {workdir} && git config user.email stage@trustyclaw.invalid && "
                f"git config user.name trustyclaw-stage && echo {branch} > STAGE_E2E.txt && "
                f"git add STAGE_E2E.txt && git commit -q -m stage-e2e && "
                f"git push -q origin HEAD:refs/heads/{branch} && echo pushed' 2>/dev/null"
            ).strip()
            if pushed != "pushed":
                raise AssertionError(f"git push of {branch} to {write_repo} failed")
            print(f"    [github push] repo={write_repo} branch={branch} result=success", flush=True)
            # Authenticated API read through the gh shim proves the pushed
            # branch is real on GitHub, not just accepted by the proxy.
            seen = self._ssh_code(
                f"{env} gh api repos/{write_repo}/branches/{branch} --jq .name 2>/dev/null"
            ).strip()
            if seen != branch:
                raise AssertionError(f"pushed branch not visible via gh api: {seen!r}")
            print(f"    [github read] branch={branch} visible=true", flush=True)
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
            print(f"    [github cleanup] branch={branch} deleted=true", flush=True)
            # The same credential must not be able to push to an unlisted repo:
            # the proxy denies receive-pack before GitHub sees it.
            denied = self._ssh_code(
                f"{env} git -C {workdir} push -q https://github.com/torvalds/linux "
                f"HEAD:refs/heads/{branch} >/dev/null 2>&1 && echo pushed || echo denied"
            ).strip()
            if denied != "denied":
                raise AssertionError("push to an unlisted repo was not denied by the proxy")
            print("    [github guard] unlisted_repo_push=denied", flush=True)
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
            print(f"    [github approval seed] branch={branch} result=success", flush=True)

            approval_policy = self.enforcement_policy()
            github = dict(approval_policy["network_integrations"]["github"])
            github["require_dot_github_approval"] = True
            approval_policy["network_integrations"]["github"] = github
            self._api("PUT", "/v1/network/policy", approval_policy)

            rest_code = self._ssh_code(
                f"{env} curl -s -o /dev/null -w '%{{http_code}}' --max-time 20 "
                f"-X PUT -d '{{\"message\":\"stage\",\"content\":\"eA==\"}}' "
                f"https://api.github.com/repos/{write_repo}/contents/.github/CODEOWNERS || true"
            ).strip()
            if rest_code != "403":
                raise AssertionError(f".github REST contents write should be denied by approval gate, got {rest_code!r}")
            print("    [github approval guard] dot_github_rest_status=403", flush=True)

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
            print(
                f"    [github approval queued] pending_ref={diagnostic_ref(pending_id)} ref={ref} "
                f"changed_paths={changed_paths}",
                flush=True,
            )

            approved = self._api("POST", f"/v1/network-tools/github-pending-pushes/{pending_id}/approve")
            if (approved.get("pending_push") or {}).get("status") != "approved":
                raise AssertionError(f"approval did not mark push approved: {approved}")
            print(
                f"    [github approval result] pending_ref={diagnostic_ref(pending_id)} "
                "status=approved",
                flush=True,
            )
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
            print(f"    [github approval cleanup] branch={branch} deleted=true", flush=True)
            reasons = {
                reason
                for event in self._network_events(since=baseline_seq)
                if event.get("host") in {"github.com", "api.github.com"}
                for reason in [event.get("reason_code")]
                if isinstance(reason, str)
            }
            for reason in ("github_dot_github_rest_write_denied", "github_push_queued_for_approval"):
                if reason not in reasons:
                    raise AssertionError(f"missing network event reason code {reason!r} after approval e2e: {sorted(reasons)}")
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
        """Dump denied GitHub network events since ``since`` with their reason
        codes: what separates an unloadable policy ('network_policy_unavailable')
        from a host that simply is not allowed ('network_policy_denied')."""
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
