#!/usr/bin/env python3
"""The smoke test: deploy a real host, validate it end to end, tear it down.

This is the single manual smoke test for the project. It validates everything
the unit tests cannot — including live agent runtime behavior exercised through
the deployed admin API. It always runs both Codex and Claude Code paths.

  - the deploy path (subnet selection, security group, IMDSv2, SSH provisioning)
  - the bootstrap on real Ubuntu 22.04 (apt, npm, user/permission setup,
    nftables, systemd, the proxy CA)
  - the admin API answering over the SSH tunnel (health, network policy)
  - admin API contract edge cases over the tunnel: auth rejection, the task
    lifecycle and its 4xx responses, idempotency replay/conflict behavior,
    policy validation (including managed OpenAI and Claude provider schema),
    and event pagination
  - concurrency on the real host: parallel task creation, a same-key
    idempotency storm, concurrent policy replaces, and parallel proxy traffic
    with consistent event sequencing
  - state transaction edge cases on the real host: racing cancels of one task
    resolve to exactly one winner, racing updates apply last-writer-wins (and
    never tear), an update racing a cancel cannot resurrect the task, and
    parallel writers never duplicate an event seq
  - network enforcement on the real host: the agent reaches an allowed domain
    only through the proxy, a denied path is blocked, direct external egress is
    dropped by nftables, and non-proxy loopback access to the admin API is
    blocked
  - deploy-time config schema on the real host: agent_runtime and agent_type
    are gone; managed providers omitted at first boot leave both runtimes
    deactivated until the runtime policy enables them
  - proxy protocol edge cases: CONNECT port pinning, unknown hosts, Host
    header mismatch, percent-encoded paths against path guards, wildcard
    domain rules, malformed request lengths, and plain-HTTP proxying
  - OpenAI account pinning: data-plane traffic fails closed before login;
    after login, missing/wrong account headers are denied, live web search is
    denied, and the cached variant is allowed
  - after an interactive Codex login: a real Codex web search
    task (which completes only if the agent's real ChatGPT traffic passes the
    live web search guard), then the live Codex behavior the host depends on,
    with small tasks running alongside Claude Code tasks through independent
    per-runtime 3-worker pools (peak concurrency proven from event timestamps,
    never above 6 total), steering a
    running task mid-turn, killing a running task and resuming its thread
    afterwards, and thread context surviving warm app-server reuse, pool
    eviction (a fresh process resuming a persisted thread), and a full host
    reboot
  - after an interactive Claude Code OAuth login: Anthropic API
    traffic fails closed before the account token hash is known, missing/wrong
    bearer tokens are denied after login, a real Claude task completes through
    the proxy, and the same steering, kill, thread recall, reboot, and prune
    checks run against Claude session resume
  - host reboot recovery: services restart enabled, task history, provider
    login, and the thread map all survive on the EBS volume
  - the cross-process event log locking: the network event log is pushed past
    its prune threshold so the proxy rewrites it by rename mid-storm
    while the admin API concurrently reads it through its narrow proxy-state
    helper — reads stay consistent, seqs stay unique and ordered, and the
    pruned file remains proxy-private

DO NOT run this in CI. It creates real billable AWS resources and needs real
network. CI runs with no network on purpose.

This script tears down what it created (terminates the instance, deletes the
security group, and deletes the data volumes when deploy reached the point of
writing their ids), even on failure. If deploy fails before the result file is
written, the tagged data volumes remain visible to the next deploy run.

The smoke owns its own deploy config: it deploys an agent named
``trustyclaw-smoke`` into ``SMOKE_REGION`` (below), which is pinned to the
region scoped in ``tests/smoke/iam_policy_smoke.json`` so the two cannot drift. It
also generates an ephemeral SSH keypair for operator access and discards it at
teardown. So there is nothing to write by hand — no config file and no SSH key.

Environment assumptions (each is checked, with a clear failure if missing):

  1. The ``aws`` CLI and ``ssh`` are installed and on PATH.
  2. AWS credentials with the permissions in ``tests/smoke/iam_policy_smoke.json``
     are exported as ``AWS_ACCESS_KEY_ID`` / ``AWS_SECRET_ACCESS_KEY``. See
     docs/Development.md for how to create a scoped IAM user.

Cost: one t3.small plus a 16 GiB root gp3 volume and two 8 GiB encrypted data
gp3 volumes for the few minutes the test runs (about one US cent). Teardown
removes the instance root volume and, when their ids are known, both data
volumes.

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
from host.runtime.state import MAX_EVENTS, PRUNE_EVERY

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
SMOKE_MANAGED_DOMAINS = ("api.openai.com", "auth.openai.com", "chatgpt.com", "api.anthropic.com", "platform.claude.com")


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
        smoke.check_network_policy()
        smoke.check_policy_validation_and_concurrency()
        smoke.check_task_lifecycle()
        smoke.check_task_pagination()
        smoke.check_idempotency()
        smoke.check_admin_concurrency()
        smoke.check_state_transactions()
        smoke.check_event_pagination()
        smoke.check_enforcement()
        smoke.check_proxy_edge_cases()
        smoke.check_proxy_concurrency()
        smoke.check_web_search_guard()
        smoke.agent_runtime = "codex"
        smoke.check_task()
        smoke.agent_runtime = "claude_code"
        smoke.check_claude_auth_and_task()
        smoke.check_both_runtimes_active()
        smoke.check_agent_parallelism()
        for runtime in SMOKE_RUNTIMES:
            smoke.agent_runtime = runtime
            smoke.check_agent_steering()
            smoke.check_agent_kill_and_thread_survival()
        smoke.check_agent_thread_recall()
        smoke.check_runtime_deactivation_stops_running_tasks()
        smoke.check_reboot_recovery()
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
        self.region = ""
        self.result: dict | None = None
        self.tunnel_open = False
        self.passed = 0
        self.total = 0
        self.parallel_task_ids: dict[str, str] = {}  # completed parallel task id -> its token
        self.parallel_threads: dict[str, tuple[str, str]] = {}  # runtime -> (thread id, token)

    @property
    def managed_domains(self) -> tuple[str, ...]:
        return SMOKE_MANAGED_DOMAINS

    def task_body(self, input_message: str, thread_id: str, *, runtime: str | None = None) -> dict:
        return {"input_message": input_message, "thread_id": thread_id, "agent_runtime": runtime or self.agent_runtime}

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
        """
        return {
            "ssh_port_opened": True,
            "managed_ai_provider_network_access": dict(SMOKE_MANAGED_PROVIDERS),
            "allowed_network_access": {
                "api.github.com": {"allow_http_methods": ["GET"], "path_guards": ["^/$", "^/zen$"]},
                "*.githubusercontent.com": {"allow_http_methods": ["GET"]},
            },
        }

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
            "ssh_public_key": public_key,
            "network_controls": {
                "ssh_port_opened": True,
                "allowed_network_access": {},
            },
        }
        self.effective_config.write_text(json.dumps(raw))
        self.config = load_input_config(self.effective_config)
        self.region = self.config.aws_region

    def deploy(self) -> None:
        self._step("deploy host")
        env = os.environ.copy()
        env["PYTHONPATH"] = str(REPO_ROOT)
        subprocess.run(
            [sys.executable, "-m", "host.deploy", "--config", str(self.effective_config)],
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
        if self.tunnel_open:
            subprocess.run(
                ["ssh", "-S", str(self.control_socket), "-O", "exit", f"trustyclaw-operator@{self.result['public_dns']}"],
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=False,
            )
        if self.result:
            print("\nTearing down...")
            self._aws("ec2", "terminate-instances", "--instance-ids", self.result["instance_id"])
            self._aws("ec2", "wait", "instance-terminated", "--instance-ids", self.result["instance_id"])
            deleted_volumes: list[str] = []
            for key in ("admin_volume_id", "agent_volume_id"):
                volume_id = self.result.get(key)
                if not isinstance(volume_id, str) or not volume_id:
                    continue
                try:
                    self._aws("ec2", "wait", "volume-available", "--volume-ids", volume_id)
                    self._aws("ec2", "delete-volume", "--volume-id", volume_id)
                    deleted_volumes.append(volume_id)
                except subprocess.CalledProcessError as exc:
                    print(f"warning: could not delete {key} {volume_id}: {exc}", file=sys.stderr)
            groups = self._aws(
                "ec2", "describe-security-groups",
                "--filters", f"Name=group-name,Values=trustyclaw-host-{self.config.agent_name}",
            ).get("SecurityGroups", [])
            for group in groups:
                self._aws("ec2", "delete-security-group", "--group-id", group["GroupId"])
            volume_note = f" and deleted data volumes {', '.join(deleted_volumes)}" if deleted_volumes else ""
            print(f"terminated {self.result['instance_id']}, deleted its security group{volume_note}")
        shutil.rmtree(self.workdir, ignore_errors=True)

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
        config = json.loads(self._ssh_code("sudo cat /mnt/trustyclaw-admin/admin-state/config.json"))
        if config.get("agent_name") != SMOKE_AGENT_NAME:
            raise AssertionError(f"host config has wrong agent_name: {config}")
        if "agent_runtime" in config:
            raise AssertionError(f"host config still persists agent_runtime: {config}")
        if "agent_type" in config:
            raise AssertionError(f"host config still persists agent_type: {config}")
        if "admin_password_sha256" not in config:
            raise AssertionError(f"host config missing password hash: {config}")
        account_files = json.loads(self._ssh_code(
            "sudo python3 - <<'PY'\n"
            "import grp, json, os, pwd\n"
            "result = {}\n"
            "for base, label in (('/mnt/trustyclaw-admin/admin-state/', 'admin'), ('/mnt/trustyclaw-admin/proxy-state/', 'proxy')):\n"
            "  for name in ('openai_account.json', 'claude_account.json'):\n"
            "    path = base + name\n"
            "    st = os.stat(path)\n"
            "    body = json.load(open(path))\n"
            "    result[label + '/' + name] = {'owner': pwd.getpwuid(st.st_uid).pw_name,\n"
            "                    'group': grp.getgrgid(st.st_gid).gr_name,\n"
            "                    'mode': oct(st.st_mode & 0o777), 'body': body}\n"
            "print(json.dumps(result))\n"
            "PY"
        ))
        expected = {
            "admin/openai_account.json": ("trustyclaw-admin", "trustyclaw-admin", "0o600", {"account_id": None}),
            "admin/claude_account.json": ("trustyclaw-admin", "trustyclaw-admin", "0o600", {}),
            "proxy/openai_account.json": ("trustyclaw-proxy", "trustyclaw-proxy", "0o600", {"account_id": None}),
            "proxy/claude_account.json": ("trustyclaw-proxy", "trustyclaw-proxy", "0o600", {}),
        }
        for name, expected_values in expected.items():
            account_file = account_files.get(name, {})
            actual = (
                account_file.get("owner"),
                account_file.get("group"),
                account_file.get("mode"),
                account_file.get("body"),
            )
            if actual != expected_values:
                raise AssertionError(f"{name} ownership/mode/body mismatch: {account_file}")
        storage_layout = json.loads(self._ssh_code(
            "sudo python3 - <<'PY'\n"
            "import grp, json, os, pwd\n"
            "paths = [\n"
            "    '/mnt/trustyclaw-admin',\n"
            "    '/mnt/trustyclaw-admin/admin-state',\n"
            "    '/mnt/trustyclaw-admin/proxy-state',\n"
            "    '/mnt/trustyclaw-admin/proxy-state/network_controls.json',\n"
            "    '/mnt/trustyclaw-admin/proxy-state/network_status.json',\n"
            "    '/mnt/trustyclaw-admin/proxy-state/openai_account.json',\n"
            "    '/mnt/trustyclaw-admin/proxy-state/claude_account.json',\n"
            "    '/mnt/trustyclaw-admin/proxy-state/.network_policy.lock',\n"
            "    '/mnt/trustyclaw-admin/proxy-state/network_events.jsonl',\n"
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
            "names = ['trustyclaw-admin', 'trustyclaw-proxy', 'trustyclaw-agent']\n"
            "print(json.dumps({name: {'uid': pwd.getpwnam(name).pw_uid, 'gid': grp.getgrnam(name).gr_gid} for name in names}))\n"
            "PY"
        ))
        expected_service_ids = {
            "trustyclaw-admin": {"uid": 47741, "gid": 47741},
            "trustyclaw-proxy": {"uid": 47742, "gid": 47742},
            "trustyclaw-agent": {"uid": 47743, "gid": 47743},
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
            "sudo -u trustyclaw-admin bash -c '! test -r /mnt/trustyclaw-admin/proxy-state/network_controls.json' && "
            "sudo -u trustyclaw-proxy bash -c '! test -r /mnt/trustyclaw-admin/admin-state/config.json' && "
            "echo ok"
        ).strip()
        if partition_access != "ok":
            raise AssertionError("admin-state and proxy-state must be directly unreadable across service users")
        expected_layout = {
            "/mnt/trustyclaw-admin": ("root", "root", "0o711", False),
            "/mnt/trustyclaw-admin/admin-state": ("trustyclaw-admin", "trustyclaw-admin", "0o700", False),
            "/mnt/trustyclaw-admin/proxy-state": ("trustyclaw-proxy", "trustyclaw-proxy", "0o700", False),
            "/mnt/trustyclaw-admin/proxy-state/network_controls.json": ("trustyclaw-proxy", "trustyclaw-proxy", "0o600", False),
            "/mnt/trustyclaw-admin/proxy-state/network_status.json": ("trustyclaw-proxy", "trustyclaw-proxy", "0o600", False),
            "/mnt/trustyclaw-admin/proxy-state/openai_account.json": ("trustyclaw-proxy", "trustyclaw-proxy", "0o600", False),
            "/mnt/trustyclaw-admin/proxy-state/claude_account.json": ("trustyclaw-proxy", "trustyclaw-proxy", "0o600", False),
            "/mnt/trustyclaw-admin/proxy-state/.network_policy.lock": ("trustyclaw-proxy", "trustyclaw-proxy", "0o600", False),
            "/mnt/trustyclaw-admin/proxy-state/network_events.jsonl": ("trustyclaw-proxy", "trustyclaw-proxy", "0o600", False),
            "/mnt/trustyclaw-admin/proxy-state/network_proxy_ca.key": ("trustyclaw-proxy", "trustyclaw-proxy", "0o600", False),
            "/mnt/trustyclaw-admin/proxy-state/network_proxy_ca.crt": ("trustyclaw-proxy", "trustyclaw-proxy", "0o644", False),
        }
        for path, expected_values in expected_layout.items():
            entry = storage_layout.get(path, {})
            actual = (entry.get("owner"), entry.get("group"), entry.get("mode"), entry.get("symlink"))
            if actual != expected_values:
                raise AssertionError(f"{path} ownership/mode mismatch: {entry}")
        self._ok(
            "host config persists runtime/name/password; admin/proxy state are private and helper-readable"
        )

    def check_network_policy(self) -> None:
        self._step("network policy get/replace")
        self._api("PUT", "/v1/network/policy", self.enforcement_policy())
        policy = self._api("GET", "/v1/network/policy")
        controls = policy["network_controls"]
        expected_provider = dict(SMOKE_MANAGED_PROVIDERS)
        if controls.get("managed_ai_provider_network_access") != expected_provider:
            raise AssertionError(f"policy did not preserve explicit managed provider: {controls}")
        rules = controls["allowed_network_access"]
        if "api.github.com" not in rules:
            raise AssertionError("replaced policy not reflected in GET")
        for host in self.managed_domains:
            if host in rules:
                raise AssertionError(f"managed provider rule {host} leaked into API policy response: {rules}")
        internal = json.loads(self._ssh_code("sudo cat /mnt/trustyclaw-admin/proxy-state/network_controls.json"))["network_controls"]
        internal_rules = internal["allowed_network_access"]
        if internal.get("managed_ai_provider_network_access") != expected_provider:
            raise AssertionError(f"stored policy did not preserve explicit managed provider: {internal}")
        for host in self.managed_domains:
            if host in internal_rules:
                raise AssertionError(f"managed provider rule {host} leaked into stored policy: {internal}")
        self._ok("policy read back and stored user-facing; proxy expands managed AI provider rules in memory")

    def check_initial_disabled_provider_deploy(self) -> None:
        self._step("initial deploy with managed providers disabled")
        policy = self._api("GET", "/v1/network/policy")["network_controls"]
        if policy.get("managed_ai_provider_network_access", {}) != {}:
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
            status, body = self._api_status("POST", login_path, idem=self._idem(label))
            if status != 409:
                raise AssertionError(f"{login_path} while initially deactivated returned {status}, expected 409: {body}")
        self._api("PUT", "/v1/network/policy", self.enforcement_policy())
        for runtime in SMOKE_RUNTIMES:
            status = self._wait_for_runtime_status({"loading", "awaiting_login", "active"}, runtime=runtime, timeout=120)
            if status not in {"loading", "awaiting_login", "active"}:
                raise AssertionError(f"{runtime} did not wake after enabling provider access, got {status}")
        self._ok("first boot fails closed with providers omitted; enabling policy wakes both runtimes")

    def check_ui_page(self) -> None:
        self._step("admin UI page served at / without auth")
        request = urllib.request.Request(f"http://127.0.0.1:{ADMIN_PORT}/")
        with urllib.request.urlopen(request, timeout=30) as response:
            content_type = response.headers.get("Content-Type", "")
            page = response.read().decode()
        if "text/html" not in content_type:
            raise AssertionError(f"UI page content type is {content_type!r}")
        if "TrustyClaw" not in page:
            raise AssertionError("UI page does not look like the admin UI")
        self._ok("static UI page served unauthenticated; API routes still require auth")

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
            + b"Idempotency-Key: smoke-admin-bad-length\r\n"
            b"Content-Length: nope\r\n\r\n",
        )
        huge = self._raw_local_http(
            ADMIN_PORT,
            b"POST /v1/tasks HTTP/1.1\r\n"
            b"Host: 127.0.0.1\r\n"
            + f"Authorization: Bearer {self.result['admin_password']}\r\n".encode()
            + b"Idempotency-Key: smoke-admin-huge-length\r\n"
            b"Content-Length: 1048577\r\n\r\n",
        )
        if b"400" not in malformed or b"malformed Content-Length" not in malformed:
            raise AssertionError(f"admin API malformed Content-Length was not rejected cleanly: {malformed[:300]!r}")
        if b"413" not in huge or b"request body too large" not in huge:
            raise AssertionError(f"admin API huge Content-Length was not rejected cleanly: {huge[:300]!r}")
        self._ok("401 without/with wrong credentials; UI served unauthenticated; malformed admin bodies fail closed")

    def check_policy_validation_and_concurrency(self) -> None:
        self._step("policy validation, ssh pin, and concurrent replaces")
        # The deploy-time ssh_port_opened value cannot be changed at runtime.
        pinned = {
            "ssh_port_opened": False,
            "managed_ai_provider_network_access": dict(SMOKE_MANAGED_PROVIDERS),
            "allowed_network_access": {},
        }
        status, body = self._api_status("PUT", "/v1/network/policy", pinned, idem=self._idem("ssh-pin"))
        if status != 400:
            raise AssertionError(f"ssh_port_opened change returned {status}, expected 400: {body}")
        invalid = {
            "ssh_port_opened": True,
            "managed_ai_provider_network_access": dict(SMOKE_MANAGED_PROVIDERS),
            "allowed_network_access": {"api.github.com": {"allow_http_methods": ["BOGUS"]}},
        }
        status, body = self._api_status("PUT", "/v1/network/policy", invalid, idem=self._idem("bad-method"))
        if status != 400:
            raise AssertionError(f"invalid policy returned {status}, expected 400: {body}")
        disabled_provider_policy = {"ssh_port_opened": True, "allowed_network_access": {}}
        status, body = self._api_status(
            "PUT", "/v1/network/policy", disabled_provider_policy, idem=self._idem("provider-disabled")
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
            status, _ = self._api_status("POST", login_path, idem=self._idem(label))
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
            policy = {"ssh_port_opened": True, "managed_ai_provider_network_access": providers, "allowed_network_access": {}}
            status, body = self._api_status("PUT", "/v1/network/policy", policy, idem=self._idem(label))
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
            status, _ = self._api_status("POST", disabled_login_path, idem=self._idem(f"{label}-disabled-login"))
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
                    "ssh_port_opened": True,
                    "managed_ai_provider_network_access": {"openai": True, "claude": True},
                    "allowed_network_access": {"chatgpt.com": {"allow_http_methods": ["GET"]}},
                },
                "managed_ai_provider_network_access.openai",
            ),
            (
                "self-managed-claude-domain",
                {
                    "ssh_port_opened": True,
                    "managed_ai_provider_network_access": {"openai": True, "claude": True},
                    "allowed_network_access": {"api.anthropic.com": {"allow_http_methods": ["POST"]}},
                },
                "managed_ai_provider_network_access.claude",
            ),
            (
                "user-openai-managed-flag",
                {
                    "ssh_port_opened": True,
                    "managed_ai_provider_network_access": dict(SMOKE_MANAGED_PROVIDERS),
                    "allowed_network_access": {
                        "api.github.com": {
                            "allow_http_methods": ["GET"],
                            "openai_disable_live_web_search": True,
                        }
                    },
                },
                "unsupported fields",
            ),
            (
                "user-openai-account-guard-flag",
                {
                    "ssh_port_opened": True,
                    "managed_ai_provider_network_access": dict(SMOKE_MANAGED_PROVIDERS),
                    "allowed_network_access": {
                        "api.github.com": {
                            "allow_http_methods": ["GET"],
                            "openai_account_guard": True,
                        }
                    },
                },
                "unsupported fields",
            ),
        ):
            status, body = self._api_status("PUT", "/v1/network/policy", bad_policy, idem=self._idem(label))
            if status != 400:
                raise AssertionError(f"{label} policy returned {status}, expected 400: {body}")
            error = body.get("error", {})
            message = error.get("message", "") if isinstance(error, dict) else str(error)
            if expected_error not in message:
                raise AssertionError(f"{label} error should mention {expected_error}, got: {body}")

        # Concurrent replaces must serialize: each one either succeeds or is
        # turned away with 409 (the lock wait is bounded), the final policy is
        # exactly one of the successful requests, and enforcement ends active.
        # A torn or interleaved write would leave a policy nobody requested, or
        # a stuck "reloading" status.
        variants = [
            {
                "ssh_port_opened": True,
                "managed_ai_provider_network_access": dict(SMOKE_MANAGED_PROVIDERS),
                "allowed_network_access": {f"smoke-{index}.example.com": {"allow_http_methods": ["GET"]}},
            }
            for index in range(4)
        ]
        results = self._parallel(
            len(variants),
            lambda index: self._api_status(
                "PUT", "/v1/network/policy", variants[index], idem=self._idem(f"cc-policy-{index}")
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
        self._step("task lifecycle transitions and 4xx contract")
        task = self._api("POST", "/v1/tasks", self.task_body("lifecycle check (smoke)", "smoke-lifecycle"))
        task_id = task["task_id"]
        if task["status"] != "queued":
            raise AssertionError(f"new task is {task['status']}, expected queued")
        listed = self._active_tasks()
        if not any(item["task_id"] == task_id and "queue_position" in item for item in listed):
            raise AssertionError(f"queued task missing from the list (or has no queue_position): {listed}")

        status, _ = self._api_status("GET", "/v1/tasks/task_999999")
        if status != 404:
            raise AssertionError(f"unknown task returned {status}, expected 404")

        updated = self._api("PUT", f"/v1/tasks/{task_id}", {"input_message": "lifecycle check, updated (smoke)"})
        if updated["input_message"] != "lifecycle check, updated (smoke)":
            raise AssertionError(f"queued task update not reflected: {updated}")
        status, _ = self._api_status("PUT", f"/v1/tasks/{task_id}", {"input_message": ""}, idem=self._idem("empty"))
        if status != 400:
            raise AssertionError(f"empty input_message returned {status}, expected 400")
        status, _ = self._api_status(
            "PUT", f"/v1/tasks/{task_id}", {"input_message": "x" * (MESSAGE_LIMIT + 1)}, idem=self._idem("huge")
        )
        if status != 400:
            raise AssertionError(f"oversized input_message returned {status}, expected 400")
        status, _ = self._api_status(
            "POST", f"/v1/tasks/{task_id}/steer", {"steer_message": "steer (smoke)"}, idem=self._idem("steer")
        )
        if status != 409:
            raise AssertionError(f"steering a queued task returned {status}, expected 409")
        status, _ = self._api_status("POST", f"/v1/tasks/{task_id}/kill", idem=self._idem("kill-queued"))
        if status != 409:
            raise AssertionError(f"killing a queued task returned {status}, expected 409")
        status, _ = self._api_status("POST", "/v1/tasks/task_999999/kill", idem=self._idem("kill-404"))
        if status != 404:
            raise AssertionError(f"killing an unknown task returned {status}, expected 404")
        for label, bad_body in (
            ("no-runtime", {"input_message": "missing runtime (smoke)", "thread_id": "smoke-bad-runtime"}),
            ("bad-runtime", {"input_message": "bad runtime (smoke)", "thread_id": "smoke-bad-runtime", "agent_runtime": "bad"}),
            ("no-thread", {"input_message": "missing thread (smoke)", "agent_runtime": self.agent_runtime}),
            ("bad-thread", {"input_message": "bad thread (smoke)", "thread_id": "not valid!", "agent_runtime": self.agent_runtime}),
        ):
            status, _ = self._api_status("POST", "/v1/tasks", bad_body, idem=self._idem(label))
            if status != 400:
                raise AssertionError(f"create with {label} returned {status}, expected 400")

        self._api("POST", f"/v1/tasks/{task_id}/cancel")
        cancelled = self._api("GET", f"/v1/tasks/{task_id}")
        if cancelled["status"] != "cancelled":
            raise AssertionError(f"cancel left task {cancelled['status']}")
        status, _ = self._api_status("POST", f"/v1/tasks/{task_id}/cancel", idem=self._idem("recancel"))
        if status != 409:
            raise AssertionError(f"cancelling a cancelled task returned {status}, expected 409")
        status, _ = self._api_status(
            "PUT", f"/v1/tasks/{task_id}", {"input_message": "resurrect (smoke)"}, idem=self._idem("resurrect")
        )
        if status != 409:
            raise AssertionError(f"updating a cancelled task returned {status}, expected 409")

        events = self._api("GET", f"/v1/tasks/{task_id}/events?since=0")["events"]
        if any(event["event_type"] == "task.message" and event["task_id"] == task_id for event in events):
            raise AssertionError(f"queued task emitted a task.message before running: {events}")
        if [event["event_type"] for event in events] != ["task.cancelled"]:
            raise AssertionError(f"queued cancel should only emit task.cancelled, got: {events}")
        if any(event["task_id"] != task_id for event in events):
            raise AssertionError("per-task events leaked another task's events")
        self._ok("queued update/cancel/kill honored; thread_id validated; terminal transitions rejected; events scoped")

    def check_task_pagination(self) -> None:
        """last_seen_task_id paging over a stable, pre-login queue (the runtime
        is not active yet, so nothing changes status between pages)."""
        self._step("task list pagination (7 queued tasks, 5-per-page)")
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
            cursor = page[-1]["task_id"]
        ours = [task_id for task_id in seen if task_id in created]
        if ours != created:
            raise AssertionError(f"pagination lost or reordered tasks: saw {ours}, created {created}")
        if len(seen) != len(set(seen)):
            raise AssertionError(f"pagination returned duplicates: {seen}")
        for task_id in created:
            self._api("POST", f"/v1/tasks/{task_id}/cancel")
        self._ok("7 tasks paged in creation order with no duplicates")

    def check_idempotency(self) -> None:
        self._step("idempotency replay, conflicts, and key validation")
        key = self._idem("replay")
        body = self.task_body("idempotency check (smoke)", "smoke-idem")
        status, first = self._api_status("POST", "/v1/tasks", body, idem=key)
        if status != 200:
            raise AssertionError(f"task create returned {status}: {first}")
        status, replay = self._api_status("POST", "/v1/tasks", body, idem=key)
        if status != 200 or replay["task_id"] != first["task_id"]:
            raise AssertionError(f"replay did not return the original response: {status} {replay}")
        active = [item for item in self._active_tasks() if item["input_message"] == body["input_message"]]
        if len(active) != 1:
            raise AssertionError(f"replay created a duplicate task: {active}")

        status, _ = self._api_status("PUT", "/v1/network/policy", self.enforcement_policy(), idem=key)
        if status != 400:
            raise AssertionError(f"key reuse on a different request returned {status}, expected 400")
        status, _ = self._api_status("POST", "/v1/tasks", body, idem=None)  # no key at all
        if status != 400:
            raise AssertionError(f"mutation without Idempotency-Key returned {status}, expected 400")
        status, _ = self._api_status("POST", "/v1/tasks", body, idem="bad key!")
        if status != 400:
            raise AssertionError(f"invalid Idempotency-Key returned {status}, expected 400")

        self._api("POST", f"/v1/tasks/{first['task_id']}/cancel")
        self._ok("replay returned the original task once; cross-request and malformed keys rejected")

    def check_admin_concurrency(self) -> None:
        self._step("concurrent task creation and a same-key idempotency storm")
        before = {item["task_id"] for item in self._active_tasks()}

        # Distinct keys: every create must land exactly once with a unique id,
        # interleaved with health reads that must never block or fail.
        creates = 8
        nonce = time.time_ns()

        def create_or_health(index: int) -> tuple[int, dict]:
            if index >= creates:
                return self._api_status("GET", "/v1/health")
            return self._api_status(
                "POST", "/v1/tasks",
                self.task_body(f"concurrent create {index} (smoke)", f"smoke-cc-{index}"),
                idem=f"smoke-cc-{nonce}-{index}",
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

        # Same key from every thread: the reservation must let exactly one
        # execute; the rest replay the stored response or see 409 in-flight.
        storm_key = self._idem("storm")
        storm_body = self.task_body("idempotency storm (smoke)", "smoke-storm")
        storm = self._parallel(8, lambda _: self._api_status("POST", "/v1/tasks", storm_body, idem=storm_key))
        storm_ids = {body["task_id"] for status, body in storm if status == 200}
        bad = [status for status, _ in storm if status not in (200, 409)]
        if bad:
            raise AssertionError(f"same-key storm returned unexpected statuses: {bad}")
        if len(storm_ids) > 1:
            raise AssertionError(f"same-key storm created more than one task: {storm_ids}")
        # Whether the racers replayed or got 409, the key must resolve to
        # exactly one stored task afterwards.
        status, settled = self._api_status("POST", "/v1/tasks", storm_body, idem=storm_key)
        if status != 200:
            raise AssertionError(f"storm key did not settle into a replayable response: {status} {settled}")
        storm_ids.add(settled["task_id"])
        if len(storm_ids) != 1:
            raise AssertionError(f"storm replay disagrees with the storm winners: {storm_ids}")

        after = {item["task_id"] for item in self._active_tasks()}
        new_ids = after - before
        expected = set(created_ids) | storm_ids
        if new_ids != expected:
            raise AssertionError(f"task list out of sync after concurrency: new {new_ids}, expected {expected}")
        for task_id in sorted(new_ids):
            self._api("POST", f"/v1/tasks/{task_id}/cancel")
        if {item["task_id"] for item in self._active_tasks()} != before:
            raise AssertionError("queued smoke tasks were not all cancelled")
        self._ok(f"{creates} parallel creates unique, storm created exactly one task, list consistent")

    def check_state_transactions(self) -> None:
        """Edge cases of the state.json read-modify-write transaction under
        real concurrency: check-then-act atomicity for terminal transitions
        (exactly one racing cancel wins), racing field updates that must be
        last-writer-wins (never merged or torn), reads that stay fast and
        consistent mid-storm, and event seqs that stay unique across parallel
        writers."""
        self._step("state transaction edge cases (atomic cancel, racing updates, seq uniqueness)")
        before = {item["task_id"] for item in self._active_tasks()}

        # 1. Concurrent cancels of one queued task. The QUEUED check and the
        # CANCELLED write share one transaction, so exactly one racer can win;
        # a lost-update regression would let several see QUEUED and all "win".
        _, target = self._api_status(
            "POST", "/v1/tasks",
            self.task_body("cancel race target (smoke)", "smoke-tx-cancel"),
        )
        cancel_id = target["task_id"]
        cancels = self._parallel(
            6, lambda i: self._api_status("POST", f"/v1/tasks/{cancel_id}/cancel", idem=self._idem(f"tx-cancel-{i}"))
        )
        statuses = sorted(status for status, _ in cancels)
        if statuses.count(200) != 1 or statuses != [200] + [409] * (len(cancels) - 1):
            raise AssertionError(f"concurrent cancels must yield exactly one 200 and the rest 409, got {statuses}")
        if self._api("GET", f"/v1/tasks/{cancel_id}")["status"] != "cancelled":
            raise AssertionError("cancel race target did not end cancelled")

        # 2. Racing updates to one queued task, interleaved with reads. Every
        # update must apply atomically: the final message is exactly one
        # racer's payload (last writer wins), never a torn or merged value,
        # and the concurrent reads never block or fail.
        _, target = self._api_status(
            "POST", "/v1/tasks",
            self.task_body("update race target (smoke)", "smoke-tx-update"),
        )
        update_id = target["task_id"]
        updaters = 6
        candidates = {f"update racer {index} (smoke)" for index in range(updaters)}

        def update_or_read(index: int) -> tuple[int, dict]:
            if index >= updaters:
                return self._api_status("GET", f"/v1/tasks/{update_id}" if index % 2 else "/v1/health")
            return self._api_status(
                "PUT", f"/v1/tasks/{update_id}",
                {"input_message": f"update racer {index} (smoke)"},
                idem=self._idem(f"tx-update-{index}"),
            )

        results = self._parallel(updaters + 4, update_or_read)
        bad = [status for status, _ in results if status != 200]
        if bad:
            raise AssertionError(f"update/read storm returned non-200 statuses: {bad}")
        final = self._api("GET", f"/v1/tasks/{update_id}")
        if final["input_message"] not in candidates:
            raise AssertionError(f"racing updates produced a value no racer sent: {final['input_message']!r}")

        # 3. An update racing a cancel: each request is atomic, so every racer
        # sees 200 or a clean 409 (never a 5xx or a resurrected task), and the
        # task ends cancelled regardless of interleaving.
        def update_or_cancel(index: int) -> tuple[int, dict]:
            if index == 0:
                return self._api_status("POST", f"/v1/tasks/{update_id}/cancel", idem=self._idem("tx-mixed-cancel"))
            return self._api_status(
                "PUT", f"/v1/tasks/{update_id}",
                {"input_message": f"post-cancel racer {index} (smoke)"},
                idem=self._idem(f"tx-mixed-{index}"),
            )

        mixed = self._parallel(5, update_or_cancel)
        bad = [status for status, _ in mixed if status not in (200, 409)]
        if bad:
            raise AssertionError(f"update-vs-cancel race returned unexpected statuses: {bad}")
        if mixed[0][0] != 200:
            raise AssertionError(f"the cancel itself failed: {mixed[0]}")
        if self._api("GET", f"/v1/tasks/{update_id}")["status"] != "cancelled":
            raise AssertionError("update-vs-cancel race left the task un-cancelled")

        # 4. Parallel writers allocated event seqs through the transaction, so
        # the agent event log must hold no duplicate seq anywhere.
        seqs: list[int] = []
        cursor = 0
        while True:
            page = self._api("GET", f"/v1/events?since={cursor}")["events"]
            if not page:
                break
            page_seqs = [int(event["seq"]) for event in page]
            seqs.extend(page_seqs)
            cursor = max(page_seqs)
        if len(seqs) != len(set(seqs)):
            duplicates = sorted({seq for seq in seqs if seqs.count(seq) > 1})
            raise AssertionError(f"agent event log has duplicate seqs after the storms: {duplicates}")

        if {item["task_id"] for item in self._active_tasks()} != before:
            raise AssertionError("state transaction checks leaked active tasks")
        self._ok("cancel race won once, updates atomic, cancel sticky, event seqs unique")

    def check_event_pagination(self) -> None:
        self._step("agent event pagination (5-event pages, strict seq ordering)")
        seqs: list[int] = []
        cursor = 0
        while True:
            page = self._api("GET", f"/v1/events?since={cursor}")["events"]
            if not page:
                break
            if len(page) > 5:
                raise AssertionError(f"event page holds {len(page)} events, expected at most 5")
            page_seqs = [int(event["seq"]) for event in page]
            if any(seq <= cursor for seq in page_seqs):
                raise AssertionError(f"page contains seq <= since cursor {cursor}: {page_seqs}")
            seqs.extend(page_seqs)
            cursor = max(page_seqs)
        if len(seqs) < 6:
            raise AssertionError(f"expected the earlier checks to leave >5 events, found {len(seqs)}")
        if sorted(seqs) != seqs or len(set(seqs)) != len(seqs):
            raise AssertionError(f"event seqs are not strictly increasing/unique: {seqs}")
        self._ok(f"{len(seqs)} events paged with strictly increasing unique seqs")

    def check_enforcement(self) -> None:
        self._step("network enforcement (proxy + nftables, as the agent user)")
        proxy = f"http://127.0.0.1:{PROXY_PORT}"
        agent = "sudo -u trustyclaw-agent env"
        allowed = self._ssh_code(f"{agent} HTTPS_PROXY={proxy} curl -s -o /dev/null -w '%{{http_code}}' --max-time 20 https://api.github.com/")
        denied = self._ssh_code(f"{agent} HTTPS_PROXY={proxy} curl -s -o /dev/null -w '%{{http_code}}' --max-time 20 https://api.github.com/repos/openai/codex")
        direct = self._ssh_code(f"{agent} curl -s -o /dev/null -w '%{{http_code}}' --max-time 12 https://api.github.com/ || true")
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
        events = self._network_events()
        decisions = {event["decision"] for event in events}
        if not {"allowed", "denied"} <= decisions:
            raise AssertionError(f"expected both allowed and denied network events, saw {decisions}")
        self._ok(
            f"proxy allowed=200 denied=403, direct blocked ({direct or 'no connection'}), "
            f"admin loopback blocked ({loopback_admin or 'no connection'}), events logged"
        )

    def check_proxy_edge_cases(self) -> None:
        self._step("proxy protocol edge cases (ports, hosts, encodings, wildcards)")
        baseline = max((event["seq"] for event in self._network_events()), default=0)
        proxy = f"http://127.0.0.1:{PROXY_PORT}"
        agent = "sudo -u trustyclaw-agent env"

        # CONNECT to a non-443 port and to an unlisted host: both denied before
        # any DNS or dial; curl reports a proxy CONNECT failure (not an HTTP code).
        self._ssh_code(f"{agent} HTTPS_PROXY={proxy} curl -s -o /dev/null --max-time 12 https://api.github.com:444/ || true")
        self._ssh_code(f"{agent} HTTPS_PROXY={proxy} curl -s -o /dev/null --max-time 12 https://example.com/ || true")
        # Host header that does not match the CONNECT host: denied inside TLS.
        mismatch = self._ssh_code(
            f"{agent} HTTPS_PROXY={proxy} curl -s -o /dev/null -w '%{{http_code}}' --max-time 20 "
            f"-H 'Host: evil.example' https://api.github.com/"
        )
        # Percent-encoded path: the guard must match the decoded form —
        # /%7A%65%6E decodes to /zen, which ^/zen$ allows, while the raw
        # encoded path matches no guard. So a 403 here means the guard failed
        # to decode. The upstream's own status is not asserted: the proxy
        # forwards the path as sent, and GitHub routes the encoded form to 404.
        encoded = self._ssh_code(
            f"{agent} HTTPS_PROXY={proxy} curl -s -o /dev/null -w '%{{http_code}}' --max-time 20 "
            f"https://api.github.com/%7A%65%6E || true"
        )
        # Wildcard rule (*.githubusercontent.com) must admit a subdomain.
        wildcard = self._ssh_code(
            f"{agent} HTTPS_PROXY={proxy} curl -s -o /dev/null -w '%{{http_code}}' --max-time 20 "
            f"https://raw.githubusercontent.com/ || true"
        )
        # Plain HTTP rides the same policy path (curl only honors the
        # lowercase http_proxy variable for http:// URLs).
        plain = self._ssh_code(
            f"{agent} http_proxy={proxy} curl -s -o /dev/null -w '%{{http_code}}' --max-time 20 "
            f"http://api.github.com/ || true"
        )
        malformed_length = self._ssh_code(
            "sudo -u trustyclaw-agent python3 - <<'PY'\n"
            "import socket\n"
            f"s = socket.create_connection(('127.0.0.1', {PROXY_PORT}), timeout=10)\n"
            "s.sendall(b'GET http://api.github.com/ HTTP/1.1\\r\\n"
            "Host: api.github.com\\r\\n"
            "Content-Length: nope\\r\\n\\r\\n')\n"
            "s.shutdown(socket.SHUT_WR)\n"
            "print(s.recv(4096).decode('utf-8', 'replace'))\n"
            "s.close()\n"
            "PY"
        )

        if mismatch != "403":
            raise AssertionError(f"Host header mismatch returned {mismatch!r}, expected 403")
        if encoded in ("403", "", "000"):
            raise AssertionError(f"percent-encoded path matching a decoded guard returned {encoded!r}, expected the proxy to allow it")
        if wildcard == "403" or wildcard == "":
            raise AssertionError(f"wildcard-allowed host returned {wildcard!r}, expected non-403")
        if plain == "403" or plain == "":
            raise AssertionError(f"plain HTTP through the proxy returned {plain!r}, expected non-403")
        if "malformed Content-Length" not in malformed_length:
            raise AssertionError(f"proxy malformed Content-Length was not rejected cleanly: {malformed_length[:300]!r}")

        events = self._network_events(since=baseline)
        reasons = {event.get("reason") for event in events if event["decision"] == "denied"}
        if "only port 443 is allowed for CONNECT" not in reasons:
            raise AssertionError(f"non-443 CONNECT denial not logged; denied reasons: {reasons}")
        if "host is not in the allowed network policy" not in reasons:
            raise AssertionError(f"unknown-host denial not logged; denied reasons: {reasons}")
        if "malformed Content-Length" not in reasons:
            raise AssertionError(f"malformed Content-Length denial not logged; denied reasons: {reasons}")
        if not any(event["protocol"] == "http" and event["decision"] == "allowed" for event in events):
            raise AssertionError("plain HTTP request did not produce an allowed http event")
        if not any(event["host"] == "raw.githubusercontent.com" and event["decision"] == "allowed" for event in events):
            raise AssertionError("wildcard-matched request did not produce an allowed event")
        if not any(event["path"] == "/%7A%65%6E" and event["decision"] == "allowed" for event in events):
            raise AssertionError("percent-encoded request was not logged as an allowed decision")
        self._ok("port pin, unknown host, Host mismatch denied; decoded guard, wildcard, plain HTTP allowed")

    def check_proxy_concurrency(self) -> None:
        self._step("parallel proxy traffic with consistent event sequencing")
        baseline = max((event["seq"] for event in self._network_events()), default=0)
        proxy = f"http://127.0.0.1:{PROXY_PORT}"
        curl = "curl -s -o /dev/null -w '%{http_code}\\n' --max-time 25"
        script = " ".join(
            [f"{curl} https://api.github.com/ &" for _ in range(6)]
            + [f"{curl} https://api.github.com/denied-{index} &" for index in range(6)]
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
        events = [event for event in self._network_events(since=baseline) if event["host"] == "api.github.com"]
        seqs = [int(event["seq"]) for event in events]
        if len(events) != 12 or len(set(seqs)) != 12:
            raise AssertionError(f"expected 12 uniquely-sequenced events, got {len(events)} (seqs {sorted(seqs)})")
        decisions = [event["decision"] for event in events]
        if decisions.count("allowed") != 6 or decisions.count("denied") != 6:
            raise AssertionError(f"expected 6 allowed + 6 denied events, got {decisions}")
        self._ok("12 parallel requests all decided and logged with unique, ordered seqs")

    def check_web_search_guard(self) -> None:
        self._step("OpenAI data-plane fails closed before account id is known")
        self._api(
            "PUT",
            "/v1/network/policy",
            {
                "ssh_port_opened": True,
                "managed_ai_provider_network_access": dict(SMOKE_MANAGED_PROVIDERS),
                "allowed_network_access": {},
            },
        )
        proxy = f"http://127.0.0.1:{PROXY_PORT}"
        url = "https://chatgpt.com/backend-api/codex/responses"
        payload = '{"input":"hello"}'

        print(f"  POST {url} before account id is known", flush=True)
        response = self._ssh_code(
            f"sudo -u trustyclaw-agent env HTTPS_PROXY={proxy} "
            f"curl -s --max-time 20 -X POST -H 'Content-Type: application/json' "
            f"--data '{payload}' {url}"
        )
        print(f"  -> {response[:200]!r}", flush=True)
        if "OpenAI account id is not available" not in response:
            raise AssertionError(f"OpenAI data-plane request did not fail closed; proxy returned {response!r}")
        events = self._network_events()
        if not any(
            event["host"] == "chatgpt.com"
            and event["decision"] == "denied"
            and event.get("reason") == "OpenAI account id is not available"
            for event in events
        ):
            raise AssertionError("no account-id-missing chatgpt.com network denial was logged")
        self._ok("chatgpt.com denied before login because the account id is unknown")

    def check_claude_auth_and_task(self) -> None:
        self._step("Claude OAuth + Anthropic account guard + real task (interactive)")
        claude_section_baseline = max((event["seq"] for event in self._network_events()), default=0)
        self._api(
            "PUT",
            "/v1/network/policy",
            {
                "ssh_port_opened": True,
                "managed_ai_provider_network_access": dict(SMOKE_MANAGED_PROVIDERS),
                "allowed_network_access": {},
            },
        )
        proxy = f"http://127.0.0.1:{PROXY_PORT}"

        hello = self._ssh_code(
            f"sudo -u trustyclaw-agent env HTTPS_PROXY={proxy} "
            "curl -s -o /dev/null -w '%{http_code}' --max-time 20 "
            "https://api.anthropic.com/api/hello"
        )
        if hello == "403" or hello == "000" or hello == "":
            raise AssertionError(f"Claude unauthenticated readiness path returned {hello!r}, expected proxy allow")

        url = "https://api.anthropic.com/v1/messages"
        payload = '{"model":"claude-sonnet-4-5","max_tokens":8,"messages":[{"role":"user","content":"hello"}]}'
        print(f"  POST {url} before Claude account token hash is known", flush=True)
        response = self._ssh_code(
            f"sudo -u trustyclaw-agent env HTTPS_PROXY={proxy} "
            f"curl -s --max-time 20 -X POST -H 'Content-Type: application/json' "
            f"--data {shlex.quote(payload)} {shlex.quote(url)}"
        )
        print(f"  -> {response[:200]!r}", flush=True)
        if "Claude account token is not available" not in response:
            raise AssertionError(f"Anthropic API request did not fail closed before login; proxy returned {response!r}")

        status = self._wait_for_runtime_status({"awaiting_login", "active"}, timeout=120)
        if status == "awaiting_login":
            login = self._api("POST", "/v1/agent-runtime/claude-oauth-login")
            replay = self._api("GET", "/v1/agent-runtime/claude-oauth-login")
            repost = self._api("POST", "/v1/agent-runtime/claude-oauth-login")
            if replay["login_url"] != login["login_url"] or repost["login_url"] != login["login_url"]:
                raise AssertionError(f"Claude login replays disagree: {login} / {replay} / {repost}")
            print(f"\n  ACTION REQUIRED: open {login['login_url']}")
            code = input("  Paste the Claude Code login code, then press Enter: ").strip()
            if not code:
                raise AssertionError("Claude Code login code was empty")
            complete = self._api("POST", "/v1/agent-runtime/claude-oauth-login/complete", {"code": code})
            if complete.get("status") != "accepted":
                raise AssertionError(f"Claude login completion returned {complete}")
            status = self._wait_for_runtime_status({"active"}, timeout=180)
        if status != "active":
            last = self._api("GET", "/v1/agent-runtime/status")
            raise AssertionError(f"Claude runtime never became active after login (last status: {last})")

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

        print(f"  POST {url} without bearer after Claude account token hash is known", flush=True)
        missing = self._ssh_code(
            f"sudo -u trustyclaw-agent env HTTPS_PROXY={proxy} "
            f"curl -s --max-time 20 -X POST -H 'Content-Type: application/json' "
            f"--data {shlex.quote(payload)} {shlex.quote(url)}"
        )
        print(f"  -> {missing[:200]!r}", flush=True)
        print(f"  POST {url} with wrong bearer after Claude account token hash is known", flush=True)
        wrong = self._ssh_code(
            f"sudo -u trustyclaw-agent env HTTPS_PROXY={proxy} "
            f"curl -s --max-time 20 -X POST -H 'Content-Type: application/json' "
            f"-H 'Authorization: Bearer smoke-wrong-token' "
            f"--data {shlex.quote(payload)} {shlex.quote(url)}"
        )
        print(f"  -> {wrong[:200]!r}", flush=True)
        if "Claude bearer token is required" not in missing:
            raise AssertionError(f"missing Claude bearer was not blocked; proxy returned {missing!r}")
        if "Claude bearer token does not match" not in wrong:
            raise AssertionError(f"wrong Claude bearer was not blocked; proxy returned {wrong!r}")

        task_baseline_seq = max((event["seq"] for event in self._network_events()), default=0)
        prompt = "Reply with exactly the word CLAUDE_SMOKE_OK and nothing else."
        print(f"  submitting Claude task: {prompt!r}", flush=True)
        task = self._api("POST", "/v1/tasks", self.task_body(prompt, "smoke-claude"))
        done = self._wait_for_task(task["task_id"], timeout=240)
        if done["status"] != "completed":
            raise AssertionError(f"Claude task ended {done['status']}: {self._task_failure_detail(task['task_id'])}")
        if "CLAUDE_SMOKE_OK" not in (done.get("output_message") or ""):
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
        print(f"  submitting Claude follow-up task on same thread: {follow_up_prompt!r}", flush=True)
        follow_up = self._api("POST", "/v1/tasks", self.task_body(follow_up_prompt, "smoke-claude"))
        follow_up_done = self._wait_for_task(follow_up["task_id"], timeout=240)
        if follow_up_done["status"] != "completed":
            raise AssertionError(
                f"Claude follow-up ended {follow_up_done['status']}: "
                f"{self._task_failure_detail(follow_up['task_id'])}"
            )
        if "CLAUDE_SMOKE_OK" not in (follow_up_done.get("output_message") or ""):
            raise AssertionError(
                "Claude follow-up did not resume the persisted session context: "
                f"{follow_up_done.get('output_message')!r}"
            )
        self.print_network_events("Claude auth/task network events", since=claude_section_baseline)
        self._ok("Claude login/account guard passed; real Claude task completed and resumed through the proxy")

    def check_task(self) -> None:
        self._step("Codex login + web search task with the guard active (interactive)")
        # Allow the OpenAI endpoints Codex needs, with the live web search guard
        # active on the ChatGPT data plane.
        self._api(
            "PUT",
            "/v1/network/policy",
            {
                "ssh_port_opened": True,
                "managed_ai_provider_network_access": dict(SMOKE_MANAGED_PROVIDERS),
                "allowed_network_access": {},
            },
        )
        # The status is cached and refreshed by a background poller every 10s;
        # earlier steps ran under policies that deny Codex traffic, so wait for
        # a poll under the policy just applied before deciding whether to log in.
        status = self._wait_for_runtime_status({"awaiting_login", "active"}, timeout=120)
        if status == "awaiting_login":
            login = self._api("POST", "/v1/agent-runtime/codex-oauth-login")
            # GET must replay the same pending login, and a repeated POST must
            # reuse it rather than mint a second device code.
            replay = self._api("GET", "/v1/agent-runtime/codex-oauth-login")
            repost = self._api("POST", "/v1/agent-runtime/codex-oauth-login")
            if replay["device_code"] != login["device_code"] or repost["device_code"] != login["device_code"]:
                raise AssertionError(f"login replays disagree: {login} / {replay} / {repost}")
            print(f"\n  ACTION REQUIRED: open {login['login_url']} and enter code {login['device_code']}")
            input("  Press Enter once the Codex login is approved... ")
            status = self._wait_for_runtime_status({"active"}, timeout=180)
        if status != "active":
            last = self._api("GET", "/v1/agent-runtime/status")
            raise AssertionError(f"agent runtime never became active after login (last status: {last})")
        # Once active, OAuth endpoints refuse because there is no pending
        # device login. The account endpoint reports the inferred account id
        # used by the proxy account pin.
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

        print(f"  POST {url} without account header after account id is known", flush=True)
        missing_account_response = post_openai(cached, account_header=None)
        print(f"  -> {missing_account_response[:200]!r}", flush=True)
        print(f"  POST {url} with wrong account header after account id is known", flush=True)
        wrong_account_response = post_openai(cached, account_header=f"{account_id}-wrong")
        print(f"  -> {wrong_account_response[:200]!r}", flush=True)
        print(f"  POST {url} with live payload and pinned account", flush=True)
        live_response = post_openai(live)
        print(f"  -> {live_response[:200]!r}", flush=True)
        print(f"  POST {url} with cached payload and pinned account", flush=True)
        cached_response = post_openai(cached)
        print(f"  -> {cached_response[:200]!r}", flush=True)
        if "OpenAI account id header is required" not in missing_account_response:
            raise AssertionError(f"missing account header was not blocked; proxy returned {missing_account_response!r}")
        if "not the configured account" not in wrong_account_response:
            raise AssertionError(f"wrong account header was not blocked; proxy returned {wrong_account_response!r}")
        if "live web search is disabled" not in live_response:
            raise AssertionError(f"live web search payload was not blocked; proxy returned {live_response!r}")
        if "live web search is disabled" in cached_response:
            raise AssertionError("cached web search payload was incorrectly blocked")

        # Only the task's own traffic is in scope: earlier steps (the web-search
        # guard) deliberately logged denied chatgpt.com events, so anchor on the
        # latest event seq before submitting and ignore everything up to it.
        baseline_seq = max((event["seq"] for event in self._network_events()), default=0)
        print(f"  network event baseline: seq {baseline_seq}", flush=True)

        # Run a task that exercises web search. The host pins Codex to cached web
        # search and the guard blocks live, so the task completes only if the
        # agent's real ChatGPT traffic passes the guard.
        prompt = "Use your web search tool to check today's date, then reply with the word DONE."
        print(f"  submitting task: {prompt!r}", flush=True)
        task = self._api("POST", "/v1/tasks", self.task_body(prompt, "smoke-web"))
        print(f"  created {task['task_id']}", flush=True)
        start = time.time()
        deadline = start + 240
        current = task
        last_status = None
        while time.time() < deadline:
            current = self._api("GET", f"/v1/tasks/{task['task_id']}")
            if current["status"] != last_status:
                print(f"  task status: {current['status']} ({int(time.time() - start)}s)", flush=True)
                last_status = current["status"]
            if current["status"] in {"completed", "failed", "cancelled"}:
                break
            time.sleep(5)

        events = self._network_events(since=baseline_seq)
        traffic: dict[tuple[str, str], int] = {}
        for event in events:
            key = (event["host"], event["decision"])
            traffic[key] = traffic.get(key, 0) + 1
        for (host, decision), count in sorted(traffic.items()):
            print(f"  task traffic: {decision} {host} x{count}", flush=True)
        if current.get("output_message") is not None:
            print(f"  task output: {current['output_message']!r}", flush=True)
        chatgpt = [event for event in events if event["host"].endswith("chatgpt.com")]
        denied = [event for event in chatgpt if event["decision"] == "denied"]
        # Codex also sends ancillary chatgpt.com traffic (e.g. analytics whose
        # body mentions web_search without a parseable tool) that the guard
        # conservatively denies and the agent shrugs off. Only a denial on the
        # turn endpoint means the guard interfered with the task itself.
        fatal = [e for e in denied if e["path"].startswith("/backend-api/codex/responses")]
        for event in denied:
            if event not in fatal:
                print(
                    f"  tolerated denial: {event['method']} {event['host']}{event['path']}"
                    f" ({event.get('reason', 'no reason recorded')})",
                    flush=True,
                )
        if current["status"] != "completed":
            raise AssertionError(f"task did not complete: {current}; denied chatgpt.com events: {denied}")
        if fatal:
            raise AssertionError(f"the guard denied agent ChatGPT turn traffic (live web search?): {fatal}")
        if not any(event["decision"] == "allowed" for event in chatgpt):
            hosts = sorted({host for host, _ in traffic})
            raise AssertionError(f"no allowed chatgpt.com traffic was observed for the task; hosts seen: {hosts}")
        self._ok("web search task completed; agent ChatGPT traffic passed the live web search guard")

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

        self._api("PUT", "/v1/network/policy", {"ssh_port_opened": True, "allowed_network_access": {}})
        for runtime in SMOKE_RUNTIMES:
            status = self._wait_for_runtime_status({"deactivated"}, runtime=runtime, timeout=90)
            if status != "deactivated":
                raise AssertionError(f"{runtime} did not deactivate after provider disable: {status}")
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
                self.task_body(
                    (
                        "Earlier in this conversation you replied with a single uppercase token. "
                        "Reply with exactly that token again and nothing else."
                    ),
                    thread_id,
                    runtime=runtime,
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
        status, body = self._api_status("POST", f"/v1/tasks/{slow_id}/kill", idem=self._idem(f"kill-{self.agent_runtime}"))
        if status != 200 or body.get("status") != "accepted":
            raise AssertionError(f"kill returned {status}: {body}")
        killed = self._wait_for_task(slow_id, timeout=60)
        if killed["status"] != "cancelled":
            raise AssertionError(f"killed task ended {killed['status']}, expected cancelled")
        print(f"  kill settled in {time.time() - start:.1f}s", flush=True)

        follow = self._api(
            "POST",
            "/v1/tasks",
            self.task_body(
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
                self.task_body(
                    (
                        "Earlier in this conversation you replied with a single uppercase word. "
                        "Reply with exactly that word again and nothing else."
                    ),
                    thread_id,
                    runtime=runtime,
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
        status, body = self._api_status("POST", "/v1/host-runtime/reboot", idem=self._idem("reboot"))
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
                self.task_body(
                    (
                        "Earlier in this conversation you replied with a single uppercase token. "
                        "Reply with exactly that token again and nothing else."
                    ),
                    thread_id,
                    runtime=runtime,
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
        """Push the network event log past MAX_EVENTS so the proxy's
        prune actually fires (every PRUNE_EVERY-th append rewrites the file by
        rename) while the admin API concurrently reads it. This is the one
        locking path only a live host can exercise — two real processes, two
        uids: the cross-process flock plus the inode re-check in locked_jsonl
        must keep reads consistent across the rename, and the pruned file must
        stay proxy-private while the admin API still reads it through the
        read-network-state helper. Runs last because it leaves >10k events
        behind, which would slow any later check that drains the log from seq
        0."""
        self._step("network event prune under concurrent helper reads (cross-process flock)")
        tail = self._ssh_code("sudo tail -n 1 /mnt/trustyclaw-admin/proxy-state/network_events.jsonl")
        baseline = int(json.loads(tail)["seq"]) if tail.strip() else 0
        # Enough denied CONNECTs to exceed MAX_EVENTS lines plus two prune
        # boundaries, so at least one real prune happens during the storm.
        needed = max(MAX_EVENTS + 2 * PRUNE_EVERY + 50 - baseline, 2 * PRUNE_EVERY + 50)
        print(f"  pushing {needed} denied requests through the proxy (seq baseline {baseline})", flush=True)

        reader_failures: list[str] = []
        reader_reads = {"count": 0}
        stop = threading.Event()

        def reader() -> None:
            cursor = baseline
            while not stop.is_set():
                status, body = self._api_status("GET", f"/v1/network/events?since={cursor}")
                if status != 200:
                    reader_failures.append(f"GET /v1/network/events -> {status}: {body}")
                    return
                reader_reads["count"] += 1
                page_seqs = [int(event["seq"]) for event in body["events"]]
                if page_seqs:
                    if len(set(page_seqs)) != len(page_seqs) or min(page_seqs) <= cursor:
                        reader_failures.append(f"inconsistent page at cursor {cursor}: {page_seqs}")
                        return
                    cursor = max(page_seqs)
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
            raise AssertionError(f"concurrent reader failed during the prune storm: {reader_failures[0]}")
        if reader_reads["count"] < 10:
            raise AssertionError(f"reader only completed {reader_reads['count']} reads; the storm outpaced it entirely")

        # On-host verification of the pruned file: bounded, unique, ordered,
        # and still proxy-private after the proxy-user prune-by-rename.
        verdict = json.loads(self._ssh_code(
            "sudo python3 - <<'PY'\n"
            "import grp, json, os, pwd\n"
            "path = '/mnt/trustyclaw-admin/proxy-state/network_events.jsonl'\n"
            "seqs = [json.loads(l)['seq'] for l in open(path) if l.strip()]\n"
            "st = os.stat(path)\n"
            "print(json.dumps({'lines': len(seqs), 'unique': len(set(seqs)) == len(seqs),\n"
            "                  'ordered': seqs == sorted(seqs), 'min_seq': min(seqs), 'max_seq': max(seqs),\n"
            "                  'owner': pwd.getpwuid(st.st_uid).pw_name, 'group': grp.getgrgid(st.st_gid).gr_name,\n"
            "                  'mode': oct(st.st_mode & 0o777)}))\n"
            "PY"
        ))
        if verdict["lines"] > MAX_EVENTS + PRUNE_EVERY:
            raise AssertionError(f"event log was not pruned: {verdict['lines']} lines")
        if verdict["min_seq"] <= baseline:
            raise AssertionError(f"no prune dropped old events (min seq {verdict['min_seq']} <= baseline {baseline})")
        if not verdict["unique"] or not verdict["ordered"]:
            raise AssertionError(f"event log corrupt after prune storm: {verdict}")
        if (verdict["owner"], verdict["group"], verdict["mode"]) != ("trustyclaw-proxy", "trustyclaw-proxy", "0o600"):
            raise AssertionError(f"pruned file lost its ownership/mode: {verdict}")
        final = self._api("GET", f"/v1/network/events?since={verdict['max_seq'] - 5}")
        if not final["events"]:
            raise AssertionError("admin API cannot read the event log after the prune")
        self._ok(
            f"{needed} events stormed; {reader_reads['count']} concurrent reads stayed consistent; "
            f"file pruned to {verdict['lines']} lines, proxy-private mode intact"
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
        idem = f"smoke-{time.time_ns()}" if method != "GET" else None

        def attempt() -> dict:
            request = urllib.request.Request(f"http://127.0.0.1:{ADMIN_PORT}{path}", data=data, method=method)
            request.add_header("Authorization", f"Bearer {self.result['admin_password']}")
            if idem is not None:
                request.add_header("Idempotency-Key", idem)
            if body is not None:
                request.add_header("Content-Type", "application/json")
            with urllib.request.urlopen(request, timeout=30) as response:
                return json.loads(response.read())

        try:
            return attempt()
        except (urllib.error.URLError, ConnectionError) as exc:
            # The tunnel can drop during a long idle (e.g. the interactive login
            # wait). Re-establish it once and retry; the request carries a stable
            # Idempotency-Key, so a retried mutation will not double-execute.
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

    def _api_status(
        self, method: str, path: str, body: dict | None = None, *,
        idem: str | None = "__auto__", bearer: str | None = "__default__",
    ) -> tuple[int, dict]:
        """One-shot request returning (status, body) instead of raising on HTTP
        errors, for checks that assert specific 4xx behavior or run from
        threads (no tunnel-reopen side effects). ``bearer=None`` sends no
        Authorization header; ``idem=None`` sends no Idempotency-Key."""
        if bearer == "__default__":
            bearer = self.result["admin_password"]
        if idem == "__auto__":
            idem = self._idem("auto") if method != "GET" else None
        data = json.dumps(body).encode() if body is not None else None
        request = urllib.request.Request(f"http://127.0.0.1:{ADMIN_PORT}{path}", data=data, method=method)
        if bearer is not None:
            request.add_header("Authorization", f"Bearer {bearer}")
        if idem is not None and method != "GET":
            request.add_header("Idempotency-Key", idem)
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

    def _idem(self, label: str) -> str:
        return f"smoke-{label}-{time.time_ns()}"

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
        """Drain `/v1/network/events`, which pages 5 events at a time with a
        `since` seq cursor, into the full list of events after ``since``."""
        events: list[dict] = []
        cursor = since
        while True:
            page = self._api("GET", f"/v1/network/events?since={cursor}")["events"]
            if not page:
                return events
            if len(page) > 5:
                raise AssertionError(f"network event page holds {len(page)} events, expected at most 5")
            page_seqs = [int(event["seq"]) for event in page]
            if page_seqs != sorted(page_seqs):
                raise AssertionError(f"network event page is not sorted by seq: {page_seqs}")
            if any(seq <= cursor for seq in page_seqs):
                raise AssertionError(f"network event page contains seq <= since cursor {cursor}: {page_seqs}")
            if len(set(page_seqs)) != len(page_seqs):
                raise AssertionError(f"network event page contains duplicate seqs: {page_seqs}")
            events.extend(page)
            cursor = max(page_seqs)

    def _agent_account(self, runtime_type: str) -> dict:
        accounts = self._api("GET", "/v1/agent-runtime/account")["accounts"]
        for account in accounts:
            if account.get("agent_runtime") == runtime_type:
                return account
        raise AssertionError(f"account summary did not include {runtime_type}: {accounts}")

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
        status = self.runtime_status_record(self._api("GET", "/v1/agent-runtime/status"), runtime)["status"]
        print(
            f"  {runtime} runtime status: {status} (waiting for {'/'.join(sorted(wanted))})",
            flush=True,
        )
        while time.time() < deadline and status not in wanted:
            time.sleep(5)
            previous, status = status, self.runtime_status_record(
                self._api("GET", "/v1/agent-runtime/status"), runtime
            )["status"]
            if status != previous:
                print(f"  {runtime} runtime status: {status}", flush=True)
        return status

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
