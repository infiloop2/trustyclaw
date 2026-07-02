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
from tests.smoke.smoke_aws import SMOKE_MANAGED_PROVIDERS, SMOKE_RUNTIMES, AwsSmoke


STAGE_AGENT_NAME = "trustyclaw-stage"


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
    args = parser.parse_args(argv)

    ssh_key = Path(args.ssh_key) if args.ssh_key is not None else _required_env_path(args.ssh_key_env)
    stage = StageAwsSmoke(Path(args.result_file), ssh_key, args.admin_password_env)
    try:
        stage.open_tunnel()
        stage.recover_baseline()
        stage.check_health()
        stage.check_ui_page()
        stage.check_admin_auth()
        stage.check_agent_file_explorer()
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
