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
from pathlib import Path
import shlex
import sys
import tempfile
import time

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT))

from host.constants import PROXY_PORT
from tests.smoke.smoke_aws import SMOKE_MANAGED_PROVIDERS, SMOKE_RUNTIMES, AwsSmoke


STAGE_AGENT_NAME = "trustyclaw-stage"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--result-file", required=True, help="Deploy result JSON from the stage upgrade/recover run.")
    parser.add_argument("--ssh-key", required=True, help="Private SSH key for the persistent stage operator key.")
    args = parser.parse_args(argv)

    stage = StageAwsSmoke(Path(args.result_file), Path(args.ssh_key))
    try:
        stage.open_tunnel()
        stage.recover_baseline()
        stage.check_health()
        stage.check_ui_page()
        stage.check_admin_auth()
        stage.check_both_runtimes_active()
        stage.agent_runtime = "codex"
        stage.check_task()
        stage.agent_runtime = "claude_code"
        stage.check_claude_auth_and_task()
        stage.check_agent_parallelism()
        for runtime in SMOKE_RUNTIMES:
            stage.agent_runtime = runtime
            stage.check_agent_steering()
            stage.check_agent_kill_and_thread_survival()
        stage.check_agent_thread_recall()
        stage.check_runtime_deactivation_stops_running_tasks()
        stage.check_reboot_recovery()
        stage.check_network_event_prune_race()
        print(f"\n{stage.passed}/{stage.total} checks passed")
        return 0 if stage.passed == stage.total else 1
    except Exception as exc:  # noqa: BLE001 - report failure with network context
        stage.print_network_events("Network events before failure", since=0)
        print(f"\n[FAIL] {exc}", file=sys.stderr)
        return 1
    finally:
        stage.close_tunnel()


class StageAwsSmoke(AwsSmoke):
    def __init__(self, result_file: Path, ssh_key: Path) -> None:
        super().__init__()
        self.result = json.loads(result_file.read_text())
        if self.result.get("agent_name") != STAGE_AGENT_NAME:
            raise AssertionError(f"stage result file is for {self.result.get('agent_name')!r}, expected {STAGE_AGENT_NAME!r}")
        self.ssh_key = str(ssh_key)
        self.region = str(self.result["region"])
        self.workdir = Path(tempfile.mkdtemp(prefix="stage-aws-"))
        self.control_socket = self.workdir / "ssh-control"
        self.thread_prefix = f"stage-{int(time.time())}-"

    def task_body(self, input_message: str, thread_id: str, *, runtime: str | None = None) -> dict:
        return super().task_body(input_message, self.thread_prefix + thread_id, runtime=runtime)

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

    def recover_baseline(self) -> None:
        self._step("stage baseline recovery")
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
                    code, _ = self._api_status("POST", f"/v1/tasks/{task_id}/kill", idem=self._idem(f"baseline-kill-{task_id}"))
                elif status == "queued":
                    code, _ = self._api_status("POST", f"/v1/tasks/{task_id}/cancel", idem=self._idem(f"baseline-cancel-{task_id}"))
                else:
                    continue
                if code not in {200, 202, 409, 404}:
                    raise AssertionError(f"baseline cleanup of {task_id} in {status} returned {code}")
            time.sleep(3)
        else:
            raise AssertionError(f"stage still has active tasks after cleanup: {self._active_tasks()}")
        for runtime in SMOKE_RUNTIMES:
            self.require_runtime_active(runtime)
        self._ok("policy reset, active tasks cleared, Codex and Claude Code are active")

    def require_runtime_active(self, runtime: str) -> None:
        status = self._wait_for_runtime_status({"active", "awaiting_login", "deactivated", "error"}, runtime=runtime, timeout=180)
        if status != "active":
            raise AssertionError(
                f"{runtime} runtime is {status}; manually open the stage admin UI, complete OAuth, then rerun stage"
            )

    def check_task(self) -> None:
        self._step("Codex account guard + real web-search task")
        self._api(
            "PUT",
            "/v1/network/policy",
            {"managed_ai_provider_network_access": dict(SMOKE_MANAGED_PROVIDERS), "allowed_network_access": {}},
        )
        self.require_runtime_active("codex")
        for method in ("POST", "GET"):
            code, _ = self._api_status(method, "/v1/agent-runtime/codex-oauth-login", idem=self._idem(f"oauth-{method}"))
            if code != 409:
                raise AssertionError(f"{method} codex-oauth-login while active returned {code}, expected 409")
        account = self._agent_account("codex")
        if account.get("status") != "active":
            raise AssertionError(f"GET account while active did not report active: {account}")
        account_id = account.get("account_id")
        if not account_id:
            raise AssertionError(f"GET account while active did not include account_id: {account}")

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
        task = self._api("POST", "/v1/tasks", self.task_body(prompt, "codex-web"))
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
        self._ok("web search task completed; account and live-search guards held")

    def check_claude_auth_and_task(self) -> None:
        self._step("Claude account guard + real task")
        self._api(
            "PUT",
            "/v1/network/policy",
            {"managed_ai_provider_network_access": dict(SMOKE_MANAGED_PROVIDERS), "allowed_network_access": {}},
        )
        self.require_runtime_active("claude_code")
        for method in ("POST", "GET"):
            code, _ = self._api_status(method, "/v1/agent-runtime/claude-oauth-login", idem=self._idem(f"claude-oauth-{method}"))
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
        task = self._api("POST", "/v1/tasks", self.task_body(prompt, "claude"))
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
        follow_up = self._api("POST", "/v1/tasks", self.task_body(follow_up_prompt, "claude"))
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


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
