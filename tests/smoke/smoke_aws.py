#!/usr/bin/env python3
"""The smoke test: deploy a real host, validate it end to end, tear it down.

This is the fresh-host smoke test for the project. It validates the deploy,
bootstrap, admin API, proxy, and state-management paths that unit tests cannot.
It deliberately does not require Codex or Claude OAuth login; persistent,
login-dependent runtime checks live in ``tests/stage/stage_aws.py``.

  - the deploy path (subnet selection, security group, IMDSv2, SSH provisioning)
  - the bootstrap on real Ubuntu 22.04 (apt, npm, user/permission setup,
    nftables, systemd, the proxy CA)
  - the admin API answering over the SSH tunnel (health, network policy)
  - the real admin UI in headless Chrome: login, app navigation, Mission
    Pursuit popovers and settings, first-message task creation, and the clear
    pre-provider-login failure shown back in the workspace
  - admin API contract edge cases over the tunnel: auth rejection, the task
    lifecycle and its 4xx responses,
    policy validation (including managed OpenAI and Claude provider schema),
    and event pagination
  - concurrency on the real host: parallel task creation, a same-key
    concurrent policy replaces, and parallel proxy traffic
    with consistent event sequencing
  - state transaction edge cases on the real host: racing cancels of one task
    resolve to exactly one winner, racing updates apply last-writer-wins (and
    never tear), an update racing a cancel cannot resurrect the task, and
    parallel writers never duplicate an event seq
  - network enforcement on the real host: the agent reaches an allowed domain
    only through the proxy, a denied path is blocked, direct external egress is
    dropped by nftables, and non-proxy loopback access to the admin API is
    blocked
  - deploy-time config schema on the real host: agent_runtime, agent_type,
    operator connection details and network_controls are absent from persisted runtime
    config; first boot creates an empty runtime network policy, with no
    network_status.json, and both runtimes stay deactivated until the runtime
    policy enables their managed providers
  - proxy protocol edge cases: CONNECT port pinning, unknown hosts, Host
    header mismatch, percent-encoded paths against path guards, wildcard
    domain rules, malformed request lengths, and plain-HTTP proxying
  - provider guard pre-login behavior: managed OpenAI/Claude access wakes
    runtimes but does not require completing OAuth
  - the cross-process event log: the network event table is pushed past its
    amortized prune threshold by the proxy process (writing under its narrow
    database role) while the admin API concurrently pages it — reads stay
    consistent, seqs stay unique and ordered, and the proxy role can write
    exactly that one table

Only run this from the dedicated smoke GitHub Action or manually with the
scoped smoke AWS credentials. It creates real billable AWS resources and needs
real network. Unit-test CI runs with no network on purpose.

This script starts by resetting any stale smoke data volumes, then tears down
all resources tagged for the smoke agent (instances, volumes, and security
groups), even if deploy failed before writing its result file.

The smoke owns its own deploy config: it deploys an agent named
``trustyclaw-smoke`` into ``SMOKE_REGION`` (below), which is pinned to the
region scoped in ``tests/smoke/iam_policy_smoke.json`` so the two cannot drift. It
also generates an ephemeral SSH keypair for operator access and discards it at
teardown. So there is nothing to write by hand — no config file and no SSH key.

Environment assumptions (each is checked, with a clear failure if missing):

  1. The ``aws`` CLI and ``ssh`` are installed and on PATH.
  2. AWS credentials with the permissions in ``tests/smoke/iam_policy_smoke.json``
     are exported as ``AWS_ACCESS_KEY_ID`` / ``AWS_SECRET_ACCESS_KEY``. See
     docs/development/fresh-aws-smoke.md for how to create a scoped IAM user.

Cost: one t3.small plus a 16 GiB root gp3 volume and two 8 GiB encrypted data
gp3 volumes for the few minutes the test runs (about one US cent). Teardown
removes the instance root volume and all tagged smoke data volumes.

Usage:
    export AWS_ACCESS_KEY_ID=...
    export AWS_SECRET_ACCESS_KEY=...
    python3 tests/smoke/smoke_aws.py
"""

from __future__ import annotations

import argparse
import calendar
import json
import os
from pathlib import Path
import shlex
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.request

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT))

from host.config import load_input_config
from host.constants import ADMIN_API_PORT as ADMIN_PORT, PROXY_PORT
from host.runtime.state import PRUNE_EVERY
from host.runtime.tools_host import BUNDLED_TOOLS
from tests.smoke.cdp_browser import ChromeBrowser

# Region the smoke deploys into. Keep in sync with the region scoped in
# tests/smoke/iam_policy_smoke.json — change both together.
SMOKE_REGION = "us-east-1"
SMOKE_AGENT_NAME = "trustyclaw-smoke"
ACCESS_KEY_ENV = "AWS_ACCESS_KEY_ID"
SECRET_KEY_ENV = "AWS_SECRET_ACCESS_KEY"

HEALTH_TIMEOUT = 600  # bootstrap installs packages; allow time before the API answers
MESSAGE_LIMIT = 50_000  # mirrors the admin API's input_message cap
SMOKE_RUNTIMES = ("codex", "claude_code")
SMOKE_MANAGED_PROVIDERS = {"openai": True, "claude": True}
SMOKE_GITHUB_INTEGRATION = {"enabled": True, "write_repositories": [{"owner": "infiloop2", "repo": "trustyclaw"}]}
SMOKE_MANAGED_DOMAINS = (
    "api.openai.com",
    "auth.openai.com",
    "chatgpt.com",
    "api.anthropic.com",
    "platform.claude.com",
    "github.com",
    "api.github.com",
    "uploads.github.com",
    "codeload.github.com",
    "raw.githubusercontent.com",
    "objects.githubusercontent.com",
    "github-cloud.githubusercontent.com",
    "release-assets.githubusercontent.com",
)
SMOKE_TOOL_CALLS: dict[str, tuple[tuple[str, dict], ...]] = {
    "brave_search": (("search_web", {"query": "TrustyClaw"}),),
    "gmail": (
        ("search_messages", {}),
        ("read_message", {"message_id": "smoke-message"}),
        ("read_thread", {"thread_id": "smoke-thread"}),
        ("list_labels", {}),
        ("list_drafts", {}),
        (
            "send_email",
            {
                "to": "stage@example.com",
                "subject": "TrustyClaw smoke",
                "blocks": [{"type": "paragraph", "text": "Smoke test; never sent."}],
            },
        ),
        ("message_action", {"action": "mark_read", "message_ids": ["smoke-message"]}),
        ("label_action", {"action": "create", "name": "trustyclaw-smoke"}),
        (
            "draft_action",
            {
                "action": "create",
                "to": "stage@example.com",
                "subject": "TrustyClaw smoke draft",
                "blocks": [{"type": "paragraph", "text": "Smoke test draft."}],
            },
        ),
    ),
    "google_calendar": (
        ("read_events", {}),
        (
            "event_change",
            {
                "operation": "create",
                "summary": "TrustyClaw smoke",
                "start_time": "2099-01-01T00:00:00+00:00",
                "end_time": "2099-01-01T01:00:00+00:00",
            },
        ),
    ),
    "ibkr": (
        ("get_positions", {}),
        ("get_account_summary", {}),
        ("get_trades", {"days": "1"}),
    ),
    "instagram": (
        ("get_profile", {}),
        ("get_recent_media", {"limit": "1"}),
        ("get_publishing_limit", {}),
        ("post_reel", {"video_asset_id": "$INSTAGRAM_VIDEO"}),
    ),
    "instagram_discovery": (
        ("search_reels", {"query": "TrustyClaw", "limit": "1"}),
        ("get_trending_reels", {"limit": "1"}),
        ("search_hashtag", {"hashtag": "ai", "limit": "1"}),
        ("get_reels_by_audio", {"audio_id": "1", "limit": "1"}),
        ("get_reel_details", {"url": "https://www.instagram.com/reel/ABC123/"}),
    ),
    "linkedin": (
        ("get_profile", {}),
        ("create_post", {"text": "TrustyClaw smoke; never published."}),
    ),
    "linkedin_discovery": (
        ("search_posts", {"query": "TrustyClaw", "limit": "1"}),
    ),
    # The remaining Polymarket actions need a live market/token from this
    # run's listing. check_tools_surface derives those after these three calls.
    "polymarket": (
        ("list_markets", {"limit": "10"}),
        ("list_events", {"limit": "1"}),
        ("search", {"query": "bitcoin", "limit_per_type": "1"}),
    ),
    "runway": (
        ("generate_video", {"prompt": "TrustyClaw smoke", "image_asset_id": "$RUNWAY_IMAGE"}),
        ("edit_video", {"prompt": "TrustyClaw smoke", "video_asset_id": "$RUNWAY_VIDEO"}),
        ("generate_image", {"prompt": "TrustyClaw smoke"}),
        ("generate_speech", {"text": "TrustyClaw smoke"}),
        ("get_task", {"task_id": "trustyclaw-smoke-missing"}),
    ),
    "twitter": (
        ("search_tweets", {"query": "TrustyClaw", "max_results": "10"}),
        ("read_tweet", {"tweet_id": "1"}),
        ("user_tweets", {"username": "trustyclaw", "max_results": "5"}),
        ("get_trends", {"max_trends": "1"}),
        ("get_personalized_trends", {}),
        ("post_tweet", {"text": "TrustyClaw smoke; never published."}),
    ),
}


def managed_integrations(providers: dict[str, bool]) -> dict:
    return {provider: {"enabled": True} for provider, enabled in providers.items() if enabled}


def network_policy(providers: dict[str, bool], allowed_network_access: dict | None = None) -> dict:
    return {
        "managed_network_integrations": managed_integrations(providers),
        "allowed_network_access": allowed_network_access or {},
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.parse_args(argv)

    for tool in ("aws", "ssh", "ssh-keygen"):
        if shutil.which(tool) is None:
            print(f"error: {tool!r} is required on PATH", file=sys.stderr)
            return 2

    smoke = AwsSmoke()
    try:
        smoke.prepare()
        smoke.deploy()
        smoke.open_tunnel()
        smoke.check_health()
        smoke.check_host_config_schema()
        smoke.check_ui_page()
        smoke.check_admin_auth()
        smoke.check_initial_disabled_provider_deploy()
        smoke.check_mission_pursuit_no_login_ui()
        smoke.check_network_policy()
        smoke.check_policy_validation_and_concurrency()
        smoke.check_task_lifecycle()
        smoke.check_task_pagination()
        smoke.check_admin_concurrency()
        smoke.check_state_transactions()
        smoke.check_event_pagination()
        smoke.check_enforcement()
        smoke.check_github_read_paths()
        smoke.check_proxy_edge_cases()
        smoke.check_proxy_concurrency()
        smoke.check_pre_login_provider_guards()
        smoke.check_tools_surface()
        smoke.check_network_event_prune_race()
        print(f"\n{smoke.passed}/{smoke.total} checks passed")
        return 0 if smoke.passed == smoke.total else 1
    except Exception as exc:  # noqa: BLE001 - report, then always tear down in finally
        smoke.print_network_events("Network events before failure", since=0)
        print(f"\n[FAIL] {exc}", file=sys.stderr)
        return 1
    finally:
        smoke.teardown()


class AwsSmoke:
    def __init__(self) -> None:
        self.agent_runtime = "codex"
        self.workdir = Path(tempfile.mkdtemp(prefix="smoke-aws-"))
        self.control_socket = self.workdir / "ssh-control"
        self.ssh_key: str | None = None
        self.effective_config = self.workdir / "effective_config.json"
        self.config = None
        self.region = SMOKE_REGION
        self.result: dict | None = None
        self.tunnel_open = False
        self.passed = 0
        self.total = 0
        self.parallel_task_ids: dict[str, str] = {}  # completed parallel task id -> its token
        self.parallel_threads: dict[str, tuple[str, str]] = {}  # runtime -> (thread id, token)

    @property
    def managed_domains(self) -> tuple[str, ...]:
        return SMOKE_MANAGED_DOMAINS

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
        selected_model = model or (
            "opus" if selected_runtime == "claude_code" else "gpt-5.6-terra"
        )
        return {
            "input_message": input_message,
            "thread_id": thread_id,
            "agent_runtime": selected_runtime,
            "model": selected_model,
            "effort": effort or "high",
        }

    def follow_up_body(self, input_message: str, thread_id: str) -> dict:
        return {"input_message": input_message, "thread_id": thread_id}

    def runtime_status_record(self, status_response: dict, runtime: str | None = None) -> dict:
        runtime = runtime or self.agent_runtime
        runtimes = status_response.get("runtimes")
        if not isinstance(runtimes, list):
            raise AssertionError(f"agent runtime status has wrong shape: {status_response}")
        for item in runtimes:
            if isinstance(item, dict) and item.get("type") == runtime:
                return item
        raise AssertionError(f"agent runtime {runtime} missing from status: {status_response}")

    def enforcement_policy(self) -> dict:
        """Self-contained policy pushed at runtime, independent of deploy config.

        The /zen guard backs the percent-encoded-path check; the wildcard rule
        backs the wildcard domain check. Both managed provider bundles stay on
        so Codex and Claude Code can be logged in and interwoven in one run.
        The GitHub integration backs the repo-scope enforcement checks.
        """
        policy = network_policy(
            SMOKE_MANAGED_PROVIDERS,
            {
                "example.com": {"allow_http_methods": ["GET"], "path_guards": ["^/$", "^/zen$"]},
                "*.example.com": {"allow_http_methods": ["GET"]},
            },
        )
        policy["managed_network_integrations"]["github"] = json.loads(json.dumps(SMOKE_GITHUB_INTEGRATION))
        return policy

    # --- lifecycle ---------------------------------------------------------

    def prepare(self) -> None:
        """Build the smoke's own deploy config: generate an ephemeral operator
        SSH key and pin the region to the IAM policy's, so the caller provides
        neither a config file nor a key."""
        self.ssh_key = str(self.workdir / "operator_key")
        subprocess.run(
            ["ssh-keygen", "-t", "ed25519", "-N", "", "-q", "-C", "trustyclaw-smoke", "-f", self.ssh_key],
            check=True,
        )
        public_key = Path(f"{self.ssh_key}.pub").read_text().strip()
        raw = {
            "agent_name": SMOKE_AGENT_NAME,
            "aws_region": SMOKE_REGION,
            "aws_access_key_id_env": ACCESS_KEY_ENV,
            "aws_secret_access_key_env": SECRET_KEY_ENV,
            "operator_connections": [
                {
                    "mode": "ssh",
                    "ssh_public_key": public_key,
                }
            ],
        }
        self.effective_config.write_text(json.dumps(raw))
        self.config = load_input_config(self.effective_config)
        self.region = self.config.aws_region

    def deploy(self) -> None:
        self._step("deploy host")
        self._destroy_tagged_smoke_resources()
        env = os.environ.copy()
        env["PYTHONPATH"] = str(REPO_ROOT)
        subprocess.run(
            [
                sys.executable,
                "-m",
                "host.cli.deploy",
                "--config",
                str(self.effective_config),
                "--result-file",
                f"{self.config.agent_name}.json",
            ],
            cwd=self.workdir,
            env=env,
            check=True,
        )
        self.result = json.loads((self.workdir / f"{self.config.agent_name}.json").read_text())
        self._ok(f"instance {self.result['instance_id']} at {self.result['public_dns']}")

    def open_tunnel(self) -> None:
        self._step("open SSH tunnel to the admin API")
        self._start_tunnel()
        self._ok("tunnel established")

    def _start_tunnel(self) -> None:
        target = f"trustyclaw-operator@{self.result['public_dns']}"
        subprocess.run(
            [
                "ssh", "-fN", "-M", "-S", str(self.control_socket),
                "-i", self.ssh_key,
                "-o", "ExitOnForwardFailure=yes",
                "-o", "StrictHostKeyChecking=accept-new",
                "-o", f"UserKnownHostsFile={self.workdir / 'known_hosts'}",
                "-o", "ConnectTimeout=15",
                # Keep the tunnel alive across the interactive login wait — an
                # idle NAT/firewall would otherwise drop it and the next request
                # would see connection-refused on the local forward.
                "-o", "ServerAliveInterval=15",
                "-o", "ServerAliveCountMax=8",
                "-o", "TCPKeepAlive=yes",
                "-L", f"{ADMIN_PORT}:127.0.0.1:{ADMIN_PORT}",
                target,
            ],
            check=True,
        )
        self.tunnel_open = True

    def _reopen_tunnel(self) -> None:
        """Tear down a dead control master and start a fresh tunnel."""
        subprocess.run(
            ["ssh", "-S", str(self.control_socket), "-O", "exit",
             f"trustyclaw-operator@{self.result['public_dns']}"],
            capture_output=True,
        )
        self.tunnel_open = False
        self._start_tunnel()

    def teardown(self) -> None:
        if self.tunnel_open and self.result:
            subprocess.run(
                ["ssh", "-S", str(self.control_socket), "-O", "exit", f"trustyclaw-operator@{self.result['public_dns']}"],
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=False,
            )
        try:
            self._destroy_tagged_smoke_resources()
        finally:
            shutil.rmtree(self.workdir, ignore_errors=True)

    def _destroy_tagged_smoke_resources(self) -> None:
        print("\nTearing down tagged smoke AWS resources...")
        instance_ids = self._tagged_instance_ids("pending,running,stopping,stopped")
        if instance_ids:
            print(f"  terminating instances: {', '.join(instance_ids)}", flush=True)
            self._aws("ec2", "terminate-instances", "--instance-ids", *instance_ids)
            self._aws("ec2", "wait", "instance-terminated", "--instance-ids", *instance_ids)
        shutting_down_ids = self._tagged_instance_ids("shutting-down")
        if shutting_down_ids:
            print(f"  waiting for already-shutting-down instances: {', '.join(shutting_down_ids)}", flush=True)
            self._aws("ec2", "wait", "instance-terminated", "--instance-ids", *shutting_down_ids)

        volume_ids = self._tagged_volume_ids()
        deleted_volumes: list[str] = []
        for volume_id in volume_ids:
            try:
                self._aws("ec2", "wait", "volume-available", "--volume-ids", volume_id)
                self._aws("ec2", "delete-volume", "--volume-id", volume_id)
                self._aws("ec2", "wait", "volume-deleted", "--volume-ids", volume_id)
                deleted_volumes.append(volume_id)
            except subprocess.CalledProcessError as exc:
                print(f"warning: could not delete volume {volume_id}: {exc}", file=sys.stderr)

        group_ids = self._tagged_security_group_ids()
        deleted_groups: list[str] = []
        for group_id in group_ids:
            try:
                self._aws("ec2", "delete-security-group", "--group-id", group_id)
                deleted_groups.append(group_id)
            except subprocess.CalledProcessError as exc:
                print(f"warning: could not delete security group {group_id}: {exc}", file=sys.stderr)

        remaining = {
            "instances": self._tagged_instance_ids(),
            "volumes": self._tagged_volume_ids(),
            "security_groups": self._tagged_security_group_ids(),
        }
        if any(remaining.values()):
            raise AssertionError(f"tagged smoke AWS resources remain after teardown: {remaining}")
        print(
            "  destroyed tagged smoke resources"
            f" (instances={len(instance_ids)}, volumes={len(deleted_volumes)}, security_groups={len(deleted_groups)})",
            flush=True,
        )

    def _smoke_tag_filters(self) -> list[str]:
        return [
            f"Name=tag:trustyclaw-host-agent-name,Values={SMOKE_AGENT_NAME}",
            "Name=tag:trustyclaw-host,Values=true",
        ]

    def _tagged_instance_ids(
        self,
        states: str = "pending,running,stopping,stopped,shutting-down",
    ) -> list[str]:
        response = self._aws(
            "ec2",
            "describe-instances",
            "--filters",
            *self._smoke_tag_filters(),
            f"Name=instance-state-name,Values={states}",
        )
        ids: list[str] = []
        for reservation in response.get("Reservations", []):
            for instance in reservation.get("Instances", []):
                instance_id = instance.get("InstanceId")
                if isinstance(instance_id, str):
                    ids.append(instance_id)
        return sorted(ids)

    def _tagged_volume_ids(self) -> list[str]:
        response = self._aws("ec2", "describe-volumes", "--filters", *self._smoke_tag_filters())
        ids: list[str] = []
        for volume in response.get("Volumes", []):
            if volume.get("State") in {"deleted", "deleting"}:
                continue
            volume_id = volume.get("VolumeId")
            if isinstance(volume_id, str):
                ids.append(volume_id)
        return sorted(ids)

    def _tagged_security_group_ids(self) -> list[str]:
        response = self._aws("ec2", "describe-security-groups", "--filters", *self._smoke_tag_filters())
        ids: list[str] = []
        for group in response.get("SecurityGroups", []):
            group_id = group.get("GroupId")
            if isinstance(group_id, str):
                ids.append(group_id)
        return sorted(ids)

    # --- checks ------------------------------------------------------------

    def check_health(self) -> None:
        self._step("admin API health (waiting for bootstrap)")
        start = time.time()
        deadline = start + HEALTH_TIMEOUT
        last: dict | None = None
        last_error: str | None = None
        while time.time() < deadline:
            try:
                last = self._api("GET", "/v1/health")
                last_error = None
                if last["network_controls"]["status"] == "active":
                    break
            except Exception as exc:  # noqa: BLE001 - surface the failure mode below
                last = None
                last_error = f"{type(exc).__name__}: {exc}"
            elapsed = int(time.time() - start)
            if elapsed % 30 < 10:
                detail = last_error or (last and f"network_controls={last['network_controls']['status']}")
                print(f"[health] waiting ({elapsed}s): {detail}", flush=True)
            time.sleep(10)
        if not last or last["network_controls"]["status"] != "active":
            raise AssertionError(
                f"network controls never became active (last health: {last}, last error: {last_error})"
            )
        for key in ("cpu", "memory", "filesystem", "swap"):
            if key not in last["host_runtime"]:
                raise AssertionError(f"health missing host_runtime.{key}")
        mounts = last["host_runtime"]["filesystem"].get("mounts", {})
        for mount_name in ("root", "admin", "agent"):
            mount = mounts.get(mount_name)
            if not isinstance(mount, dict) or mount.get("total_bytes", 0) <= 0:
                raise AssertionError(f"health missing filesystem mount {mount_name}: {last['host_runtime']['filesystem']}")
        codex = self.runtime_status_record(last["agent_runtime"], "codex")
        claude = self.runtime_status_record(last["agent_runtime"], "claude_code")
        self._ok(
            f"healthy; codex runtime {codex['status']}, claude_code runtime {claude['status']}, "
            "swap and all storage mounts reported"
        )

    def check_host_config_schema(self) -> None:
        self._step("deployed host config schema")
        config = json.loads(self._ssh_code(
            "sudo -u postgres psql -tA -d trustyclaw_admin -c \""
            "SELECT json_build_object("
            "'agent_name', c.agent_name,"
            " 'admin_password_sha256', c.admin_password_sha256,"
            " 'operator_connections', COALESCE((SELECT json_agg(json_strip_nulls(json_build_object("
            "'mode', o.mode, 'ssh_public_key', o.ssh_public_key,"
            " 'hostname', o.hostname, 'tunnel_token', o.tunnel_token)) ORDER BY o.mode)"
            " FROM operator_connections o), '[]'::json))::text FROM config c\""
        ))
        if config.get("agent_name") != SMOKE_AGENT_NAME:
            raise AssertionError(f"host config has wrong agent_name: {config}")
        if not config.get("admin_password_sha256"):
            raise AssertionError(f"host config missing password hash: {config}")
        if self.ssh_key is None:
            raise AssertionError("smoke SSH key was not prepared")
        expected_connections = [{"mode": "ssh", "ssh_public_key": Path(f"{self.ssh_key}.pub").read_text().strip()}]
        if config.get("operator_connections") != expected_connections:
            raise AssertionError(f"host config has wrong operator connections: {config}")
        # The config schema is typed columns now; deployment-only inputs
        # cannot exist as stray keys, pinned by the exact column set.
        config_columns = set(self._ssh_code(
            "sudo -u postgres psql -tA -d trustyclaw_admin -c "
            "\"SELECT column_name FROM information_schema.columns WHERE table_name = 'config'\""
        ).split())
        if config_columns != {"singleton", "agent_name", "admin_password_sha256"}:
            raise AssertionError(f"config table has unexpected columns: {sorted(config_columns)}")
        # Proxy account pins live in the proxy_provider_pins table now
        # (missing rows are the no-pin default until a login lands).
        pin_rows = self._ssh_code(
            "sudo -u postgres psql -tA -d trustyclaw_admin "
            "-c \"SELECT provider FROM proxy_provider_pins WHERE provider NOT IN ('openai', 'claude')\""
        ).strip()
        if pin_rows:
            raise AssertionError(f"unexpected proxy_provider_pins rows: {pin_rows}")
        # Admin-side provider account records live in the database, in the
        # provider_accounts table (empty or explicit-null records until login).
        admin_accounts = self._ssh_code(
            "sudo -u postgres psql -tA -d trustyclaw_admin "
            "-c \"SELECT provider FROM provider_accounts WHERE provider NOT IN ('openai', 'claude')\""
        ).strip()
        if admin_accounts:
            raise AssertionError(f"unexpected provider_accounts rows: {admin_accounts}")
        storage_layout = json.loads(self._ssh_code(
            "sudo python3 - <<'PY'\n"
            "import grp, json, os, pwd\n"
            "paths = [\n"
            "    '/mnt/trustyclaw-admin',\n"
            "    '/mnt/trustyclaw-admin/postgres',\n"
            "    '/mnt/trustyclaw-admin/postgres/14/main',\n"
            "    '/mnt/trustyclaw-admin/proxy-state',\n"
            "    '/mnt/trustyclaw-admin/proxy-state/network_proxy_ca.key',\n"
            "    '/mnt/trustyclaw-admin/proxy-state/network_proxy_ca.crt',\n"
            "]\n"
            "result = {}\n"
            "for path in paths:\n"
            "    st = os.lstat(path)\n"
            "    result[path] = {'owner': pwd.getpwuid(st.st_uid).pw_name,\n"
            "                    'group': grp.getgrgid(st.st_gid).gr_name,\n"
            "                    'mode': oct(st.st_mode & 0o777),\n"
            "                    'symlink': os.path.islink(path)}\n"
            "print(json.dumps(result))\n"
            "PY"
        ))
        service_ids = json.loads(self._ssh_code(
            "sudo python3 - <<'PY'\n"
            "import grp, json, pwd\n"
            "names = ['trustyclaw-admin', 'trustyclaw-proxy', 'trustyclaw-agent', 'cloudflared', 'postgres']\n"
            "print(json.dumps({name: {'uid': pwd.getpwnam(name).pw_uid, 'gid': grp.getgrnam(name).gr_gid} for name in names}))\n"
            "PY"
        ))
        expected_service_ids = {
            "trustyclaw-admin": {"uid": 47741, "gid": 47741},
            "trustyclaw-proxy": {"uid": 47742, "gid": 47742},
            "trustyclaw-agent": {"uid": 47743, "gid": 47743},
            "cloudflared": {"uid": 47744, "gid": 47744},
            "postgres": {"uid": 47745, "gid": 47745},
        }
        if service_ids != expected_service_ids:
            raise AssertionError(f"service IDs are not stable: {service_ids}")
        agent_ca_access = self._ssh_code(
            "sudo -u trustyclaw-agent bash -c "
            "'test -r /usr/local/share/ca-certificates/trustyclaw-network-proxy.crt && "
            "! test -r /mnt/trustyclaw-admin/proxy-state/network_proxy_ca.crt && echo ok'"
        ).strip()
        if agent_ca_access != "ok":
            raise AssertionError("agent must read only the installed proxy CA copy, not proxy-state directly")
        partition_access = self._ssh_code(
            "sudo -u trustyclaw-admin bash -c '! test -r /mnt/trustyclaw-admin/proxy-state/network_proxy_ca.key' && "
            "sudo -u trustyclaw-proxy bash -c '! test -r /mnt/trustyclaw-admin/postgres/14/main/pg_hba.conf' && "
            "echo ok"
        ).strip()
        if partition_access != "ok":
            raise AssertionError("proxy-state and the Postgres data directory must be unreadable across service users")
        # The admin role has full access; the proxy role may connect (its
        # narrow per-table grants are pinned by the network event storm check)
        # but must not be able to create objects; the agent has no role at all.
        database_access = self._ssh_code(
            "sudo -u trustyclaw-admin psql -tA -d trustyclaw_admin -c 'SELECT 1' && "
            "sudo -u trustyclaw-proxy psql -tA -d trustyclaw_admin -c 'SELECT 2' && "
            "sudo -u trustyclaw-proxy bash -c '! psql -tA -d trustyclaw_admin -c \"CREATE TABLE smoke_illegal (n INT)\" 2>/dev/null' && "
            "sudo -u trustyclaw-agent bash -c '! psql -tA -d trustyclaw_admin -c \"SELECT 1\" 2>/dev/null' && "
            "echo ok"
        ).strip().splitlines()
        if database_access != ["1", "2", "ok"]:
            raise AssertionError(
                f"database access must be admin-full, proxy-narrow, agent-none: {database_access}"
            )
        expected_layout = {
            "/mnt/trustyclaw-admin": ("root", "root", "0o711", False),
            "/mnt/trustyclaw-admin/postgres": ("root", "root", "0o711", False),
            "/mnt/trustyclaw-admin/postgres/14/main": ("postgres", "postgres", "0o700", False),
            "/mnt/trustyclaw-admin/proxy-state": ("trustyclaw-proxy", "trustyclaw-proxy", "0o700", False),
            "/mnt/trustyclaw-admin/proxy-state/network_proxy_ca.key": ("trustyclaw-proxy", "trustyclaw-proxy", "0o600", False),
            "/mnt/trustyclaw-admin/proxy-state/network_proxy_ca.crt": ("trustyclaw-proxy", "trustyclaw-proxy", "0o644", False),
        }
        for path, expected_values in expected_layout.items():
            entry = storage_layout.get(path, {})
            actual = (entry.get("owner"), entry.get("group"), entry.get("mode"), entry.get("symlink"))
            if actual != expected_values:
                raise AssertionError(f"{path} ownership/mode mismatch: {entry}")
        self._ok(
            "host config persists name/password in the database; database and proxy state are private per service user"
        )

    def check_network_policy(self) -> None:
        self._step("network policy get/replace")
        self._api("PUT", "/v1/network/policy", self.enforcement_policy())
        policy = self._api("GET", "/v1/network/policy")
        controls = policy["network_controls"]
        expected_provider = self.enforcement_policy()["managed_network_integrations"]
        if controls.get("managed_network_integrations") != expected_provider:
            raise AssertionError(f"policy did not preserve explicit managed provider: {controls}")
        rules = controls["allowed_network_access"]
        if "example.com" not in rules:
            raise AssertionError("replaced policy not reflected in GET")
        for host in self.managed_domains:
            if host in rules:
                raise AssertionError(f"managed provider rule {host} leaked into API policy response: {rules}")
        # The stored policy is typed rows now; check them directly.
        stored_integrations = set(self._ssh_code(
            "sudo -u postgres psql -tA -d trustyclaw_admin -c 'SELECT integration FROM managed_integrations'"
        ).split())
        expected_enabled = set(expected_provider)
        if stored_integrations != expected_enabled:
            raise AssertionError(
                f"stored policy did not preserve enabled integrations: {sorted(stored_integrations)}"
            )
        stored_repos = set(self._ssh_code(
            "sudo -u postgres psql -tA -d trustyclaw_admin -c "
            "\"SELECT owner || '/' || repo FROM github_repositories\""
        ).split())
        expected_repos = {
            f"{repo['owner']}/{repo['repo']}" for repo in SMOKE_GITHUB_INTEGRATION["write_repositories"]
        }
        if stored_repos != expected_repos:
            raise AssertionError(f"stored GitHub write-repository list mismatch: {sorted(stored_repos)}")
        stored_domains = set(self._ssh_code(
            "sudo -u postgres psql -tA -d trustyclaw_admin -c 'SELECT domain FROM allowed_domains'"
        ).split())
        if "example.com" not in stored_domains:
            raise AssertionError(f"replaced policy not reflected in stored rows: {sorted(stored_domains)}")
        for host in self.managed_domains:
            if host in stored_domains:
                raise AssertionError(f"managed integration rule {host} leaked into stored policy: {sorted(stored_domains)}")
        self._check_github_credential_lifecycle()
        self._ok("policy read back and stored user-facing; proxy expands managed AI provider rules in memory")

    def _check_github_credential_lifecycle(self) -> None:
        smoke_token = f"github_pat_smoke_{time.time_ns()}"
        metadata = self._api("GET", "/v1/network-tools/github-credential")
        if metadata.get("configured") is not False:
            raise AssertionError(f"initial GitHub credential should be unconfigured: {metadata}")
        saved = self._api("PUT", "/v1/network-tools/github-credential", {"mode": "pat", "token": smoke_token})
        if saved.get("configured") is not True or smoke_token in json.dumps(saved):
            raise AssertionError(f"GitHub credential PUT should return metadata only: {saved}")
        # The working token lives in the proxy-readable row as secretbox
        # ciphertext — encrypted at rest like every other stored secret, and
        # never on disk anywhere.
        published = self._ssh_code(
            "sudo -u postgres psql -tA -d trustyclaw_admin -c 'SELECT token FROM proxy_github_token'"
        ).strip()
        if not published.startswith("enc:v1:") or smoke_token in published:
            raise AssertionError("the published proxy_github_token row must hold ciphertext, not the token")
        no_file = self._ssh_code("sudo bash -c '! test -e /etc/trustyclaw-github && echo absent'").strip()
        if no_file != "absent":
            raise AssertionError("the agent token-file directory should not exist in the injection era")
        # The proxy injects the stored (fake) PAT into agent GitHub requests:
        # gh runs with a fixed placeholder GH_TOKEN, the proxy strips it and
        # injects the smoke token, and GitHub answers 401 for the bad
        # credential — proof the injected token (not the placeholder) reached
        # GitHub. gh's own "not logged in" refusal would mean a broken shim.
        proxy = f"http://127.0.0.1:{PROXY_PORT}"
        gh_injected = self._ssh_code(
            f"sudo -u trustyclaw-agent env HTTPS_PROXY={proxy} https_proxy={proxy} "
            "gh api repos/infiloop2/trustyclaw 2>&1 || true"
        )
        if "401" not in gh_injected or "gh auth login" in gh_injected:
            raise AssertionError(f"proxy did not inject the stored token upstream: {gh_injected!r}")
        # GraphQL is denied at the proxy regardless of credentials, so gh's
        # GraphQL-backed path fails with the proxy's 403, not a GitHub 401.
        gh_graphql = self._ssh_code(
            f"sudo -u trustyclaw-agent env HTTPS_PROXY={proxy} https_proxy={proxy} "
            "gh api graphql -f query='query{viewer{login}}' 2>&1 || true"
        )
        if "403" not in gh_graphql:
            raise AssertionError(f"gh graphql should be denied by the proxy: {gh_graphql!r}")
        deleted = self._api("DELETE", "/v1/network-tools/github-credential")
        if deleted.get("configured") is not False:
            raise AssertionError(f"GitHub credential DELETE should clear metadata: {deleted}")
        rows = self._ssh_code(
            "sudo -u postgres psql -tA -d trustyclaw_admin -c 'SELECT count(*) FROM proxy_github_token'"
        ).strip()
        if rows != "0":
            raise AssertionError("proxy_github_token should be cleared after DELETE")
        # With the row cleared, the same request goes upstream with the
        # placeholder stripped and nothing injected: the public repo read
        # succeeds unauthenticated — revocation is instant, and agent-supplied
        # credentials demonstrably never pass through.
        gh_uninjected = self._ssh_code(
            f"sudo -u trustyclaw-agent env HTTPS_PROXY={proxy} https_proxy={proxy} "
            "gh api repos/infiloop2/trustyclaw 2>&1 || true"
        )
        if '"full_name"' not in gh_uninjected or "401" in gh_uninjected:
            raise AssertionError(f"unauthenticated public read should succeed after DELETE: {gh_uninjected!r}")

    def check_initial_disabled_provider_deploy(self) -> None:
        self._step("initial deploy with managed providers disabled")
        expected_empty_policy = {
            "managed_network_integrations": {},
            "allowed_network_access": {},
        }
        stored_rows = self._ssh_code(
            "sudo -u postgres psql -tA -d trustyclaw_admin -c 'SELECT count(*) FROM network_policy'"
        ).strip()
        if stored_rows != "0":
            raise AssertionError(
                f"a fresh deploy must not seed a policy row (missing row = fail-closed empty default): {stored_rows}"
            )
        policy = self._api("GET", "/v1/network/policy")["network_controls"]
        if policy != expected_empty_policy:
            raise AssertionError(f"initial API policy should be empty: {policy}")
        health = self._api("GET", "/v1/health")
        if health["network_controls"]["status"] != "active":
            raise AssertionError(f"empty valid policy should report derived active network status: {health}")
        if policy.get("managed_network_integrations", {}) != {}:
            raise AssertionError(f"initial policy should keep managed providers disabled: {policy}")
        if policy.get("allowed_network_access") != {}:
            raise AssertionError(f"initial policy should not have user network rules: {policy}")
        for runtime in SMOKE_RUNTIMES:
            status = self._wait_for_runtime_status({"deactivated"}, runtime=runtime, timeout=90)
            if status != "deactivated":
                raise AssertionError(f"{runtime} should start deactivated when its provider is disabled, got {status}")
            account = self._agent_account(runtime)
            if account.get("status") != "deactivated":
                raise AssertionError(f"{runtime} account summary should report deactivated: {account}")
            if "account_id" in account or "email" in account:
                raise AssertionError(f"{runtime} account summary leaked account identity while deactivated: {account}")
        for label, login_path in (
            ("initial-codex-disabled-login", "/v1/agent-runtime/codex-oauth-login"),
            ("initial-claude-disabled-login", "/v1/agent-runtime/claude-oauth-login"),
        ):
            status, body = self._api_status("POST", login_path)
            if status != 409:
                raise AssertionError(f"{login_path} while initially deactivated returned {status}, expected 409: {body}")
        self._api("PUT", "/v1/network/policy", self.enforcement_policy())
        for runtime in SMOKE_RUNTIMES:
            status = self._wait_for_runtime_status({"loading", "awaiting_login", "active"}, runtime=runtime, timeout=120)
            if status not in {"loading", "awaiting_login", "active"}:
                raise AssertionError(f"{runtime} did not wake after enabling provider access, got {status}")
        self._ok("first boot creates an empty derived-active policy; providers omitted keep runtimes deactivated")

    def check_ui_page(self) -> None:
        self._step("admin UI page served at / without auth")
        request = urllib.request.Request(f"http://127.0.0.1:{ADMIN_PORT}/")
        with urllib.request.urlopen(request, timeout=30) as response:
            content_type = response.headers.get("Content-Type", "")
            self._assert_admin_ui_security_headers(response.headers)
            page = response.read().decode()
        if "text/html" not in content_type:
            raise AssertionError(f"UI page content type is {content_type!r}")
        if "TrustyClaw" not in page:
            raise AssertionError("UI page does not look like the admin UI")
        for path, expected_type in (
            ("/admin_ui.css", "text/css"),
            ("/admin_ui/app.js", "application/javascript"),
            ("/admin_ui/api.js", "application/javascript"),
            ("/admin_ui/helpers.js", "application/javascript"),
            ("/admin_ui/health.js", "application/javascript"),
            ("/admin_ui/threads.js", "application/javascript"),
            ("/admin_ui/files.js", "application/javascript"),
            ("/admin_ui/processes.js", "application/javascript"),
            ("/admin_ui/logs.js", "application/javascript"),
            ("/admin_ui/network.js", "application/javascript"),
            ("/admin_ui/tools.js", "application/javascript"),
            ("/admin_ui/integration_catalog.js", "application/javascript"),
            ("/admin_ui/connection_guide.js", "application/javascript"),
        ):
            request = urllib.request.Request(f"http://127.0.0.1:{ADMIN_PORT}{path}")
            with urllib.request.urlopen(request, timeout=30) as response:
                asset_type = response.headers.get("Content-Type", "")
                self._assert_admin_ui_security_headers(response.headers)
                asset_body = response.read().decode()
            if expected_type not in asset_type:
                raise AssertionError(f"UI asset {path} content type is {asset_type!r}")
            page += "\n" + asset_body
        for path in (
            "/guide-assets/google-auth-app-information.png",
            "/guide-assets/google-auth-data-access.png",
            "/guide-assets/google-auth-web-client.png",
        ):
            request = urllib.request.Request(f"http://127.0.0.1:{ADMIN_PORT}{path}")
            with urllib.request.urlopen(request, timeout=30) as response:
                asset_type = response.headers.get("Content-Type", "")
                self._assert_admin_ui_security_headers(response.headers)
                asset_body = response.read()
            if "image/png" not in asset_type or not asset_body.startswith(b"\x89PNG\r\n\x1a\n"):
                raise AssertionError(f"UI guide asset {path} is not a PNG ({asset_type!r})")
        for expected in (
            '<link rel="stylesheet" href="/admin_ui.css">',
            '<script type="module" src="/admin_ui/app.js"></script>',
            "<h2>Sessions</h2>",
            "/v1/threads",
            "/v1/threads/${encodeURIComponent(selectedThreadId)}/tasks",
            "/v1/tasks/${encodeURIComponent(taskId)}/events",
            "showThread",
            "showTaskEvents",
            'data-action="show-thread"',
            'data-action="show-task-events"',
            "button[data-action]",
            "TASK_EVENT_PAGE_BATCH",
            "loadMoreTaskEvents",
            'id="panel-home"',
            'id="panel-agent"',
            'id="panel-processes"',
            'id="panel-network"',
            'id="processes"',
            "/v1/agent-processes",
            "refreshAgentProcesses",
            'id="runtime-overview"',
            'data-integration-message="custom_domain"',
            'data-action="start-login"',
            'Start ${esc(runtimeLabel)} login',
            'data-action="reset-linked-account"',
            'Disconnect</button>',
            "usageRing",
            'id="ai-inference-integrations"',
            'id="tools"',
            "AI Inference",
            "Manual",
            'id="github-repos"',
            'id="domain-rules"',
            'id="github-repo"',
            'id="github-token"',
            'id="github-credential-mode"',
            'id="github-app-fields"',
            'id="github-app-id"',
            'id="github-app-installation-id"',
            'id="github-app-private-key"',
            'id="github-credential-status"',
            'id="preset-info-popover"',
            "<span>Internet Access and Tools</span>",
            "Reboot host",
            "Custom Domain Access",
            "Add domain rule",
            "MANAGED_INTEGRATIONS",
            "integration_catalog.js",
            "toggleIntegrationInfo",
            "renderIntegrationInfo",
            "objectValue",
            "!Array.isArray(value)",
            "activeNetworkPolicy",
            "clonePolicy",
            "publishPolicy",
            "setIntegrationEnabled",
            "renderNetworkControls",
            "renderManagedIntegrations",
            "renderGithubRepos",
            "renderDomainRules",
            "addDomainRule",
            "removeDomainRule",
            'data-action="enable-integration"',
            'data-action="disable-integration"',
            'data-action="remove-github-repo"',
            'data-action="remove-domain-rule"',
            'data-action="add-github-repo"',
            'data-action="set-github-credential"',
            'data-action="delete-github-credential"',
            'data-action="add-domain-rule"',
            'id="github-expansion"',
            'id="github-credential-clear"',
            'data-action="recheck-github-audit"',
            "renderGithubAudit",
            "recheckGithubAudit",
            "audit-banner",
            "/v1/network-tools/github-audit",
            "Connect your OpenAI subscription and let your agent use Codex for tasks and cached web search.",
            "Connect your Anthropic subscription and let your agent use Claude Code for tasks. Web search is optional and off by default.",
            "OpenAI's cached web search",
            "Reads can reach any public repository",
            "api.openai.com",
            "auth.openai.com",
            "chatgpt.com",
            "api.anthropic.com",
            "platform.claude.com",
            "api.github.com",
            "uploads.github.com",
            "codeload.github.com",
            "objects.githubusercontent.com",
            "github-cloud.githubusercontent.com",
            "raw.githubusercontent.com",
            "release-assets.githubusercontent.com",
            "pypi.org",
            "files.pythonhosted.org",
            "nodejs.org",
            "registry.npmjs.org",
            "Add domain rule",
            'id="tools"',
            'data-action="toggle-tool-expansion"',
            "toggleToolExpansion",
            "toggleInfoPopover",
            "tool-approvals-table",
            "refreshTools",
            "refreshExpandedToolApprovals",
            "decideToolApproval",
            'data-action="enable-tool"',
            'data-action="save-tool-config"',
            "connectTool",
            "completeToolConnect",
            "/oauth/callback",
            "/v1/tools",
            'id="panel-tool-log"',
            'id="tool-events"',
            "Tool audit log",
            "/v1/tools/events",
            "tool-page",
            'id="tab-connection-guide"',
            'id="panel-connection-guide"',
            "refreshConnectionGuide",
            "View integration guide",
            "setup_steps",
        ):
            if expected not in page:
                raise AssertionError(f"UI page is missing expected admin UI fragment {expected!r}")
        for removed in (
            "onclick=",
            "oninput=",
            "/v1/tasks/finished",
            "loadFinishedTasks",
            "loadAllTaskEvents",
            "retained_task_count",
            "ssh_port_opened",
            "editWebsiteRule",
            "removeWebsiteRule",
            'id="policy-provider-openai"',
            'id="policy-provider-claude"',
            'id="policy-websites"',
            "Policy preset applied",
            "Network policy replaced",
        ):
            if removed in page:
                raise AssertionError(f"UI page still contains removed finished-task path {removed!r}")
        self._ok("static UI page served unauthenticated; thread history and network policy controls present; API routes still require auth")

    def check_mission_pursuit_no_login_ui(self) -> None:
        """Drive the fresh workspace through the real admin shell and app UI.

        A fresh smoke deliberately has no provider login. That still proves
        the complete operator path through task creation and its clear
        fail-fast result, without granting the smoke a provider account.
        """
        self._step("Mission Pursuit browser flow before provider login")
        for runtime in SMOKE_RUNTIMES:
            status = self._wait_for_runtime_status({"awaiting_login"}, runtime=runtime, timeout=180)
            if status != "awaiting_login":
                raise AssertionError(f"fresh {runtime} runtime settled at {status}, expected awaiting_login")

        password = json.dumps(self.result["admin_password"])
        message = "Build a practical launch plan for a neighborhood repair cafe."
        with ChromeBrowser(f"http://127.0.0.1:{ADMIN_PORT}/") as browser:
            browser.wait_for(
                "document.getElementById('login') && !document.getElementById('login').hidden",
                description="the admin login screen",
            )
            browser.evaluate(
                f"document.getElementById('password').value={password};"
                "document.querySelector('[data-action=login]').click(); true"
            )
            browser.wait_for(
                "document.getElementById('app') && !document.getElementById('app').hidden",
                description="admin login to succeed",
            )
            browser.wait_for(
                "document.getElementById('app-tabs').textContent.includes('Mission Pursuit')",
                description="Mission Pursuit app discovery",
            )
            browser.evaluate("document.getElementById('tab-app-mission_pursuit').click(); true")
            app = browser.target("/v1/apps/mission_pursuit/ui/index.html")
            app.wait_for(
                "document.getElementById('hero') && !document.getElementById('hero').hidden",
                description="the fresh Mission Pursuit workspace",
            )
            if app.evaluate("document.querySelectorAll('#info-close').length") != 0:
                raise AssertionError("How it works unexpectedly has a close button")

            app.evaluate("document.getElementById('info-toggle').click(); true")
            app.wait_for(
                "!document.getElementById('info-popover').hidden",
                description="How it works popover to open",
            )
            info_text = app.evaluate("document.getElementById('info-popover').innerText")
            for expected in (
                "Set the mission together",
                "Work through artifacts",
                "Keep moving while you are away",
            ):
                if expected not in str(info_text):
                    raise AssertionError(f"How it works is missing {expected!r}: {info_text!r}")
            app.evaluate("document.body.dispatchEvent(new PointerEvent('pointerdown', {bubbles: true})); true")
            app.wait_for(
                "document.getElementById('info-popover').hidden",
                description="How it works to dismiss outside",
            )

            bounds = (
                "JSON.stringify((() => { const r=document.getElementById('hero').getBoundingClientRect();"
                "return [r.x,r.y,r.width,r.height]; })())"
            )
            before = app.evaluate(bounds)
            app.evaluate("document.getElementById('agent-settings-toggle').click(); true")
            app.wait_for(
                "!document.getElementById('agent-settings-popover').hidden",
                description="agent settings to open",
            )
            after = app.evaluate(bounds)
            if before != after:
                raise AssertionError(f"opening agent settings shifted the workspace: {before} -> {after}")
            app.evaluate(
                "document.getElementById('agent-runtime').value='claude_code';"
                "document.getElementById('agent-runtime')"
                ".dispatchEvent(new Event('change',{bubbles:true})); true"
            )
            app.wait_for(
                "document.getElementById('agent-runtime').value === 'claude_code' && "
                "!document.getElementById('agent-settings-apply').disabled",
                description="Claude Code draft settings",
            )
            app.evaluate("document.getElementById('agent-settings-apply').click(); true")
            app.wait_for(
                "document.getElementById('agent-settings-popover').hidden && "
                "document.getElementById('agent-settings-toggle').textContent.includes('Claude Code')",
                description="Claude Code draft settings to apply",
            )

            app.evaluate(
                f"document.getElementById('hero-input').value={json.dumps(message)};"
                "document.getElementById('hero-send').click(); true"
            )
            app.wait_for(
                "!document.getElementById('workspace').hidden && "
                f"document.getElementById('feed').innerText.includes({json.dumps(message)})",
                description="the first mission message to appear",
            )
            app.wait_for(
                "document.getElementById('feed').innerText.includes('Agent turn failed:') && "
                "document.getElementById('feed').innerText.includes('awaiting_login') && "
                "document.getElementById('busy-bar').hidden",
                timeout=120,
                description="the no-login turn to fail clearly",
            )
            headings = app.evaluate(
                "[...document.querySelectorAll('.conversation-head h2, .rail-card .rail-head h2')]"
                ".map(element => element.textContent.trim())"
            )
            expected_headings = ["Conversation", "Artifacts", "Schedules", "Memory", "Tools"]
            if headings != expected_headings:
                raise AssertionError(
                    f"Mission Pursuit workspace headings are {headings!r}, expected {expected_headings!r}"
                )
            empty_surfaces = app.evaluate(
                "[document.getElementById('artifacts').textContent,"
                " document.getElementById('memories').textContent,"
                " document.getElementById('tools').textContent]"
            )
            for actual, expected in zip(
                empty_surfaces,
                ("No artifacts yet", "Nothing remembered yet", "No tools recorded"),
                strict=True,
            ):
                if expected not in str(actual):
                    raise AssertionError(f"fresh Mission Pursuit surface is missing {expected!r}: {actual!r}")
            if not app.evaluate(
                "Boolean(document.getElementById('hero-send').querySelector('svg') && "
                "document.getElementById('chat-send').querySelector('svg'))"
            ):
                raise AssertionError("Mission Pursuit send controls are not integrated icon buttons")

            app.evaluate("document.getElementById('agent-settings-toggle').click(); true")
            app.wait_for(
                "!document.getElementById('agent-settings-popover').hidden",
                description="persisted agent settings to open",
            )
            app.evaluate(
                "document.getElementById('agent-runtime').value='codex';"
                "document.getElementById('agent-runtime')"
                ".dispatchEvent(new Event('change',{bubbles:true})); true"
            )
            app.wait_for(
                "!document.getElementById('agent-settings-warning').hidden && "
                "!document.getElementById('agent-settings-apply').disabled",
                description="short-term-memory warning for a runtime switch",
            )
            app.evaluate("document.getElementById('agent-settings-apply').click(); true")
            app.wait_for(
                "document.getElementById('agent-settings-popover').hidden && "
                "document.getElementById('agent-settings-toggle').textContent.includes('Codex') && "
                "document.getElementById('feed').innerText.includes('Switched to Codex')",
                description="the persisted runtime switch",
            )

        snapshot = self._app_api("GET", "/v1/apps/mission_pursuit/api/workspace")
        if snapshot.get("workspace", {}).get("agent_runtime") != "codex":
            raise AssertionError(f"Mission Pursuit did not persist the UI runtime switch: {snapshot}")
        if not any(item.get("content") == message for item in snapshot.get("messages", [])):
            raise AssertionError(f"Mission Pursuit did not persist the browser message: {snapshot}")
        if snapshot.get("busy"):
            raise AssertionError(f"Mission Pursuit left the failed no-login turn busy: {snapshot['busy']}")
        self._ok("real browser covered login, app navigation, popovers, settings, first turn, and fail-fast UI")

    @staticmethod
    def _assert_admin_ui_security_headers(headers) -> None:
        csp = headers.get("Content-Security-Policy", "")
        required_directives = (
            "default-src 'self'",
            "connect-src 'self'",
            "script-src 'self'",
            "style-src 'self'",
            "img-src 'self' data:",
            "frame-ancestors 'none'",
        )
        missing = [directive for directive in required_directives if directive not in csp]
        if missing:
            raise AssertionError(f"admin UI Content-Security-Policy missing {missing}: {csp!r}")
        expected_headers = {
            "Referrer-Policy": "no-referrer",
            "X-Content-Type-Options": "nosniff",
            "X-Frame-Options": "DENY",
        }
        for name, expected in expected_headers.items():
            actual = headers.get(name, "")
            if actual != expected:
                raise AssertionError(f"admin UI {name} header is {actual!r}, expected {expected!r}")

    def check_admin_auth(self) -> None:
        self._step("admin API authentication")
        status, _ = self._api_status("GET", "/v1/agent-runtime/status", bearer=None)
        if status != 401:
            raise AssertionError(f"request without credentials returned {status}, expected 401")
        status, _ = self._api_status("GET", "/v1/agent-runtime/status", bearer="wrong-password")
        if status != 401:
            raise AssertionError(f"request with a wrong password returned {status}, expected 401")
        # The UI page is the one unauthenticated route.
        request = urllib.request.Request(f"http://127.0.0.1:{ADMIN_PORT}/")
        with urllib.request.urlopen(request, timeout=30) as response:
            page = response.read()
        if b"<html" not in page.lower():
            raise AssertionError("GET / did not serve the admin UI page")
        malformed = self._raw_local_http(
            ADMIN_PORT,
            b"POST /v1/tasks HTTP/1.1\r\n"
            b"Host: 127.0.0.1\r\n"
            + f"Authorization: Bearer {self.result['admin_password']}\r\n".encode()
            + b"Content-Length: nope\r\n\r\n",
        )
        huge = self._raw_local_http(
            ADMIN_PORT,
            b"POST /v1/tasks HTTP/1.1\r\n"
            b"Host: 127.0.0.1\r\n"
            + f"Authorization: Bearer {self.result['admin_password']}\r\n".encode()
            + b"Content-Length: 1048577\r\n\r\n",
        )
        if b"400" not in malformed or b"malformed Content-Length" not in malformed:
            raise AssertionError(f"admin API malformed Content-Length was not rejected cleanly: {malformed[:300]!r}")
        if b"413" not in huge or b"request body too large" not in huge:
            raise AssertionError(f"admin API huge Content-Length was not rejected cleanly: {huge[:300]!r}")
        self._ok("401 without/with wrong credentials; UI served unauthenticated; malformed admin bodies fail closed")

    def check_policy_validation_and_concurrency(self) -> None:
        self._step("policy validation and concurrent replaces")
        # SSH is deployment config, not runtime network policy.
        pinned = network_policy(SMOKE_MANAGED_PROVIDERS)
        pinned["ssh_port_opened"] = False
        status, body = self._api_status("PUT", "/v1/network/policy", pinned)
        if status != 400:
            raise AssertionError(f"runtime ssh_port_opened field returned {status}, expected 400: {body}")
        invalid = network_policy(SMOKE_MANAGED_PROVIDERS, {"api.example.com": {"allow_http_methods": ["BOGUS"]}})
        status, body = self._api_status("PUT", "/v1/network/policy", invalid)
        if status != 400:
            raise AssertionError(f"invalid policy returned {status}, expected 400: {body}")
        disabled_provider_policy = {"allowed_network_access": {}}
        status, body = self._api_status(
            "PUT", "/v1/network/policy", disabled_provider_policy
        )
        if status != 200:
            raise AssertionError(f"disabling all managed providers returned {status}, expected 200: {body}")
        for runtime in SMOKE_RUNTIMES:
            disabled_status = self._wait_for_runtime_status({"deactivated"}, runtime=runtime, timeout=60)
            if disabled_status != "deactivated":
                raise AssertionError(f"{runtime} did not deactivate after provider access was disabled")
        for label, login_path in (
            ("codex-provider-disabled-login", "/v1/agent-runtime/codex-oauth-login"),
            ("claude-provider-disabled-login", "/v1/agent-runtime/claude-oauth-login"),
        ):
            status, _ = self._api_status("POST", login_path)
            if status != 409:
                raise AssertionError(f"{login_path} while provider is deactivated returned {status}, expected 409")

        for label, providers, enabled_runtime, disabled_runtime, disabled_login_path in (
            (
                "openai-only",
                {"openai": True},
                "codex",
                "claude_code",
                "/v1/agent-runtime/claude-oauth-login",
            ),
            (
                "claude-only",
                {"claude": True},
                "claude_code",
                "codex",
                "/v1/agent-runtime/codex-oauth-login",
            ),
        ):
            policy = network_policy(providers)
            status, body = self._api_status("PUT", "/v1/network/policy", policy)
            if status != 200:
                raise AssertionError(f"{label} provider policy returned {status}, expected 200: {body}")
            enabled_status = self._wait_for_runtime_status(
                {"loading", "awaiting_login", "active"}, runtime=enabled_runtime, timeout=120
            )
            if enabled_status not in {"loading", "awaiting_login", "active"}:
                raise AssertionError(f"{label} should enable {enabled_runtime}, got {enabled_status}")
            disabled_status = self._wait_for_runtime_status({"deactivated"}, runtime=disabled_runtime, timeout=60)
            if disabled_status != "deactivated":
                raise AssertionError(f"{label} should deactivate {disabled_runtime}, got {disabled_status}")
            status, _ = self._api_status("POST", disabled_login_path)
            if status != 409:
                raise AssertionError(f"{label} disabled runtime login returned {status}, expected 409")
            print(
                f"  {label}: {enabled_runtime} status {enabled_status}; {disabled_runtime} deactivated",
                flush=True,
            )

        for label, bad_policy, expected_error in (
            (
                "self-managed-openai-domain",
                {
                    "managed_network_integrations": managed_integrations({"openai": True, "claude": True}),
                    "allowed_network_access": {"chatgpt.com": {"allow_http_methods": ["GET"]}},
                },
                "managed_network_integrations.openai",
            ),
            (
                "self-managed-claude-domain",
                {
                    "managed_network_integrations": managed_integrations({"openai": True, "claude": True}),
                    "allowed_network_access": {"api.anthropic.com": {"allow_http_methods": ["POST"]}},
                },
                "managed_network_integrations.claude",
            ),
            (
                "user-openai-managed-flag",
                {
                    "managed_network_integrations": managed_integrations(SMOKE_MANAGED_PROVIDERS),
                    "allowed_network_access": {
                        "api.example.com": {
                            "allow_http_methods": ["GET"],
                            "openai_external_url_request_guard": True,
                        }
                    },
                },
                "unsupported fields",
            ),
            (
                "user-openai-account-guard-flag",
                {
                    "managed_network_integrations": managed_integrations(SMOKE_MANAGED_PROVIDERS),
                    "allowed_network_access": {
                        "api.example.com": {
                            "allow_http_methods": ["GET"],
                            "openai_account_guard": True,
                        }
                    },
                },
                "unsupported fields",
            ),
        ):
            status, body = self._api_status("PUT", "/v1/network/policy", bad_policy)
            if status != 400:
                raise AssertionError(f"{label} policy returned {status}, expected 400: {body}")
            error = body.get("error", {})
            message = error.get("message", "") if isinstance(error, dict) else str(error)
            if expected_error not in message:
                raise AssertionError(f"{label} error should mention {expected_error}, got: {body}")

        # Concurrent replaces must serialize: each one either succeeds or is
        # turned away with 409 (the lock wait is bounded), the final policy is
        # exactly one of the successful requests, and enforcement ends active.
        # A torn or interleaved write would leave a policy nobody requested.
        variants = [
            network_policy(SMOKE_MANAGED_PROVIDERS, {f"smoke-{index}.example.com": {"allow_http_methods": ["GET"]}})
            for index in range(4)
        ]
        results = self._parallel(
            len(variants),
            lambda index: self._api_status(
                "PUT", "/v1/network/policy", variants[index]
            ),
        )
        succeeded = []
        for index, (status, body) in enumerate(results):
            if status == 200:
                succeeded.append(variants[index])
            elif status != 409:
                raise AssertionError(f"concurrent policy replace {index} returned {status}: {body}")
        if not succeeded:
            raise AssertionError(f"no concurrent policy replace succeeded: {results}")
        final = self._api("GET", "/v1/network/policy")["network_controls"]
        if final not in succeeded:
            raise AssertionError(f"final policy matches none of the successful replacements: {final}")
        health = self._api("GET", "/v1/health")
        if health["network_controls"]["status"] != "active":
            raise AssertionError(f"network status not active after concurrent replaces: {health}")
        # Leave the enforcement policy in place for the checks that follow.
        self._api("PUT", "/v1/network/policy", self.enforcement_policy())
        self._ok(
            f"ssh pin, provider schema, and validation enforced; "
            f"asymmetric provider activation checked; {len(succeeded)}/4 concurrent replaces applied, rest 409, status active"
        )

    def check_task_lifecycle(self) -> None:
        self._step("task fail-fast lifecycle and 4xx contract")
        task = self._api("POST", "/v1/tasks", self.task_body("lifecycle check (smoke)", "smoke-lifecycle"))
        task_id = task["task_id"]
        if task["status"] != "queued":
            raise AssertionError(f"new task is {task['status']}, expected queued")
        # Runtime status is not a claim condition: pre-login, a worker claims
        # the queued task immediately and fails it with the runtime status as
        # the error, so queued work never parks behind a missing login.
        failed = self._wait_for_task_status(task_id, "failed", timeout=60)
        if failed["status"] != "failed":
            raise AssertionError(f"pre-login task ended {failed['status']}, expected fail-fast failure: {failed}")
        if "tasks run only while it is active" not in failed.get("error_message", ""):
            raise AssertionError(f"fail-fast error should name the runtime status: {failed}")

        status, _ = self._api_status("GET", "/v1/tasks/task_999999")
        if status != 404:
            raise AssertionError(f"unknown task returned {status}, expected 404")

        # Message validation runs before the status gate, so the 400 contract
        # holds even on a terminal task.
        status, _ = self._api_status("PUT", f"/v1/tasks/{task_id}", {"input_message": ""})
        if status != 400:
            raise AssertionError(f"empty input_message returned {status}, expected 400")
        status, _ = self._api_status(
            "PUT", f"/v1/tasks/{task_id}", {"input_message": "x" * (MESSAGE_LIMIT + 1)}
        )
        if status != 400:
            raise AssertionError(f"oversized input_message returned {status}, expected 400")
        # Every transition off a terminal task is a clean 409.
        status, _ = self._api_status(
            "PUT", f"/v1/tasks/{task_id}", {"input_message": "lifecycle check, updated (smoke)"}
        )
        if status != 409:
            raise AssertionError(f"updating a failed task returned {status}, expected 409")
        status, _ = self._api_status(
            "POST", f"/v1/tasks/{task_id}/steer", {"steer_message": "steer (smoke)"}
        )
        if status != 409:
            raise AssertionError(f"steering a failed task returned {status}, expected 409")
        status, _ = self._api_status("POST", f"/v1/tasks/{task_id}/kill")
        if status != 409:
            raise AssertionError(f"killing a failed task returned {status}, expected 409")
        status, _ = self._api_status("POST", f"/v1/tasks/{task_id}/cancel")
        if status != 409:
            raise AssertionError(f"cancelling a failed task returned {status}, expected 409")
        status, _ = self._api_status("POST", "/v1/tasks/task_999999/kill")
        if status != 404:
            raise AssertionError(f"killing an unknown task returned {status}, expected 404")
        for label, bad_body in (
            ("no-runtime", {"input_message": "missing runtime (smoke)", "thread_id": "smoke-bad-runtime"}),
            ("bad-runtime", {"input_message": "bad runtime (smoke)", "thread_id": "smoke-bad-runtime", "agent_runtime": "bad"}),
            ("no-thread", {"input_message": "missing thread (smoke)", "agent_runtime": self.agent_runtime}),
            ("bad-thread", {"input_message": "bad thread (smoke)", "thread_id": "not valid!", "agent_runtime": self.agent_runtime}),
        ):
            status, _ = self._api_status("POST", "/v1/tasks", bad_body)
            if status != 400:
                raise AssertionError(f"create with {label} returned {status}, expected 400")

        events = self._api("GET", f"/v1/tasks/{task_id}/events?since=0")["events"]
        if [event["event_type"] for event in events] != ["task.started", "task.message", "task.failed"]:
            raise AssertionError(f"fail-fast should emit started/message/failed exactly, got: {events}")
        if any(event["task_id"] != task_id for event in events):
            raise AssertionError("per-task events leaked another task's events")

        thread = self._api("GET", "/v1/threads")["threads"]
        matching = [item for item in thread if item["thread_id"] == "smoke-lifecycle"]
        if len(matching) != 1:
            raise AssertionError(f"lifecycle thread missing from /v1/threads: {thread}")
        if matching[0]["agent_runtime"] != self.agent_runtime:
            raise AssertionError(f"lifecycle thread runtime mismatch: {matching[0]}")
        if matching[0].get("task_count") != 1:
            raise AssertionError(f"lifecycle thread task_count mismatch: {matching[0]}")
        if "retained_task_count" in matching[0] or "continuable" in matching[0]:
            raise AssertionError(f"thread response contains removed fields: {matching[0]}")

        thread_tasks = self._api("GET", "/v1/threads/smoke-lifecycle/tasks")["tasks"]
        if [item["task_id"] for item in thread_tasks] != [task_id]:
            raise AssertionError(f"thread task list did not return the lifecycle task only: {thread_tasks}")
        if thread_tasks[0]["status"] != "failed":
            raise AssertionError(f"thread task list did not retain failed task status: {thread_tasks}")

        # An existing thread derives its session config; supplying agent_runtime
        # (or model/effort) is rejected outright, so the wrong-runtime case never
        # reaches a runtime-mismatch check. task_body deliberately carries those
        # fields here to prove they are refused.
        status, _ = self._api_status(
            "POST",
            "/v1/tasks",
            self.task_body("session fields on existing thread (smoke)", "smoke-lifecycle", runtime="claude_code"),
        )
        if status != 400:
            raise AssertionError(
                f"supplying session fields to an existing thread returned {status}, expected 400"
            )
        status, _ = self._api_status("GET", "/v1/tasks/finished")
        if status != 404:
            raise AssertionError(f"removed finished-task endpoint returned {status}, expected 404")
        self._ok(
            "pre-login task failed fast with the runtime status; validation 400s and terminal 409s honored; "
            "events scoped; thread list/task history covered"
        )

    def check_task_pagination(self) -> None:
        """The active-task list holds queued and running work only: pre-login,
        every created task drains via fail-fast, thread histories retain the
        failed tasks, and cursor paging over whatever is in flight stays
        bounded and duplicate-free."""
        self._step("task list drain and cursor paging (7 fail-fast tasks)")
        created = [
            self._api(
                "POST",
                "/v1/tasks",
                self.task_body(f"pagination filler {index} (smoke)", f"smoke-page-{index}"),
            )["task_id"]
            for index in range(7)
        ]
        seen: list[str] = []
        cursor = None
        while True:
            path = "/v1/tasks" if cursor is None else f"/v1/tasks?last_seen_task_id={cursor}"
            page = self._api("GET", path)["tasks"]
            if not page:
                break
            if len(page) > 5:
                raise AssertionError(f"task page holds {len(page)} tasks, expected at most 5")
            seen.extend(task["task_id"] for task in page)
            if len(seen) > 100:
                raise AssertionError(f"cursor paging did not terminate: {seen}")
            cursor = page[-1]["task_id"]
        if len(seen) != len(set(seen)):
            raise AssertionError(f"pagination returned duplicates: {seen}")
        for task_id in created:
            final = self._wait_for_task(task_id, timeout=60)
            if final["status"] != "failed":
                raise AssertionError(f"pre-login filler {task_id} ended {final['status']}, expected failed")
        remaining = {item["task_id"] for item in self._active_tasks()} & set(created)
        if remaining:
            raise AssertionError(f"failed tasks still listed as active: {remaining}")
        for index, task_id in enumerate(created):
            history = self._api("GET", f"/v1/threads/smoke-page-{index}/tasks")["tasks"]
            if [item["task_id"] for item in history] != [task_id]:
                raise AssertionError(f"thread smoke-page-{index} history mismatch: {history}")
        self._ok("7 tasks drained via fail-fast; paging duplicate-free; thread histories retained")

    def check_admin_concurrency(self) -> None:
        self._step("concurrent task creation with interleaved health reads")
        # Every create must land exactly once with a unique id, interleaved
        # with health reads that must never block or fail. The tasks fail
        # fast pre-login, so consistency is checked per task and against the
        # thread histories rather than the (draining) active list.
        creates = 8

        def create_or_health(index: int) -> tuple[int, dict]:
            if index >= creates:
                return self._api_status("GET", "/v1/health")
            return self._api_status(
                "POST", "/v1/tasks",
                self.task_body(f"concurrent create {index} (smoke)", f"smoke-cc-{index}"),
            )

        results = self._parallel(creates + 3, create_or_health)
        created_ids = []
        for index, (status, body) in enumerate(results):
            if status != 200:
                raise AssertionError(f"concurrent request {index} returned {status}: {body}")
            if index < creates:
                created_ids.append(body["task_id"])
        if len(set(created_ids)) != creates:
            raise AssertionError(f"concurrent creates produced duplicate task ids: {created_ids}")

        for index, task_id in enumerate(sorted(created_ids)):
            if self._wait_for_task(task_id, timeout=60)["status"] != "failed":
                raise AssertionError(f"concurrent create {task_id} did not drain via fail-fast")
        for index in range(creates):
            history = self._api("GET", f"/v1/threads/smoke-cc-{index}/tasks")["tasks"]
            if len(history) != 1 or history[0]["status"] != "failed":
                raise AssertionError(f"thread smoke-cc-{index} history mismatch: {history}")
        self._ok(f"{creates} parallel creates unique, every task landed exactly once")

    def check_state_transactions(self) -> None:
        """Edge cases of the admin-state read-modify-write transaction under
        real concurrency: check-then-act atomicity for terminal transitions
        (exactly one racing cancel wins), racing field updates that must be
        last-writer-wins (never merged or torn), reads that stay fast and
        consistent mid-storm, and event seqs that stay unique across parallel
        writers."""
        self._step("state transaction edge cases (atomic terminal transitions, racing writes, seq uniqueness)")

        # 1. Concurrent cancels racing the fail-fast worker over one task. The
        # status check and the terminal write share one transaction, so AT
        # MOST one cancel can win (the worker may fail the task first); a
        # lost-update regression would let several racers see QUEUED and all
        # "win". The task must end terminal either way.
        _, target = self._api_status(
            "POST", "/v1/tasks",
            self.task_body("cancel race target (smoke)", "smoke-tx-cancel"),
        )
        cancel_id = target["task_id"]
        cancels = self._parallel(
            6, lambda i: self._api_status("POST", f"/v1/tasks/{cancel_id}/cancel")
        )
        statuses = sorted(status for status, _ in cancels)
        if statuses.count(200) > 1 or any(status not in (200, 409) for status in statuses):
            raise AssertionError(f"concurrent cancels must yield at most one 200 and the rest 409, got {statuses}")
        final_status = self._wait_for_task(cancel_id, timeout=60)["status"]
        if final_status not in {"cancelled", "failed"}:
            raise AssertionError(f"cancel race target ended {final_status}, expected a terminal status")

        # 2. Racing updates to one task, interleaved with reads, while the
        # fail-fast worker races them all. Every write must apply atomically:
        # each racer sees 200 or a clean 409 (never a 5xx), the reads never
        # block or fail, and the final message is exactly one sent value or
        # the original — never a torn or merged value.
        _, target = self._api_status(
            "POST", "/v1/tasks",
            self.task_body("update race target (smoke)", "smoke-tx-update"),
        )
        update_id = target["task_id"]
        updaters = 6
        candidates = {f"update racer {index} (smoke)" for index in range(updaters)}
        candidates.add("update race target (smoke)")

        def update_or_read(index: int) -> tuple[int, dict]:
            if index >= updaters:
                return self._api_status("GET", f"/v1/tasks/{update_id}" if index % 2 else "/v1/health")
            return self._api_status(
                "PUT", f"/v1/tasks/{update_id}",
                {"input_message": f"update racer {index} (smoke)"},
            )

        results = self._parallel(updaters + 4, update_or_read)
        for index, (status, body) in enumerate(results):
            expected = (200, 409) if index < updaters else (200,)
            if status not in expected:
                raise AssertionError(f"update/read storm request {index} returned {status}: {body}")
        final = self._api("GET", f"/v1/tasks/{update_id}")
        if final["input_message"] not in candidates:
            raise AssertionError(f"racing updates produced a value no racer sent: {final['input_message']!r}")

        # 3. Terminal statuses are sticky: once the task is terminal, further
        # cancels and updates are clean 409s and the status never changes.
        first_terminal = self._wait_for_task(update_id, timeout=60)["status"]
        if first_terminal not in {"cancelled", "failed"}:
            raise AssertionError(f"update race target ended {first_terminal}, expected a terminal status")

        def update_or_cancel(index: int) -> tuple[int, dict]:
            if index == 0:
                return self._api_status("POST", f"/v1/tasks/{update_id}/cancel")
            return self._api_status(
                "PUT", f"/v1/tasks/{update_id}",
                {"input_message": f"post-terminal racer {index} (smoke)"},
            )

        mixed = self._parallel(5, update_or_cancel)
        bad = [status for status, _ in mixed if status != 409]
        if bad:
            raise AssertionError(f"post-terminal transitions returned non-409 statuses: {bad}")
        if self._api("GET", f"/v1/tasks/{update_id}")["status"] != first_terminal:
            raise AssertionError("terminal status did not stick under racing writes")

        # 4. Parallel writers allocated event seqs through the transaction, so
        # the agent event log must hold no duplicate seq anywhere.
        seqs = [int(event["seq"]) for event in self._agent_events()]
        if len(seqs) != len(set(seqs)):
            duplicates = sorted({seq for seq in seqs if seqs.count(seq) > 1})
            raise AssertionError(f"agent event log has duplicate seqs after the storms: {duplicates}")

        self._ok("terminal transition won at most once, writes atomic, terminal sticky, event seqs unique")

    def check_event_pagination(self) -> None:
        self._step("agent event pagination (newest-first cursor pages, strict seq ordering)")
        events = self._agent_events()
        if len(events) < 6:
            raise AssertionError(f"expected the earlier checks to leave >5 events, found {len(events)}")
        seqs = [int(event["seq"]) for event in events]
        if sorted(seqs) != seqs or len(set(seqs)) != len(seqs):
            raise AssertionError(f"event seqs are not strictly increasing/unique: {seqs}")
        limited = self._api("GET", "/v1/events?limit=3")["events"]
        if [int(event["seq"]) for event in limited] != seqs[-1:-4:-1]:
            raise AssertionError(f"limit=3 did not return the newest three events: {limited}")
        self._ok(f"{len(seqs)} events drained through the cursor with unique seqs")

    def check_enforcement(self) -> None:
        self._step("network enforcement (proxy + nftables, as the agent user)")
        proxy = f"http://127.0.0.1:{PROXY_PORT}"
        agent = "sudo -u trustyclaw-agent env"
        allowed = self._ssh_code(f"{agent} HTTPS_PROXY={proxy} curl -s -o /dev/null -w '%{{http_code}}' --max-time 20 https://example.com/")
        denied = self._ssh_code(f"{agent} HTTPS_PROXY={proxy} curl -s -o /dev/null -w '%{{http_code}}' --max-time 20 https://example.com/denied")
        direct = self._ssh_code(f"{agent} curl -s -o /dev/null -w '%{{http_code}}' --max-time 12 https://example.com/ || true")
        loopback_admin = self._ssh_code(
            f"{agent} curl -s -o /dev/null -w '%{{http_code}}' --connect-timeout 2 --max-time 5 "
            f"http://127.0.0.1:{ADMIN_PORT}/v1/health || true"
        )
        if allowed != "200":
            raise AssertionError(f"allowed request through proxy returned {allowed!r}, expected 200")
        if denied != "403":
            raise AssertionError(f"denied path through proxy returned {denied!r}, expected 403")
        if direct == "200":
            raise AssertionError("direct (un-proxied) request succeeded; nftables is not blocking the agent")
        if loopback_admin not in ("", "000"):
            raise AssertionError(
                f"agent reached loopback admin API directly ({loopback_admin}); nftables should allow only the proxy port"
            )
        # GitHub reads are universal: both the configured repository and a
        # foreign public repository are forwarded and served, while GraphQL
        # (which can mutate and cannot be parsed) fails closed at the proxy.
        gh_allowed = self._ssh_code(
            f"{agent} HTTPS_PROXY={proxy} curl -s -o /dev/null -w '%{{http_code}}' --max-time 20 "
            f"https://github.com/infiloop2/trustyclaw"
        )
        gh_foreign = self._ssh_code(
            f"{agent} HTTPS_PROXY={proxy} curl -s -o /dev/null -w '%{{http_code}}' --max-time 20 "
            f"https://github.com/torvalds/linux"
        )
        gh_graphql = self._ssh_code(
            f"{agent} HTTPS_PROXY={proxy} curl -s -o /dev/null -w '%{{http_code}}' --max-time 20 "
            "-X POST -d '{\"query\":\"{viewer{login}}\"}' https://api.github.com/graphql || true"
        )
        if gh_allowed != "200":
            raise AssertionError(f"configured GitHub repo through proxy returned {gh_allowed!r}, expected 200")
        if gh_foreign != "200":
            raise AssertionError(f"foreign GitHub repo through proxy returned {gh_foreign!r}, expected 200 (reads are universal)")
        if gh_graphql != "403":
            raise AssertionError(f"GitHub GraphQL through proxy returned {gh_graphql!r}, expected proxy 403")
        # End-to-end: a real git clone of the configured repository rides
        # smart-HTTP through the proxy (git and gh are installed by bootstrap;
        # git trusts the proxy CA via the system store).
        tool_versions = self._ssh_code("git --version && gh --version | head -1")
        if "git version" not in tool_versions or "gh version" not in tool_versions:
            raise AssertionError(f"git/gh missing on the host: {tool_versions!r}")
        self._ssh_code("sudo rm -rf /tmp/trustyclaw-smoke-clone /tmp/trustyclaw-smoke-foreign")
        clone_ok = self._ssh_code(
            f"{agent} HTTPS_PROXY={proxy} https_proxy={proxy} "
            "git clone --depth 1 https://github.com/infiloop2/trustyclaw /tmp/trustyclaw-smoke-clone "
            ">/dev/null 2>&1 && echo cloned; sudo rm -rf /tmp/trustyclaw-smoke-clone"
        ).strip()
        if clone_ok != "cloned":
            raise AssertionError("git clone of the configured repository through the proxy failed")
        # Reads are universal, so a foreign public repo clones too (a small one
        # keeps the smoke fast); the denied network events come from the
        # GraphQL and write denials above.
        foreign_clone = self._ssh_code(
            f"{agent} HTTPS_PROXY={proxy} https_proxy={proxy} "
            "git clone --depth 1 https://github.com/octocat/Hello-World /tmp/trustyclaw-smoke-foreign "
            ">/dev/null 2>&1 && echo cloned || echo denied; sudo rm -rf /tmp/trustyclaw-smoke-foreign"
        ).strip()
        if foreign_clone != "cloned":
            raise AssertionError("git clone of a foreign public repository should be allowed (reads are universal)")
        events = self._network_events()
        decisions = {event["decision"] for event in events}
        if not {"allowed", "denied"} <= decisions:
            raise AssertionError(f"expected both allowed and denied network events, saw {decisions}")
        self._ok(
            f"proxy allowed=200 denied=403, direct blocked ({direct or 'no connection'}), "
            f"admin loopback blocked ({loopback_admin or 'no connection'}), "
            f"github reads listed={gh_allowed} foreign={gh_foreign} graphql-denied={gh_graphql}, events logged"
        )

    def check_github_read_paths(self) -> None:
        """Every GitHub guard branch, unauthenticated — the guard decides
        without a credential, so this covers the whole surface without one. When
        GitHub is enabled reads are universal, so foreign repos and non-repo
        paths (search) are forwarded and answer with GitHub's own status; writes
        are gated on the write repo (infiloop2/trustyclaw), so a write to an
        unlisted repo is denied by the proxy while a write to the listed repo
        reaches upstream and returns GitHub's 401 (no credential installed).
        Repository administration is denied even on the write repo, at the guard
        before any credential, so the full admin-write denylist (forks, hooks,
        keys, pages, actions secrets/permissions/oidc, protections, statuses,
        run approval/deletion, ...) is exercised here as proxy 403s. GraphQL and
        non-repo writes (create repo/gist) are denied too. HEAD refs keep the
        checks default-branch-agnostic."""
        self._step("github guard matrix (universal reads, scoped writes)")
        proxy = f"http://127.0.0.1:{PROXY_PORT}"
        curl = (
            f"sudo -u trustyclaw-agent env HTTPS_PROXY={proxy} "
            "curl -s -o /dev/null -w '%{http_code}' --max-time 20"
        )
        # These unauthenticated API reads can be forwarded correctly while
        # GitHub rate-limits the shared egress IP. Keep proxy-denial checks
        # below exact; only upstream read responses get this tolerance.
        read_ok = {"200", "403", "429"}
        unauthenticated_read_ok = {"401", "403", "429"}
        checks = [
            # Reads are universal: the listed repo, a foreign public repo, and
            # non-repo paths (search) are all forwarded to GitHub.
            ("api listed repo read", f"{curl} https://api.github.com/repos/infiloop2/trustyclaw", read_ok),
            ("api foreign repo read", f"{curl} https://api.github.com/repos/torvalds/linux", read_ok),
            ("api search read", f"{curl} 'https://api.github.com/search/repositories?q=trustyclaw'", read_ok),
            ("api rate_limit read", f"{curl} https://api.github.com/rate_limit", read_ok),
            # /user needs auth; the proxy forwards it and GitHub's own 401 (no
            # credential installed) comes back — a proxy denial would be 403.
            ("api /user reaches upstream", f"{curl} https://api.github.com/user || true", unauthenticated_read_ok),
            ("raw listed file", f"{curl} https://raw.githubusercontent.com/infiloop2/trustyclaw/HEAD/README.md", read_ok),
            ("raw foreign file", f"{curl} https://raw.githubusercontent.com/torvalds/linux/HEAD/README", read_ok),
            ("codeload listed tarball", f"{curl} https://codeload.github.com/infiloop2/trustyclaw/tar.gz/HEAD", read_ok),
            ("codeload foreign tarball", f"{curl} https://codeload.github.com/torvalds/linux/tar.gz/HEAD", read_ok),
            ("github.com web read", f"{curl} https://github.com/torvalds/linux", read_ok),
            # The API tarball endpoint 302s to codeload; following the
            # redirect exercises both domains in one read chain.
            (
                "api tarball redirect to codeload",
                f"{curl} -L https://api.github.com/repos/infiloop2/trustyclaw/tarball/HEAD",
                read_ok,
            ),
            # Writes to an unlisted repo are denied by the proxy before any
            # credential question arises.
            (
                "receive-pack discovery on unlisted denied",
                f"{curl} 'https://github.com/torvalds/linux/info/refs?service=git-receive-pack' || true",
                "403",
            ),
            (
                "api write to unlisted denied",
                f"{curl} -X POST -d '{{}}' https://api.github.com/repos/torvalds/linux/issues || true",
                "403",
            ),
            # A write to the listed repo passes the proxy and reaches upstream,
            # which answers 401 without a credential (a proxy denial is 403).
            (
                "api write to listed reaches upstream",
                f"{curl} -X POST -d '{{}}' https://api.github.com/repos/infiloop2/trustyclaw/issues || true",
                "401",
            ),
            # GraphQL is denied outright (can mutate, cannot be parsed).
            (
                "api graphql denied",
                f"{curl} -X POST -d '{{\"query\":\"{{viewer{{login}}}}\"}}' https://api.github.com/graphql || true",
                "403",
            ),
            # Writes that target no repository at all (create a repo, create a
            # gist) are never a configured write repo.
            (
                "api create-repo denied",
                f"{curl} -X POST -d '{{\"name\":\"x\"}}' https://api.github.com/user/repos || true",
                "403",
            ),
            (
                "api create-gist denied",
                f"{curl} -X POST -d '{{\"files\":{{}}}}' https://api.github.com/gists || true",
                "403",
            ),
            # Encoded traversal: %2e%2e decodes to .. and collapses a write path
            # onto an unlisted repo, which the canonicalizing guard must deny.
            (
                "encoded traversal write denied",
                f"{curl} -X POST -d '{{}}' 'https://api.github.com/repos/infiloop2/trustyclaw/%2e%2e/%2e%2e/%2e%2e/repos/torvalds/linux/issues' || true",
                "403",
            ),
            # github.com web mutations are denied everywhere: the API is the
            # only mutation surface.
            (
                "github.com web mutation denied",
                f"{curl} -X POST https://github.com/infiloop2/trustyclaw/issues || true",
                "403",
            ),
            (
                "uploads to unlisted denied",
                f"{curl} -X POST https://uploads.github.com/repos/torvalds/linux/releases/1/assets || true",
                "403",
            ),
            # LFS batch: upload is denied on any repo; an unparseable body
            # fails closed.
            (
                "lfs upload denied",
                f"{curl} -X POST -H 'Content-Type: application/json' "
                f"-d '{{\"operation\":\"upload\",\"objects\":[]}}' "
                f"https://github.com/infiloop2/trustyclaw.git/info/lfs/objects/batch || true",
                "403",
            ),
            (
                "lfs garbage body fails closed",
                f"{curl} -X POST -d 'not-json' https://github.com/infiloop2/trustyclaw.git/info/lfs/objects/batch || true",
                "403",
            ),
        ]
        for name, command, expected in checks:
            got = self._ssh_code(command).strip()
            if isinstance(expected, set):
                if got not in expected:
                    raise AssertionError(f"{name}: expected one of {sorted(expected)}, got {got!r}")
                continue
            if got != expected:
                raise AssertionError(f"{name}: expected {expected}, got {got!r}")
        # Repository administration is denied even on the listed write repo, and
        # the proxy denies it at the guard before any credential — so the full
        # denylist is exercised here without a token (a proxy 403, not GitHub's
        # 401/404). One representative method per sub-resource; the unit tests
        # cover the exhaustive matrix.
        admin_writes = [
            ("PUT", ""),                                   # repo resource (settings/visibility)
            ("POST", "forks"),
            ("POST", "generate"),
            ("POST", "transfer"),
            ("PUT", "collaborators/attacker"),
            ("POST", "keys"),
            ("POST", "hooks"),
            ("POST", "pages"),
            ("POST", "releases"),
            ("PUT", "environments/prod"),
            ("PUT", "actions/secrets/TOKEN"),
            ("PUT", "actions/variables/NAME"),
            ("PUT", "actions/permissions"),
            ("PUT", "actions/oidc/customization/sub"),
            ("POST", "actions/runners/registration-token"),
            ("PUT", "actions/workflows/ci.yml/disable"),
            ("POST", "dispatches"),
            ("PUT", "branches/main/protection"),
            ("POST", "statuses/abc123"),
            ("POST", "check-runs"),
            ("POST", "deployments"),
            ("POST", "actions/runs/1/approve"),
            ("DELETE", "actions/runs/1"),
            ("PATCH", "code-scanning/alerts/1"),
            ("PUT", "vulnerability-alerts"),
        ]
        for method, sub in admin_writes:
            suffix = f"/{sub}" if sub else ""
            code = self._ssh_code(
                f"{curl} -X {method} -d '{{}}' https://api.github.com/repos/infiloop2/trustyclaw{suffix} || true"
            ).strip()
            if code != "403":
                raise AssertionError(f"admin write {method} {sub or '<repo>'}: expected proxy 403, got {code!r}")
        # Real git: ls-remote of any repo rides the (now universal) read leg,
        # while a push to an unlisted repo must be denied by the proxy at the
        # receive-pack discovery leg.
        agentenv = f"sudo -u trustyclaw-agent env HTTPS_PROXY={proxy} https_proxy={proxy}"
        ls_remote = self._ssh_code(
            f"{agentenv} git ls-remote https://github.com/torvalds/linux HEAD "
            ">/dev/null 2>&1 && echo ok || echo failed"
        ).strip()
        if ls_remote != "ok":
            raise AssertionError("git ls-remote of a public repo failed through the proxy")
        workdir = "/tmp/trustyclaw-smoke-push-denial"
        self._ssh_code(f"sudo rm -rf {workdir}")
        push_denied = self._ssh_code(
            f"{agentenv} git clone --depth 1 https://github.com/infiloop2/trustyclaw {workdir} >/dev/null 2>&1 && "
            f"{agentenv} git -C {workdir} push https://github.com/torvalds/linux HEAD:refs/heads/smoke-denied >/dev/null 2>&1 "
            "&& echo pushed || echo denied"
        ).strip()
        self._ssh_code(f"sudo rm -rf {workdir}")
        if push_denied != "denied":
            raise AssertionError("git push to an unlisted repo should be denied by the proxy")
        self._ok(
            f"{len(checks)} guard-branch checks + {len(admin_writes)} admin-write denials "
            "+ git ls-remote/push-denial across the github domains"
        )

    def check_proxy_edge_cases(self) -> None:
        self._step("proxy protocol edge cases (ports, hosts, encodings, wildcards)")
        baseline = max((event["seq"] for event in self._network_events()), default=0)
        proxy = f"http://127.0.0.1:{PROXY_PORT}"
        agent = "sudo -u trustyclaw-agent env"

        # CONNECT to a non-443 port and to an unlisted host: both denied before
        # any DNS or dial; curl reports a proxy CONNECT failure (not an HTTP code).
        self._ssh_code(f"{agent} HTTPS_PROXY={proxy} curl -s -o /dev/null --max-time 12 https://example.com:444/ || true")
        self._ssh_code(f"{agent} HTTPS_PROXY={proxy} curl -s -o /dev/null --max-time 12 https://iana.org/ || true")
        # Host header that does not match the CONNECT host: denied inside TLS.
        mismatch = self._ssh_code(
            f"{agent} HTTPS_PROXY={proxy} curl -s -o /dev/null -w '%{{http_code}}' --max-time 20 "
            f"-H 'Host: evil.example' https://example.com/"
        )
        # Percent-encoded path: the guard must match the decoded form —
        # /%7A%65%6E decodes to /zen, which ^/zen$ allows, while the raw
        # encoded path matches no guard. So a 403 here means the guard failed
        # to decode. The upstream's own status is not asserted: the proxy
        # forwards the path as sent, and GitHub routes the encoded form to 404.
        encoded = self._ssh_code(
            f"{agent} HTTPS_PROXY={proxy} curl -s -o /dev/null -w '%{{http_code}}' --max-time 20 "
            f"https://example.com/%7A%65%6E || true"
        )
        # Wildcard rule (*.example.com) must admit a subdomain.
        wildcard = self._ssh_code(
            f"{agent} HTTPS_PROXY={proxy} curl -s -o /dev/null -w '%{{http_code}}' --max-time 20 "
            f"https://www.example.com/ || true"
        )
        # Plain HTTP is not supported: even a policy-allowed domain gets a
        # logged 403 (curl only honors the lowercase http_proxy variable for
        # http:// URLs).
        plain = self._ssh_code(
            f"{agent} http_proxy={proxy} curl -s -o /dev/null -w '%{{http_code}}' --max-time 20 "
            f"http://example.com/ || true"
        )

        if mismatch != "403":
            raise AssertionError(f"Host header mismatch returned {mismatch!r}, expected 403")
        if encoded in ("403", "", "000"):
            raise AssertionError(f"percent-encoded path matching a decoded guard returned {encoded!r}, expected the proxy to allow it")
        if wildcard == "403" or wildcard == "":
            raise AssertionError(f"wildcard-allowed host returned {wildcard!r}, expected non-403")
        if plain != "403":
            raise AssertionError(f"plain HTTP through the proxy returned {plain!r}, expected 403")

        events = self._network_events(since=baseline)
        reasons = {event.get("reason") for event in events if event["decision"] == "denied"}
        if "only port 443 is allowed for CONNECT" not in reasons:
            raise AssertionError(f"non-443 CONNECT denial not logged; denied reasons: {reasons}")
        if "host is not in the allowed network policy" not in reasons:
            raise AssertionError(f"unknown-host denial not logged; denied reasons: {reasons}")
        if "plain HTTP is not supported; use HTTPS" not in reasons:
            raise AssertionError(f"plain HTTP denial not logged; denied reasons: {reasons}")
        if not any(event["host"] == "www.example.com" and event["decision"] == "allowed" for event in events):
            raise AssertionError("wildcard-matched request did not produce an allowed event")
        if not any(event["path"] == "/%7A%65%6E" and event["decision"] == "allowed" for event in events):
            raise AssertionError("percent-encoded request was not logged as an allowed decision")
        self._ok("port pin, unknown host, Host mismatch, plain HTTP denied; decoded guard and wildcard allowed")

    def check_proxy_concurrency(self) -> None:
        self._step("parallel proxy traffic with consistent event sequencing")
        self._api(
            "PUT",
            "/v1/network/policy",
            {
                "managed_network_integrations": {},
                "allowed_network_access": {
                    "example.com": {
                        "allow_http_methods": ["GET"],
                        "path_guards": ["^/$"],
                    },
                },
            },
        )
        for runtime in SMOKE_RUNTIMES:
            status = self._wait_for_runtime_status({"deactivated"}, runtime=runtime, timeout=60)
            if status != "deactivated":
                raise AssertionError(f"{runtime} should be deactivated before proxy concurrency check, got {status}")
        baseline = max((event["seq"] for event in self._network_events()), default=0)
        proxy = f"http://127.0.0.1:{PROXY_PORT}"
        curl = "curl -s -o /dev/null -w '%{http_code}\\n' --max-time 25"
        script = " ".join(
            [f"{curl} https://example.com/ &" for _ in range(6)]
            + [f"{curl} https://example.com/denied-{index} &" for index in range(6)]
            + ["wait"]
        )
        codes = self._ssh_code(f'sudo -u trustyclaw-agent env HTTPS_PROXY={proxy} bash -c "{script}"')
        lines = [line.strip() for line in codes.splitlines() if line.strip()]
        if len(lines) != 12:
            raise AssertionError(f"expected 12 parallel responses, got {len(lines)}: {lines}")
        if lines.count("403") != 6:
            raise AssertionError(f"expected exactly 6 denied responses, got {lines}")

        # Every one of the 12 decisions must be logged with a unique seq: lost
        # or duplicated entries under parallel load mean the proxy's event
        # serialization (in-process lock + file lock + derived seq) is broken.
        events = [event for event in self._network_events(since=baseline) if event["host"] == "example.com"]
        seqs = [int(event["seq"]) for event in events]
        if len(events) != 12 or len(set(seqs)) != 12:
            raise AssertionError(f"expected 12 uniquely-sequenced events, got {len(events)} (seqs {sorted(seqs)})")
        decisions = [event["decision"] for event in events]
        if decisions.count("allowed") != 6 or decisions.count("denied") != 6:
            raise AssertionError(f"expected 6 allowed + 6 denied events, got {decisions}")
        self._ok("12 parallel requests all decided and logged with unique, ordered seqs")

    def check_pre_login_provider_guards(self) -> None:
        self._step("managed provider data-plane fails closed before login")
        baseline = max((event["seq"] for event in self._network_events()), default=0)
        self._api(
            "PUT",
            "/v1/network/policy",
            network_policy(SMOKE_MANAGED_PROVIDERS),
        )
        proxy = f"http://127.0.0.1:{PROXY_PORT}"

        openai_url = "https://chatgpt.com/backend-api/codex/responses"
        openai_payload = '{"input":"hello"}'
        print(f"  POST {openai_url} before account id is known", flush=True)
        openai_response = self._ssh_code(
            f"sudo -u trustyclaw-agent env HTTPS_PROXY={proxy} "
            f"curl -s --max-time 20 -X POST -H 'Content-Type: application/json' "
            f"--data {shlex.quote(openai_payload)} {shlex.quote(openai_url)}"
        )
        print(f"  -> {openai_response[:200]!r}", flush=True)
        if "OpenAI account id is not available" not in openai_response:
            raise AssertionError(f"OpenAI data-plane request did not fail closed; proxy returned {openai_response!r}")

        claude_hello = self._ssh_code(
            f"sudo -u trustyclaw-agent env HTTPS_PROXY={proxy} "
            "curl -s -o /dev/null -w '%{http_code}' --max-time 20 "
            "https://api.anthropic.com/api/hello"
        )
        if claude_hello == "403" or claude_hello == "000" or claude_hello == "":
            raise AssertionError(f"Claude unauthenticated readiness path returned {claude_hello!r}, expected proxy allow")

        claude_url = "https://api.anthropic.com/v1/messages"
        claude_payload = '{"model":"claude-sonnet-4-5","max_tokens":8,"messages":[{"role":"user","content":"hello"}]}'
        print(f"  POST {claude_url} before Claude account token hash is known", flush=True)
        claude_response = self._ssh_code(
            f"sudo -u trustyclaw-agent env HTTPS_PROXY={proxy} "
            f"curl -s --max-time 20 -X POST -H 'Content-Type: application/json' "
            f"--data {shlex.quote(claude_payload)} {shlex.quote(claude_url)}"
        )
        print(f"  -> {claude_response[:200]!r}", flush=True)
        if "Claude account token is not available" not in claude_response:
            raise AssertionError(f"Anthropic API request did not fail closed before login; proxy returned {claude_response!r}")

        events = self._network_events(since=baseline)
        if not any(
            event["host"] == "chatgpt.com"
            and event["decision"] == "denied"
            and event.get("reason") == "OpenAI account id is not available"
            for event in events
        ):
            raise AssertionError("no account-id-missing chatgpt.com network denial was logged")
        if not any(
            event["host"] == "api.anthropic.com"
            and event["decision"] == "denied"
            and event.get("reason") == "Claude account token is not available"
            for event in events
        ):
            raise AssertionError("no token-missing api.anthropic.com network denial was logged")
        if not any(
            event["host"] == "api.anthropic.com"
            and event["path"] == "/api/hello"
            and event["decision"] == "allowed"
            for event in events
        ):
            raise AssertionError("Claude unauthenticated readiness request was not logged as allowed")
        self._ok(
            "OpenAI and Claude data-plane requests denied before login while Claude readiness stayed allowed"
        )

    def check_tools_surface(self) -> None:
        """Every bundled action on a fresh host with no tool configuration.

        Credentialed actions and OAuth starts must fail closed; all six public
        Polymarket reads must execute. The same pass covers MCP discovery,
        local image/video upload, exact audit arguments, approvals, peer
        credentials, and the tools-service-only egress boundary.
        """
        self._step("bundled tools: listing, enablement, agent shim, approvals surface")
        listing = self._api("GET", "/v1/tools")
        tool_ids = sorted(entry["tool_id"] for entry in listing["tools"])
        expected_tool_ids = sorted(BUNDLED_TOOLS)
        if tool_ids != expected_tool_ids:
            raise AssertionError(f"unexpected bundled tools: {tool_ids}")
        gmail = next(entry for entry in listing["tools"] if entry["tool_id"] == "gmail")
        if gmail["enabled"] or gmail["connection_status"] != {"connected": False}:
            raise AssertionError(f"gmail must start disabled and disconnected: {gmail}")

        # Enablement is not gated on config: enabling before the key is set succeeds.
        enabled = self._api("POST", "/v1/tools/brave_search/enable", {})
        if enabled != {"tool_id": "brave_search", "enabled": True}:
            raise AssertionError(f"brave_search enable without config should succeed: {enabled}")

        # The agent-facing path end to end: the MCP shim runs as
        # trustyclaw-agent and reaches the tools socket by peer credentials.
        shim_command = (
            "sudo -u trustyclaw-agent env PYTHONPATH=/opt/trustyclaw-host "
            "python3 -m host.runtime.tools_mcp_shim"
        )
        next_request_id = 10

        def shim_tool_call(name: str, arguments: dict) -> tuple[dict, object]:
            nonlocal next_request_id
            request = json.dumps(
                {
                    "jsonrpc": "2.0",
                    "id": next_request_id,
                    "method": "tools/call",
                    "params": {"name": name, "arguments": arguments},
                }
            )
            next_request_id += 1
            response_text = self._ssh_code(
                f"printf '%s\\n' {shlex.quote(request)} | {shim_command}"
            )
            rpc = json.loads(response_text)
            result = rpc.get("result")
            if not isinstance(result, dict):
                raise AssertionError(f"{name} returned an invalid MCP response: {rpc}")
            content = result.get("content")
            text = content[0].get("text", "") if isinstance(content, list) and content and isinstance(content[0], dict) else ""
            parsed: object = None
            if not result.get("isError"):
                try:
                    parsed = json.loads(text)
                except json.JSONDecodeError as exc:
                    raise AssertionError(f"{name} returned non-JSON success text: {text!r}") from exc
            return result, parsed
        list_request = json.dumps({"jsonrpc": "2.0", "id": 1, "method": "tools/list"})
        shim_listing = self._ssh_code(f"printf '%s\\n' {shlex.quote(list_request)} | {shim_command}")
        print(f"  shim tools/list -> {shim_listing[:200]!r}", flush=True)
        if "brave_search_search_web" not in shim_listing or "check_tool_approval" not in shim_listing:
            raise AssertionError(f"MCP shim listing missing expected tools: {shim_listing!r}")
        if "list_bundled_tools" not in shim_listing:
            raise AssertionError(f"MCP shim listing missing list_bundled_tools: {shim_listing!r}")
        if "gmail_search_messages" in shim_listing:
            raise AssertionError("MCP shim listed a disabled tool")

        # The catalog built-in shows disabled tools too, so the agent can ask
        # the operator to enable an existing tool instead of rebuilding it.
        catalog_request = json.dumps(
            {"jsonrpc": "2.0", "id": 2, "method": "tools/call", "params": {"name": "list_bundled_tools", "arguments": {}}}
        )
        catalog_response = self._ssh_code(f"printf '%s\\n' {shlex.quote(catalog_request)} | {shim_command}")
        print(f"  shim list_bundled_tools -> {catalog_response[:200]!r}", flush=True)
        if '"isError": true' in catalog_response:
            raise AssertionError(f"list_bundled_tools failed: {catalog_response!r}")
        # The result text is JSON-escaped inside the MCP content; parse it back
        # out and assert the disabled gmail tool appears with enabled false.
        catalog_text = json.loads(catalog_response)["result"]["content"][0]["text"]
        catalog_tools = {entry["tool_id"]: entry for entry in json.loads(catalog_text)["tools"]}
        if not catalog_tools["brave_search"]["enabled"]:
            raise AssertionError(f"catalog must show brave_search enabled: {catalog_text!r}")
        if catalog_tools["gmail"]["enabled"]:
            raise AssertionError(f"catalog must show gmail disabled: {catalog_text!r}")

        # The egress boundary is split: the tools service holds internet egress,
        # the admin service holds none. nftables drops the admin uid's outbound
        # TCP while the tools uid reaches 443 (to a raw IP, so no DNS is needed).
        admin_egress = self._ssh_code(
            "sudo -u trustyclaw-admin timeout 6 bash -c 'exec 3<>/dev/tcp/1.1.1.1/443' 2>&1 "
            "&& echo TC_OPEN || echo TC_BLOCKED"
        )
        if "TC_BLOCKED" not in admin_egress:
            raise AssertionError(f"admin uid must have no internet egress: {admin_egress!r}")
        tools_egress = self._ssh_code(
            "sudo -u trustyclaw-tools timeout 6 bash -c 'exec 3<>/dev/tcp/1.1.1.1/443' 2>&1 "
            "&& echo TC_OPEN || echo TC_BLOCKED"
        )
        if "TC_OPEN" not in tools_egress:
            raise AssertionError(f"tools uid must reach the internet for tool APIs: {tools_egress!r}")

        # The agent-facing tools socket is owned by the dedicated tools service.
        service_active = self._ssh_code("systemctl is-active trustyclaw-tools.service 2>&1 || true")
        if service_active.strip() != "active":
            raise AssertionError(f"trustyclaw-tools.service must be active: {service_active!r}")
        socket_owner = self._ssh_code("stat -c '%U' /run/trustyclaw-tools/tools.sock 2>&1 || true")
        if "trustyclaw-tools" not in socket_owner:
            raise AssertionError(f"tools socket must be owned by trustyclaw-tools: {socket_owner!r}")

        # Peers are scoped strictly by path: other service users are rejected
        # outright, and even the admin uid is rejected on the agent MCP routes
        # (it holds only the /operator/... delegation routes).
        probe_script = (
            "from host.runtime.tools_mcp_shim import UnixHTTPConnection; "
            "c = UnixHTTPConnection('/run/trustyclaw-tools/tools.sock'); "
            "c.request('GET', '/tools'); print(c.getresponse().status)"
        )
        for probe_user in ("trustyclaw-proxy", "trustyclaw-admin"):
            peer_probe = self._ssh_code(
                f"sudo -u {probe_user} env PYTHONPATH=/opt/trustyclaw-host "
                f"python3 -c {shlex.quote(probe_script)}"
            )
            if peer_probe.strip() != "403":
                raise AssertionError(
                    f"tools socket must reject {probe_user} on agent routes, got {peer_probe!r}"
                )

        # The fresh host starts with no approvals or tool config whatsoever.
        approvals = self._api("GET", "/v1/tools/gmail/approvals")
        if approvals["approvals"]:
            raise AssertionError(f"expected no approvals on a fresh host: {approvals}")
        status, body = self._api_status("POST", "/v1/tools/gmail/approvals/approval_1/approve", {})
        if status != 404:
            raise AssertionError(f"deciding a missing approval must 404, got {status} {body}")

        # Enable every package without setting even a dummy value. OAuth starts
        # must fail locally on the absent client config; they never contact the
        # provider or create a connection.
        for tool_id in BUNDLED_TOOLS:
            self._api("POST", f"/v1/tools/{tool_id}/enable", {})
        empty_config_listing = self._api("GET", "/v1/tools")["tools"]
        for entry in empty_config_listing:
            configured = [item["key"] for item in entry.get("config", []) if item.get("set")]
            if configured:
                raise AssertionError(
                    f"fresh smoke must not configure {entry['tool_id']}, found {configured}"
                )
            if entry.get("connection") == "oauth":
                if (entry.get("connection_status") or {}).get("connected") is True:
                    raise AssertionError(f"fresh smoke unexpectedly connected {entry['tool_id']}: {entry}")
                status, body = self._api_status(
                    "POST",
                    f"/v1/tools/{entry['tool_id']}/oauth_connect/start",
                    {"redirect_uri": f"http://127.0.0.1:{ADMIN_PORT}/oauth/callback"},
                )
                if status != 400 or "not set" not in str(body).lower():
                    raise AssertionError(
                        f"{entry['tool_id']} OAuth start without config must fail locally: {status} {body}"
                    )

        all_listed = self._ssh_code(f"printf '%s\\n' {shlex.quote(list_request)} | {shim_command}")
        all_tool_names = {
            entry["name"] for entry in json.loads(all_listed)["result"]["tools"]
        }
        expected_actions = {
            f"{tool_id}_{action.id}"
            for tool_id, tool in BUNDLED_TOOLS.items()
            for action in tool.manifest.actions
        }
        missing_actions = expected_actions - all_tool_names
        if missing_actions:
            raise AssertionError(f"MCP shim omitted bundled actions: {sorted(missing_actions)}")
        if not {"stage_image", "stage_video"}.issubset(all_tool_names):
            raise AssertionError(f"MCP shim omitted media staging actions: {sorted(all_tool_names)}")

        # Exercise both local media uploads without provider config. The files
        # live in the agent workspace, are opened by the agent-side shim, and
        # are removed immediately after the private tool-scoped copies exist.
        media_root = "/mnt/trustyclaw-agent/agent-home"
        image_path = f"{media_root}/trustyclaw-smoke.png"
        video_path = f"{media_root}/trustyclaw-smoke.mp4"
        create_media = (
            "umask 077; "
            f"dd if=/dev/zero of={shlex.quote(image_path)} bs=512 count=1 status=none; "
            f"dd if=/dev/zero of={shlex.quote(video_path)} bs=512 count=1 status=none"
        )
        self._ssh_code(f"sudo -u trustyclaw-agent sh -c {shlex.quote(create_media)}")
        try:
            _, image_stage = shim_tool_call(
                "stage_image", {"path": image_path, "for_tool": "runway"}
            )
            _, runway_video_stage = shim_tool_call(
                "stage_video", {"path": video_path, "for_tool": "runway"}
            )
            _, instagram_video_stage = shim_tool_call(
                "stage_video", {"path": video_path, "for_tool": "instagram"}
            )
        finally:
            self._ssh_code(
                "sudo -u trustyclaw-agent rm -f "
                f"{shlex.quote(image_path)} {shlex.quote(video_path)}"
            )
        if (
            not isinstance(image_stage, dict)
            or not isinstance(runway_video_stage, dict)
            or not isinstance(instagram_video_stage, dict)
        ):
            raise AssertionError("local media staging returned an invalid result")
        asset_ids = {
            "$RUNWAY_IMAGE": image_stage.get("image_asset_id"),
            "$RUNWAY_VIDEO": runway_video_stage.get("video_asset_id"),
            "$INSTAGRAM_VIDEO": instagram_video_stage.get("video_asset_id"),
        }
        if not all(isinstance(value, str) and value for value in asset_ids.values()):
            raise AssertionError(f"local media staging returned missing asset ids: {asset_ids}")

        spool = "/mnt/trustyclaw-admin/tools-state/assets"
        spool_mode = self._ssh_code(f"sudo stat -c '%U:%G:%a' {spool}")
        if spool_mode.strip() != "trustyclaw-tools:trustyclaw-tools:700":
            raise AssertionError(f"tool asset spool has unsafe ownership or mode: {spool_mode!r}")
        for label, asset_id in asset_ids.items():
            if not isinstance(asset_id, str):
                raise AssertionError(f"local media staging returned invalid {label}: {asset_id!r}")
            asset_stat = self._ssh_code(
                f"sudo stat -c '%U:%G:%a:%s' {shlex.quote(f'{spool}/{asset_id}')}"
            )
            if asset_stat.strip() != "trustyclaw-tools:trustyclaw-tools:600:512":
                raise AssertionError(
                    f"staged media is not private on the admin volume: {asset_stat!r}"
                )

        # Invoke every declared action. Credentialed packages must fail closed
        # on absent config/connection, while the public Polymarket package must
        # execute. Static Polymarket listing supplies ids for its three
        # dependent reads below.
        triggered_actions: set[str] = set()
        public_results: dict[str, dict] = {}
        for tool_id, calls in SMOKE_TOOL_CALLS.items():
            for action_id, arguments_template in calls:
                arguments = {
                    key: asset_ids.get(value, value) if isinstance(value, str) else value
                    for key, value in arguments_template.items()
                }
                name = f"{tool_id}_{action_id}"
                response, parsed = shim_tool_call(name, arguments)
                if tool_id == "polymarket":
                    if response.get("isError") or not isinstance(parsed, dict) or parsed.get("status") != "success_executed":
                        raise AssertionError(f"credential-free {name} failed: {response} {parsed}")
                    public_results[action_id] = parsed
                else:
                    content = response.get("content") or [{}]
                    text = str(content[0].get("text", "")) if isinstance(content[0], dict) else ""
                    if not response.get("isError") or not any(
                        phrase in text.lower() for phrase in ("not set", "not connected", "reconnect")
                    ):
                        raise AssertionError(
                            f"{name} without config/connection did not fail closed: {response}"
                        )
                triggered_actions.add(name)

        markets = public_results.get("list_markets", {}).get("markets")
        market = next(
            (
                item for item in markets if isinstance(item, dict)
                and item.get("id") and item.get("clob_token_ids")
            ),
            None,
        ) if isinstance(markets, list) else None
        if not isinstance(market, dict):
            raise AssertionError(f"Polymarket listing returned no usable active market: {markets}")
        try:
            token_ids = json.loads(str(market["clob_token_ids"]))
        except (json.JSONDecodeError, KeyError) as exc:
            raise AssertionError(f"Polymarket returned invalid token ids: {market}") from exc
        token_id = next(
            (value for value in token_ids if isinstance(value, str) and value.isdigit()),
            None,
        ) if isinstance(token_ids, list) else None
        if token_id is None:
            raise AssertionError(f"Polymarket returned no decimal outcome token id: {market}")
        dependent_public_calls = (
            ("get_market", {"market_id": market["id"]}),
            ("get_order_book", {"token_id": token_id}),
            ("price_history", {"token_id": token_id, "interval": "1d"}),
        )
        for action_id, arguments in dependent_public_calls:
            name = f"polymarket_{action_id}"
            response, parsed = shim_tool_call(name, arguments)
            if response.get("isError") or not isinstance(parsed, dict) or parsed.get("status") != "success_executed":
                raise AssertionError(f"credential-free {name} failed: {response} {parsed}")
            triggered_actions.add(name)

        if triggered_actions != expected_actions:
            raise AssertionError(
                "fresh smoke did not trigger every bundled action: "
                f"missing={sorted(expected_actions - triggered_actions)}, "
                f"extra={sorted(triggered_actions - expected_actions)}"
            )

        # Every action call, including local failures, is recorded with
        # expandable exact arguments in the tool audit log.
        events = self._api("GET", "/v1/tools/events?limit=100")["events"]
        action_events = {
            f"{event['tool_id']}_{event['action_id']}": event
            for event in events
            if f"{event['tool_id']}_{event['action_id']}" in expected_actions
        }
        if set(action_events) != expected_actions:
            raise AssertionError(
                f"tool audit log missed actions: {sorted(expected_actions - set(action_events))}"
            )
        if not all(event.get("has_arguments") is True for event in action_events.values()):
            raise AssertionError("tool audit log did not mark every action's arguments as expandable")
        brave_event = action_events["brave_search_search_web"]
        brave_detail = self._api("GET", f"/v1/tools/events/{brave_event['seq']}")["event"]
        if brave_detail.get("arguments") != {"query": "TrustyClaw"}:
            raise AssertionError(f"tool audit detail lost exact arguments: {brave_detail}")

        for tool_id in BUNDLED_TOOLS:
            pending = self._api("GET", f"/v1/tools/{tool_id}/approvals")["approvals"]
            if pending:
                raise AssertionError(f"{tool_id} queued an approval without credentials: {pending}")

        for tool_id in BUNDLED_TOOLS:
            self._api("POST", f"/v1/tools/{tool_id}/disable", {})
        self._ok(
            "every bundled action listed, triggered, and audited with no tool config; OAuth starts and "
            "credentialed actions failed closed, all public Polymarket reads completed, local image/video "
            "uploads worked, non-agent peers were rejected, and no approval was queued"
        )

    def check_both_runtimes_active(self) -> None:
        self._step("both agent runtimes logged in together")
        statuses = {}
        for runtime in SMOKE_RUNTIMES:
            statuses[runtime] = self._wait_for_runtime_status({"active"}, runtime=runtime, timeout=120)
        if statuses != {"codex": "active", "claude_code": "active"}:
            raise AssertionError(f"both runtimes should be active before mixed tasks: {statuses}")
        accounts = {runtime: self._agent_account(runtime) for runtime in SMOKE_RUNTIMES}
        for runtime, account in accounts.items():
            if account.get("status") != "active" or not account.get("account_id"):
                raise AssertionError(f"{runtime} account should be active before mixed tasks: {account}")
            self._assert_provider_metadata(runtime, account)
        self._assert_provider_account_anchors(live_pins=True)
        self._ok("Codex and Claude Code are active at the same time with account pins available")

    def check_runtime_deactivation_stops_running_tasks(self) -> None:
        self._step("runtime deactivation closes active Codex and Claude tasks")
        specs = [
            ("codex", "smoke-deactivate-codex", "CODEX_SHOULD_NOT_FINISH"),
            ("claude_code", "smoke-deactivate-claude", "CLAUDE_SHOULD_NOT_FINISH"),
        ]
        tasks = {}
        for runtime, thread_id, token in specs:
            task = self._api(
                "POST",
                "/v1/tasks",
                self.task_body(
                    (
                        "Do not finish yet. Wait for a follow-up instruction. "
                        f"When you receive it, reply with exactly the word {token} and nothing else."
                    ),
                    thread_id,
                    runtime=runtime,
                ),
            )
            tasks[task["task_id"]] = runtime
        for task_id, runtime in tasks.items():
            current = self._wait_for_task_status(task_id, "running", timeout=180)
            if current["status"] != "running":
                raise AssertionError(f"{runtime} deactivation target never started: {current}")

        self._api("PUT", "/v1/network/policy", {"allowed_network_access": {}})
        for runtime in SMOKE_RUNTIMES:
            status = self._wait_for_runtime_status({"deactivated"}, runtime=runtime, timeout=90)
            if status != "deactivated":
                raise AssertionError(f"{runtime} did not deactivate after provider disable: {status}")
        self._assert_provider_account_anchors(live_pins=False)
        for task_id, runtime in tasks.items():
            done = self._wait_for_task(task_id, timeout=90)
            if done["status"] != "failed":
                raise AssertionError(f"{runtime} running task was not failed by deactivation: {done}")
            if "deactivated" not in (done.get("error_message") or ""):
                raise AssertionError(f"{runtime} failed with unexpected deactivation reason: {done}")

        self._api("PUT", "/v1/network/policy", self.enforcement_policy())
        for runtime in SMOKE_RUNTIMES:
            status = self._wait_for_runtime_status({"active"}, runtime=runtime, timeout=240)
            if status != "active":
                raise AssertionError(f"{runtime} did not recover to active after provider re-enable: {status}")
        self._assert_provider_account_anchors(live_pins=True)
        self._ok("disabling providers failed running tasks, closed runtimes, and both runtimes recovered after re-enable")

    def check_agent_parallelism(self) -> None:
        """Mixed-runtime parallelism on the live host: three Codex tasks and
        three Claude Code tasks run at the same time through independent
        per-runtime pools, then all are steered to completion."""
        self._step("mixed agent parallelism: 3 Codex + 3 Claude tasks, max concurrency 6")
        specs = [
            ("codex", "smoke-codex-par-a", "CODEX_ALPHA"),
            ("claude_code", "smoke-claude-par-a", "CLAUDE_ALPHA"),
            ("codex", "smoke-codex-par-b", "CODEX_BRAVO"),
            ("claude_code", "smoke-claude-par-b", "CLAUDE_BRAVO"),
            ("codex", "smoke-codex-par-c", "CODEX_CHARLIE"),
            ("claude_code", "smoke-claude-par-c", "CLAUDE_CHARLIE"),
        ]
        created: dict[str, tuple[str, str, str]] = {}
        for runtime, thread_id, token in specs:
            task = self._api(
                "POST",
                "/v1/tasks",
                self.task_body(
                    (
                        "Do not finish yet. Wait for a follow-up instruction. "
                        f"When you receive it, reply with exactly the word {token} and nothing else."
                    ),
                    thread_id,
                    runtime=runtime,
                ),
            )
            created[task["task_id"]] = (runtime, thread_id, token)
        print(f"  created {', '.join(sorted(created))}", flush=True)

        max_running_total = 0
        max_running_by_runtime = {runtime: 0 for runtime in SMOKE_RUNTIMES}
        all_running_seen = False
        deadline = time.time() + 300
        while time.time() < deadline:
            active = {t["task_id"]: t["status"] for t in self._active_tasks() if t["task_id"] in created}
            running = {task_id for task_id, status in active.items() if status == "running"}
            max_running_total = max(max_running_total, len(running))
            runtime_status = self._api("GET", "/v1/agent-runtime/status")
            active_by_runtime = {}
            for runtime in SMOKE_RUNTIMES:
                active_task_ids = [
                    task_id
                    for task_id in self.runtime_status_record(runtime_status, runtime).get("active_task_ids", [])
                    if task_id in created
                ]
                active_by_runtime[runtime] = active_task_ids
                max_running_by_runtime[runtime] = max(max_running_by_runtime[runtime], len(active_task_ids))
                if len(active_task_ids) > 3:
                    raise AssertionError(f"more than 3 {runtime} tasks reported running: {runtime_status}")
            if sum(len(ids) for ids in active_by_runtime.values()) > 6:
                raise AssertionError(f"more than 6 mixed tasks reported running: {runtime_status}")
            if all(len(active_by_runtime[runtime]) == 3 for runtime in SMOKE_RUNTIMES):
                all_running_seen = True
                break
            time.sleep(2)

        if not all_running_seen:
            snapshot = self._api("GET", "/v1/agent-runtime/status")
            raise AssertionError(
                "all six mixed runtime tasks never ran together; "
                f"max total={max_running_total}, max by runtime={max_running_by_runtime}, last={snapshot}"
            )

        for task_id, (_, _, token) in sorted(created.items()):
            self._api(
                "POST",
                f"/v1/tasks/{task_id}/steer",
                {"steer_message": f"Now reply with exactly the word {token} and nothing else."},
            )

        for task_id, (runtime, _, token) in created.items():
            done = self._wait_for_task(task_id, timeout=300)
            if done["status"] != "completed":
                raise AssertionError(f"mixed {runtime} task {task_id} ended {done['status']}: {self._task_failure_detail(task_id)}")
            if token not in (done.get("output_message") or "").upper():
                raise AssertionError(f"mixed {runtime} task {task_id} answered {done.get('output_message')!r}, expected {token}")

        intervals: dict[str, tuple[float, float]] = {}
        by_runtime_intervals: dict[str, list[tuple[float, float]]] = {runtime: [] for runtime in SMOKE_RUNTIMES}
        for task_id, (runtime, _, _) in created.items():
            events = self._task_events(task_id)
            started = next((e["timestamp"] for e in events if e["event_type"] == "task.started"), None)
            completed = next((e["timestamp"] for e in events if e["event_type"] == "task.completed"), None)
            if started is None or completed is None:
                raise AssertionError(f"missing task.started/task.completed events for {task_id}: {events}")
            interval = (self._epoch(started), self._epoch(completed))
            intervals[task_id] = interval
            by_runtime_intervals[runtime].append(interval)
        peak = self._max_concurrency(list(intervals.values()))
        runtime_peaks = {
            runtime: self._max_concurrency(runtime_intervals)
            for runtime, runtime_intervals in by_runtime_intervals.items()
        }
        if peak != 6:
            raise AssertionError(f"mixed task peak concurrency should be 6, got {peak}: {intervals}")
        for runtime, runtime_peak in runtime_peaks.items():
            if runtime_peak != 3:
                raise AssertionError(f"{runtime} peak concurrency should be 3, got {runtime_peak}: {intervals}")
        print(
            "  peak concurrency from event intervals: "
            f"total={peak}, codex={runtime_peaks['codex']}, claude_code={runtime_peaks['claude_code']} "
            f"(live max total seen: {max_running_total})",
            flush=True,
        )
        self.parallel_task_ids = {task_id: token for task_id, (_, _, token) in created.items()}
        self.parallel_threads = {
            runtime: (thread_id, token)
            for runtime, thread_id, token in specs
            if thread_id.endswith("-par-a")
        }

        for runtime, (thread_id, token) in self.parallel_threads.items():
            task = self._api(
                "POST",
                "/v1/tasks",
                self.follow_up_body(
                    (
                        "Earlier in this conversation you replied with a single uppercase token. "
                        "Reply with exactly that token again and nothing else."
                    ),
                    thread_id,
                ),
            )
            done = self._wait_for_task(task["task_id"], timeout=240)
            if done["status"] != "completed":
                raise AssertionError(
                    f"{runtime} follow-up task ended {done['status']}: {self._task_failure_detail(task['task_id'])}"
                )
            if token not in (done.get("output_message") or "").upper():
                raise AssertionError(f"{runtime} thread context lost across tasks: {done.get('output_message')!r}")

        self._ok("6 mixed tasks ran together at total cap 6/per-runtime cap 3; both runtimes kept thread context")

    def check_agent_steering(self) -> None:
        """Mid-turn steering through the admin API: a steer sent while the task
        is running must redirect the turn (the host delivers pending steers
        before reading the next runtime message)."""
        self._step(f"{self.agent_runtime} steering: redirect a running task mid-turn")
        slow = self._api(
            "POST",
            "/v1/tasks",
            self.task_body(
                "Slowly write a 300-word essay about the history of bananas, one sentence at a time.",
                f"smoke-steer-{self.agent_runtime}",
            ),
        )
        task_id = slow["task_id"]
        current = self._wait_for_task_status(task_id, "running", timeout=120)
        if current["status"] != "running":
            raise AssertionError(f"steer target never started (status {current['status']})")
        self._api(
            "POST",
            f"/v1/tasks/{task_id}/steer",
            {"steer_message": "Task update: stop the essay and reply with exactly the word STEERED."},
        )
        done = self._wait_for_task(task_id, timeout=240)
        if done["status"] != "completed":
            raise AssertionError(f"steered task ended {done['status']}: {self._task_failure_detail(task_id)}")
        if "STEERED" not in (done.get("output_message") or "").upper():
            raise AssertionError(f"steer did not take effect, output: {done.get('output_message')!r}")
        self._ok(f"{self.agent_runtime} steer redirected the running turn")

    def check_agent_kill_and_thread_survival(self) -> None:
        """Kill a running task (its runtime process is terminated mid-turn), then
        run another task on the same thread: the kill must not corrupt the
        persisted runtime thread/session."""
        self._step(f"{self.agent_runtime} kill: cancel a running task, then reuse its thread")
        slow = self._api(
            "POST",
            "/v1/tasks",
            self.task_body(
                "Slowly write a 500-word essay about the history of bananas, one sentence at a time.",
                f"smoke-kill-{self.agent_runtime}",
            ),
        )
        slow_id = slow["task_id"]
        current = self._wait_for_task_status(slow_id, "running", timeout=120)
        if current["status"] != "running":
            raise AssertionError(f"slow task never started (status {current['status']}); cannot test kill")
        start = time.time()
        status, body = self._api_status("POST", f"/v1/tasks/{slow_id}/kill")
        if status != 200 or body.get("status") != "accepted":
            raise AssertionError(f"kill returned {status}: {body}")
        killed = self._wait_for_task(slow_id, timeout=60)
        if killed["status"] != "cancelled":
            raise AssertionError(f"killed task ended {killed['status']}, expected cancelled")
        print(f"  kill settled in {time.time() - start:.1f}s", flush=True)

        follow = self._api(
            "POST",
            "/v1/tasks",
            self.follow_up_body(
                "Stop the essay. Reply with exactly the word SURVIVED and nothing else.",
                f"smoke-kill-{self.agent_runtime}",
            ),
        )
        done = self._wait_for_task(follow["task_id"], timeout=240)
        if done["status"] != "completed":
            raise AssertionError(
                f"follow-up on the killed thread ended {done['status']}: {self._task_failure_detail(follow['task_id'])}"
            )
        if "SURVIVED" not in (done.get("output_message") or "").upper():
            raise AssertionError(f"follow-up on killed thread answered {done.get('output_message')!r}")
        self._ok(f"{self.agent_runtime} kill cancelled the running task; a later task resumed the same thread")

    def check_agent_thread_recall(self) -> None:
        """Thread context must survive runtime process recycling. By now the
        earlier parallel threads have cycled through the runtime pools, so these
        recalls exercise persisted thread/session resume."""
        self._step("agent thread recall after process recycling")
        if not self.parallel_threads:
            raise AssertionError("no completed parallel threads recorded; recall check must run after parallelism")
        for runtime, (thread_id, token) in self.parallel_threads.items():
            task = self._api(
                "POST",
                "/v1/tasks",
                self.follow_up_body(
                    (
                        "Earlier in this conversation you replied with a single uppercase word. "
                        "Reply with exactly that word again and nothing else."
                    ),
                    thread_id,
                ),
            )
            done = self._wait_for_task(task["task_id"], timeout=240)
            if done["status"] != "completed":
                raise AssertionError(
                    f"{runtime} recall on {thread_id} ended {done['status']}: {self._task_failure_detail(task['task_id'])}"
                )
            if token not in (done.get("output_message") or "").upper():
                raise AssertionError(f"{runtime} {thread_id} lost its context: {done.get('output_message')!r}")
        self._ok("Codex and Claude threads recalled their context after pool eviction and reuse")

    def check_reboot_recovery(self) -> None:
        """POST /v1/host-runtime/reboot, then prove the host comes back with
        everything intact: the admin API and proxy restart enabled, task
        history and the thread map survive on the EBS volume, provider login
        persists, and a post-reboot task resumes a pre-reboot thread."""
        self._step("host reboot: services, state, login, and threads survive")
        finished_id = next(iter(self.parallel_task_ids), None)
        if finished_id is None:
            raise AssertionError("no completed parallel task recorded; reboot check must run after parallelism")
        status, body = self._api_status("POST", "/v1/host-runtime/reboot")
        if status != 200 or body.get("status") != "accepted":
            raise AssertionError(f"reboot returned {status}: {body}")
        print("  reboot accepted; waiting for the host to go down and come back", flush=True)
        time.sleep(20)  # let the host actually drop before reconnecting

        deadline = time.time() + 420
        health = None
        while time.time() < deadline:
            try:
                self._reopen_tunnel()
                health = self._api("GET", "/v1/health")
                if health["network_controls"]["status"] == "active":
                    break
            except Exception as exc:  # noqa: BLE001 - ssh/api both fail until boot completes
                print(f"  still waiting ({type(exc).__name__})", flush=True)
            time.sleep(10)
        if not health or health["network_controls"]["status"] != "active":
            raise AssertionError(f"host did not come back healthy after reboot (last health: {health})")

        # Pre-reboot history survived on disk.
        survivor = self._api("GET", f"/v1/tasks/{finished_id}")
        if survivor["status"] != "completed":
            raise AssertionError(f"completed task changed across reboot: {survivor}")

        # The provider logins persisted: both runtimes re-derive active without
        # a new login flow.
        for runtime in SMOKE_RUNTIMES:
            status = self._wait_for_runtime_status({"active", "awaiting_login", "error"}, runtime=runtime, timeout=180)
            if status != "active":
                raise AssertionError(f"{runtime} is {status} after reboot; expected the login to survive")

        # And pre-reboot threads resume with their context for both runtimes.
        for runtime, (thread_id, token) in self.parallel_threads.items():
            recall = self._api(
                "POST",
                "/v1/tasks",
                self.follow_up_body(
                    (
                        "Earlier in this conversation you replied with a single uppercase token. "
                        "Reply with exactly that token again and nothing else."
                    ),
                    thread_id,
                ),
            )
            done = self._wait_for_task(recall["task_id"], timeout=300)
            if done["status"] != "completed":
                raise AssertionError(
                    f"{runtime} post-reboot recall ended {done['status']}: {self._task_failure_detail(recall['task_id'])}"
                )
            if token not in (done.get("output_message") or "").upper():
                raise AssertionError(f"{runtime} thread context lost across reboot: {done.get('output_message')!r}")
        self._ok("host rebooted clean; history, both logins, and both runtime thread contexts survived")

    # --- helpers -----------------------------------------------------------

    def check_network_event_prune_race(self) -> None:
        """Storm denied CONNECTs through the proxy while the admin API
        concurrently pages the network event table. Two real processes, two
        database roles: the proxy inserts rows under its narrow grant (its
        amortized prune fires every PRUNE_EVERY-th insert, a no-op below the
        cap), the admin reads them, and paging must stay unique and ordered
        throughout. Also pins the role isolation only a live host can show:
        the proxy role can write exactly the network_events table and nothing
        else."""
        self._step("network event storm under concurrent reads (two database roles)")
        baseline_row = self._ssh_code(
            "sudo -u postgres psql -tA -d trustyclaw_admin -c 'SELECT COALESCE(max(seq), 0) FROM network_events'"
        ).strip()
        baseline = int(baseline_row or 0)
        needed = 2 * PRUNE_EVERY + 50  # cross at least two amortized-prune boundaries
        print(f"  pushing {needed} denied requests through the proxy (seq baseline {baseline})", flush=True)

        reader_failures: list[str] = []
        reader_reads = {"count": 0}
        stop = threading.Event()

        def reader() -> None:
            while not stop.is_set():
                status, body = self._api_status("GET", "/v1/network/events")
                if status != 200:
                    reader_failures.append(f"GET /v1/network/events -> {status}: {body}")
                    return
                reader_reads["count"] += 1
                page_seqs = [int(event["seq"]) for event in body["events"]]
                if page_seqs:
                    if len(set(page_seqs)) != len(page_seqs) or page_seqs != sorted(page_seqs, reverse=True):
                        reader_failures.append(f"inconsistent network event page: {page_seqs}")
                        return
                else:
                    time.sleep(0.2)

        reader_thread = threading.Thread(target=reader, daemon=True)
        reader_thread.start()
        generator = (
            "sudo -u trustyclaw-agent python3 - <<'PY'\n"
            "import socket, threading\n"
            f"count, workers = {needed}, 8\n"
            "def worker(n):\n"
            "    for _ in range(n):\n"
            "        try:\n"
            f"            s = socket.create_connection((\"127.0.0.1\", {PROXY_PORT}), timeout=10)\n"
            "            s.sendall(b\"CONNECT denied.smoke.invalid:443 HTTP/1.1\\r\\n"
            "Host: denied.smoke.invalid:443\\r\\n\\r\\n\")\n"
            "            s.recv(4096)\n"
            "            s.close()\n"
            "        except OSError:\n"
            "            pass\n"
            "threads = [threading.Thread(target=worker, args=(-(-count // workers),)) for _ in range(workers)]\n"
            "[t.start() for t in threads]\n"
            "[t.join() for t in threads]\n"
            "print(\"generated\")\n"
            "PY"
        )
        try:
            output = self._ssh_code(generator)
        finally:
            stop.set()
            reader_thread.join(timeout=30)
        if "generated" not in output:
            raise AssertionError(f"event generator did not finish cleanly: {output!r}")
        if reader_failures:
            raise AssertionError(f"concurrent reader failed during the storm: {reader_failures[0]}")
        if reader_reads["count"] < 10:
            raise AssertionError(f"reader only completed {reader_reads['count']} reads; the storm outpaced it entirely")

        verdict = json.loads(self._ssh_code(
            "sudo -u postgres psql -tA -d trustyclaw_admin -c "
            "\"SELECT json_build_object('rows', count(*), 'max_seq', COALESCE(max(seq), 0),"
            " 'unique', count(*) = count(DISTINCT seq))::text FROM network_events WHERE seq > "
            f"{baseline}\""
        ))
        if verdict["rows"] < needed:
            raise AssertionError(f"storm generated {needed} denials but only {verdict['rows']} events landed")
        if not verdict["unique"]:
            raise AssertionError(f"duplicate event seqs after the storm: {verdict}")
        # Role isolation: the proxy role can touch exactly network_events.
        isolation = self._ssh_code(
            "sudo -u trustyclaw-proxy psql -tA -d trustyclaw_admin -c 'SELECT count(*) >= 0 FROM network_events' && "
            "sudo -u trustyclaw-proxy bash -c '! psql -tA -d trustyclaw_admin -c \"SELECT count(*) FROM tasks\" 2>/dev/null' && "
            "sudo -u trustyclaw-proxy bash -c '! psql -tA -d trustyclaw_admin -c \"SELECT agent_name FROM config\" 2>/dev/null' && "
            "echo ok"
        ).strip().splitlines()
        if isolation != ["t", "ok"]:
            raise AssertionError(f"proxy database role is not confined to network_events: {isolation}")
        final = self._api("GET", "/v1/network/events")
        if not final["events"]:
            raise AssertionError("admin API cannot read the network events after the storm")
        self._ok(
            f"{needed} events stormed; {reader_reads['count']} concurrent reads stayed consistent; "
            "proxy role confined to network_events"
        )

    def _raw_local_http(self, port: int, request: bytes) -> bytes:
        import socket

        with socket.create_connection(("127.0.0.1", port), timeout=30) as sock:
            sock.sendall(request)
            sock.shutdown(socket.SHUT_WR)
            chunks: list[bytes] = []
            while True:
                chunk = sock.recv(65536)
                if not chunk:
                    break
                chunks.append(chunk)
            return b"".join(chunks)

    def _api(self, method: str, path: str, body: dict | None = None) -> dict:
        data = json.dumps(body).encode() if body is not None else None

        def attempt() -> dict:
            request = urllib.request.Request(f"http://127.0.0.1:{ADMIN_PORT}{path}", data=data, method=method)
            request.add_header("Authorization", f"Bearer {self.result['admin_password']}")
            if body is not None:
                request.add_header("Content-Type", "application/json")
            with urllib.request.urlopen(request, timeout=30) as response:
                return json.loads(response.read())

        try:
            return attempt()
        except (urllib.error.URLError, ConnectionError) as exc:
            # The tunnel can drop during a long idle; the failure then hits the
            # connect of the NEXT request, which never reached the server, so
            # one reopen-and-retry is safe. (A response lost mid-flight would
            # retry a mutation that already executed; that is vanishingly rare
            # and fails the run visibly rather than silently.)
            reason = getattr(exc, "reason", exc)
            if isinstance(exc, urllib.error.HTTPError):
                payload = exc.read()
                try:
                    detail = json.loads(payload)
                except json.JSONDecodeError:
                    detail = payload.decode(errors="replace")
                raise AssertionError(f"{method} {path} returned HTTP {exc.code}: {detail}") from exc
            print(f"  (admin API unreachable: {reason}; reopening tunnel and retrying)", flush=True)
            self._reopen_tunnel()
            return attempt()

    def _app_api(self, method: str, path: str, body: dict | None = None) -> dict:
        """Call an app route through the same scoped bridge the UI uses."""
        data = json.dumps(body).encode() if body is not None else None
        request = urllib.request.Request(
            f"http://127.0.0.1:{ADMIN_PORT}{path}", data=data, method=method
        )
        request.add_header("Authorization", f"Bearer {self.result['admin_password']}")
        request.add_header("X-TrustyClaw-App-Bridge", "mission_pursuit")
        if body is not None:
            request.add_header("Content-Type", "application/json")
        with urllib.request.urlopen(request, timeout=30) as response:
            return json.loads(response.read())

    def _api_status(
        self, method: str, path: str, body: dict | None = None, *,
        bearer: str | None = "__default__",
    ) -> tuple[int, dict]:
        """One-shot request returning (status, body) instead of raising on HTTP
        errors, for checks that assert specific 4xx behavior or run from
        threads (no tunnel-reopen side effects). ``bearer=None`` sends no
        Authorization header."""
        if bearer == "__default__":
            bearer = self.result["admin_password"]
        data = json.dumps(body).encode() if body is not None else None
        request = urllib.request.Request(f"http://127.0.0.1:{ADMIN_PORT}{path}", data=data, method=method)
        if bearer is not None:
            request.add_header("Authorization", f"Bearer {bearer}")
        if body is not None:
            request.add_header("Content-Type", "application/json")
        try:
            with urllib.request.urlopen(request, timeout=30) as response:
                return response.status, json.loads(response.read())
        except urllib.error.HTTPError as exc:
            payload = exc.read()
            try:
                return exc.code, json.loads(payload)
            except json.JSONDecodeError:
                return exc.code, {"raw": payload.decode(errors="replace")}

    def _parallel(self, count: int, fn) -> list:
        """Run fn(0..count-1) on real threads and return results in order; a
        worker's exception is re-raised after all workers finish."""
        results: list = [None] * count

        def run(index: int) -> None:
            try:
                results[index] = fn(index)
            except Exception as exc:  # noqa: BLE001 - surfaced after join below
                results[index] = exc

        threads = [threading.Thread(target=run, args=(index,)) for index in range(count)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        for result in results:
            if isinstance(result, Exception):
                raise AssertionError(f"parallel request failed: {result}") from result
        return results

    def _active_tasks(self) -> list[dict]:
        """Drain the paged GET /v1/tasks list (active tasks only)."""
        tasks: list[dict] = []
        last: str | None = None
        while True:
            query = f"?last_seen_task_id={last}" if last else ""
            page = self._api("GET", f"/v1/tasks{query}")["tasks"]
            if not page:
                return tasks
            tasks.extend(page)
            last = page[-1]["task_id"]

    def _network_events(self, since: int = 0) -> list[dict]:
        """Drain `/v1/network/events` cursor pages into events after ``since``."""
        return self._drain_event_pages("/v1/network/events", since)

    def _agent_events(self, since: int = 0) -> list[dict]:
        """Drain `/v1/events` cursor pages into events after ``since``."""
        return self._drain_event_pages("/v1/events", since)

    def _drain_event_pages(self, endpoint: str, since: int) -> list[dict]:
        """Walk an audit log's newest-first cursor pages, asserting the page
        contract, and return events after ``since`` oldest-first."""
        events: list[dict] = []
        before: int | None = None
        while True:
            query = "?limit=100" if before is None else f"?before={before}&limit=100"
            page = self._api("GET", f"{endpoint}{query}")["events"]
            if not page:
                return sorted(events, key=lambda event: int(event["seq"]))
            if len(page) > 100:
                raise AssertionError(f"{endpoint} page holds {len(page)} events, expected at most 100")
            page_seqs = [int(event["seq"]) for event in page]
            if page_seqs != sorted(page_seqs, reverse=True):
                raise AssertionError(f"{endpoint} page is not sorted by descending seq: {page_seqs}")
            if before is not None and any(seq >= before for seq in page_seqs):
                raise AssertionError(f"{endpoint} page contains seq >= before cursor {before}: {page_seqs}")
            if len(set(page_seqs)) != len(page_seqs):
                raise AssertionError(f"{endpoint} page contains duplicate seqs: {page_seqs}")
            events.extend(event for event in page if int(event["seq"]) > since)
            if min(page_seqs) <= since:
                return sorted(events, key=lambda event: int(event["seq"]))
            before = min(page_seqs)

    def _agent_account(self, runtime_type: str) -> dict:
        accounts = self._api("GET", "/v1/agent-runtime/account")["accounts"]
        for account in accounts:
            if account.get("agent_runtime") == runtime_type:
                return account
        raise AssertionError(f"account summary did not include {runtime_type}: {accounts}")

    def _assert_provider_metadata(self, runtime_type: str, account: dict) -> None:
        forbidden_fragments = ("token", "secret", "key", "authorization", "bearer", "sha256")
        allowed_keys = {"agent_runtime", "provider", "status", "account_id", "email", "plan_type"}
        if runtime_type == "codex":
            allowed_keys.add("codex_usage")
        elif runtime_type == "claude_code":
            allowed_keys.add("claude_usage")
        unexpected_keys = sorted(set(account) - allowed_keys)
        if unexpected_keys:
            raise AssertionError(f"{runtime_type} account metadata exposed unexpected key(s) {unexpected_keys}: {account}")

        def check_no_secretish_keys(value: object) -> None:
            if isinstance(value, dict):
                for key, item in value.items():
                    lowered = str(key).lower()
                    if any(fragment in lowered for fragment in forbidden_fragments):
                        raise AssertionError(f"{runtime_type} account metadata leaked secret-like key {key!r}: {account}")
                    check_no_secretish_keys(item)
            elif isinstance(value, list):
                for item in value:
                    check_no_secretish_keys(item)

        check_no_secretish_keys(account)

    def _assert_provider_account_anchors(self, *, live_pins: bool) -> None:
        snapshot = self._provider_account_pin_snapshot()
        openai = snapshot.get("openai")
        claude = snapshot.get("claude")
        if not isinstance(openai, dict) or not openai.get("admin_account_id"):
            raise AssertionError(f"OpenAI admin account anchor is missing: {snapshot}")
        if not isinstance(claude, dict) or not claude.get("admin_account_id") or not claude.get("admin_token_sha256"):
            raise AssertionError(f"Claude admin account anchor is missing: {snapshot}")
        if live_pins:
            if openai.get("pin_account_id") != openai.get("admin_account_id"):
                raise AssertionError(f"OpenAI proxy pin does not match admin account anchor: {snapshot}")
            if claude.get("pin_account_id") != claude.get("admin_account_id"):
                raise AssertionError(f"Claude proxy account pin does not match admin account anchor: {snapshot}")
            if claude.get("pin_token_sha256") != claude.get("admin_token_sha256"):
                raise AssertionError(f"Claude proxy token pin does not match admin account anchor: {snapshot}")
        else:
            if openai.get("pin_account_id") or openai.get("pin_token_sha256"):
                raise AssertionError(f"OpenAI live proxy pin survived provider deactivation: {snapshot}")
            if claude.get("pin_account_id") or claude.get("pin_token_sha256"):
                raise AssertionError(f"Claude live proxy pin survived provider deactivation: {snapshot}")

    def _provider_account_pin_snapshot(self) -> dict:
        query = """
SELECT jsonb_object_agg(
    providers.provider,
    jsonb_build_object(
        'admin_account_id', provider_accounts.account_id,
        'admin_token_sha256', provider_accounts.metadata->>'access_token_sha256',
        'pin_account_id', proxy_provider_pins.account_id,
        'pin_token_sha256', proxy_provider_pins.access_token_sha256
    )
)::text
FROM (VALUES ('openai'), ('claude')) AS providers(provider)
LEFT JOIN provider_accounts USING (provider)
LEFT JOIN proxy_provider_pins USING (provider)
"""
        output = self._ssh_code(
            "sudo -u postgres psql -tA -d trustyclaw_admin -c "
            + shlex.quote(query)
        )
        return json.loads(output) if output else {}

    def print_network_events(self, label: str, *, since: int = 0) -> None:
        try:
            events = self._network_events(since=since)
        except Exception as exc:  # noqa: BLE001 - best-effort debug output
            print(f"  {label}: could not read network events: {type(exc).__name__}: {exc}", flush=True)
            return
        print(f"  {label}: {len(events)} event(s) after seq {since}", flush=True)
        for event in events:
            reason = event.get("reason")
            suffix = f" reason={reason!r}" if reason else ""
            print(
                f"    seq={event.get('seq')} {event.get('decision')} "
                f"{event.get('method')} {event.get('protocol')}://{event.get('host')}{event.get('path')}{suffix}",
                flush=True,
            )

    def _task_failure_detail(self, task_id: str) -> str:
        """Failure context for assertions: the task's error_message plus its
        last few events (which carry agent messages and failure payloads)."""
        task = self._api("GET", f"/v1/tasks/{task_id}")
        tail = "; ".join(
            f"{event['event_type']}: {event['payload'].get('error_message') or event['payload'].get('message', '')}"
            for event in self._task_events(task_id)[-4:]
        )
        return f"error_message={task.get('error_message')!r}; recent events: {tail or '<none>'}"

    @staticmethod
    def _max_concurrency(intervals: list[tuple[float, float]]) -> int:
        """Peak number of simultaneously open [start, end] intervals. At equal
        timestamps ends are processed before starts, so a serialized handoff
        at second granularity does not count as overlap."""
        points: list[tuple[float, int]] = []
        for start, end in intervals:
            points.append((start, 1))
            points.append((end, -1))
        points.sort(key=lambda point: (point[0], point[1]))
        current = peak = 0
        for _, delta in points:
            current += delta
            peak = max(peak, current)
        return peak

    def _task_events(self, task_id: str) -> list[dict]:
        events: list[dict] = []
        cursor = 0
        while True:
            page = self._api("GET", f"/v1/tasks/{task_id}/events?since={cursor}")["events"]
            if not page:
                return events
            events.extend(page)
            cursor = max(int(event["seq"]) for event in page)

    @staticmethod
    def _epoch(timestamp: str) -> float:
        return calendar.timegm(time.strptime(timestamp, "%Y-%m-%dT%H:%M:%SZ"))

    def _wait_for_task_status(self, task_id: str, wanted: str, *, timeout: float) -> dict:
        """Wait until the task reaches ``wanted`` or any terminal status."""
        deadline = time.time() + timeout
        while True:
            task = self._api("GET", f"/v1/tasks/{task_id}")
            if task["status"] == wanted or task["status"] in {"completed", "failed", "cancelled"}:
                return task
            if time.time() >= deadline:
                return task
            time.sleep(2)

    def _wait_for_task(self, task_id: str, *, timeout: float) -> dict:
        deadline = time.time() + timeout
        while True:
            task = self._api("GET", f"/v1/tasks/{task_id}")
            if task["status"] in {"completed", "failed", "cancelled"} or time.time() >= deadline:
                return task
            time.sleep(2)

    def _wait_for_runtime_status(self, wanted: set[str], *, timeout: float, runtime: str | None = None) -> str:
        runtime = runtime or self.agent_runtime
        deadline = time.time() + timeout
        record = self.runtime_status_record(self._api("GET", "/v1/agent-runtime/status"), runtime)
        status = record["status"]
        print(
            self._runtime_status_line(runtime, record, wanted),
            flush=True,
        )
        previous_detail = record.get("error_message")
        while time.time() < deadline and status not in wanted:
            time.sleep(5)
            previous = status
            record = self.runtime_status_record(
                self._api("GET", "/v1/agent-runtime/status"), runtime
            )
            status = record["status"]
            detail = record.get("error_message")
            if status != previous or detail != previous_detail:
                print(self._runtime_status_line(runtime, record), flush=True)
            previous_detail = detail
        return status

    def _runtime_status_line(self, runtime: str, record: dict, wanted: set[str] | None = None) -> str:
        status = record["status"]
        suffix = f" (waiting for {'/'.join(sorted(wanted))})" if wanted else ""
        detail = record.get("error_message")
        if isinstance(detail, str) and detail:
            return f"  {runtime} runtime status: {status}{suffix}; error_message={detail!r}"
        return f"  {runtime} runtime status: {status}{suffix}"

    def _ssh_code(self, remote_command: str) -> str:
        result = subprocess.run(
            [
                "ssh", "-S", str(self.control_socket),
                "-o", f"UserKnownHostsFile={self.workdir / 'known_hosts'}",
                f"trustyclaw-operator@{self.result['public_dns']}", remote_command,
            ],
            capture_output=True, text=True,
        )
        return result.stdout.strip()

    def _aws(self, *args: str) -> dict:
        proc = subprocess.run(
            ["aws", *args, "--region", self.region],
            check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
        )
        return json.loads(proc.stdout) if proc.stdout.strip() else {}

    def _step(self, name: str) -> None:
        self.total += 1
        self._current = name
        print(f"[ .. ] {name}", flush=True)

    def _ok(self, detail: str) -> None:
        self.passed += 1
        print(f"[ OK ] {self._current}: {detail}\n", flush=True)


if __name__ == "__main__":
    raise SystemExit(main())
