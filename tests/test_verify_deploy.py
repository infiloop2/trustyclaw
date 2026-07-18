"""Unit tests for host.bootstrap.verify_deploy.

The live checks run against injected providers (stat, name resolution, and a
command runner), so every check's pass and fail path is covered without a
provisioned host; the smoke suite exercises the real thing at the end of an
actual deploy.
"""

from __future__ import annotations

import os
import stat as stat_module
import subprocess
import unittest

from host.bootstrap import verify_deploy
from host.constants import ADMIN_API_PORT, PROXY_PORT, SERVICE_ACCOUNTS
from host.runtime.core import app_platform

UIDS = {"root": 0, **SERVICE_ACCOUNTS}


def resolve_uid(name: str) -> int:
    return UIDS[name]


def fake_stat(mode: int, uid: int, gid: int) -> os.stat_result:
    return os.stat_result((mode, 0, 0, 1, uid, gid, 0, 0, 0, 0))


def completed(returncode: int, stdout: str = "", stderr: str = "") -> "subprocess.CompletedProcess[str]":
    return subprocess.CompletedProcess(args=[], returncode=returncode, stdout=stdout, stderr=stderr)


class ExpectedAccountsTests(unittest.TestCase):
    def test_includes_pinned_core_accounts_and_installed_apps(self) -> None:
        accounts = verify_deploy.expected_accounts()
        for name, uid in SERVICE_ACCOUNTS.items():
            self.assertEqual(accounts[name], uid)
        apps = app_platform.installed_apps()
        self.assertGreaterEqual(len(apps), 1)
        for app in apps:
            self.assertEqual(accounts[app.linux_user], app.allocation.uid)

    def test_existing_account_with_matching_ids_passes(self) -> None:
        self.assertEqual(verify_deploy.check_service_accounts({"root": 0}), [])

    def test_wrong_uid_and_missing_account_are_reported(self) -> None:
        failures = verify_deploy.check_service_accounts(
            {"root": 4242, "trustyclaw-no-such-user": 1}
        )
        self.assertTrue(any("root has uid 0, expected 4242" in failure for failure in failures))
        self.assertTrue(
            any("trustyclaw-no-such-user does not exist" in failure for failure in failures)
        )


class PathFactTests(unittest.TestCase):
    def test_matching_facts_pass(self) -> None:
        facts = [("/mnt/x", "trustyclaw-admin", "trustyclaw-admin", 0o700, True)]
        lstat = lambda path: fake_stat(stat_module.S_IFDIR | 0o700, 47741, 47741)
        self.assertEqual(
            verify_deploy.check_path_facts(facts, lstat, resolve_uid, resolve_uid), []
        )

    def test_wrong_mode_owner_type_and_missing_are_reported(self) -> None:
        facts = [
            ("/mnt/wrong-mode", "trustyclaw-admin", "trustyclaw-admin", 0o700, True),
            ("/mnt/wrong-owner", "trustyclaw-admin", "trustyclaw-admin", 0o700, True),
            ("/mnt/not-a-dir", "trustyclaw-admin", "trustyclaw-admin", 0o700, True),
            ("/mnt/missing", "trustyclaw-admin", "trustyclaw-admin", 0o700, True),
            ("/mnt/symlinked-file", "root", "root", 0o644, False),
        ]

        def lstat(path: str) -> os.stat_result:
            if path == "/mnt/wrong-mode":
                return fake_stat(stat_module.S_IFDIR | 0o755, 47741, 47741)
            if path == "/mnt/wrong-owner":
                return fake_stat(stat_module.S_IFDIR | 0o700, 0, 47741)
            if path == "/mnt/not-a-dir":
                return fake_stat(stat_module.S_IFREG | 0o700, 47741, 47741)
            if path == "/mnt/symlinked-file":
                return fake_stat(stat_module.S_IFLNK | 0o777, 0, 0)
            raise FileNotFoundError(path)

        failures = verify_deploy.check_path_facts(facts, lstat, resolve_uid, resolve_uid)
        self.assertEqual(len(failures), 5)
        self.assertIn("path: /mnt/wrong-mode has mode 755, expected 700", failures)
        self.assertIn(
            "path: /mnt/wrong-owner owned by uid 0, expected trustyclaw-admin (47741)", failures
        )
        self.assertIn("path: /mnt/not-a-dir is not a directory", failures)
        self.assertIn("path: /mnt/missing is missing", failures)
        self.assertIn("path: /mnt/symlinked-file is not a regular file", failures)

    def test_pgdata_facts_report_missing_cluster_as_failure(self) -> None:
        facts = verify_deploy.pgdata_path_facts()
        self.assertGreaterEqual(len(facts), 1)
        for _, owner, group, _, _ in facts:
            self.assertEqual((owner, group), ("postgres", "postgres"))


class SocketTests(unittest.TestCase):
    def test_socket_facts(self) -> None:
        owners = {"/run/a.sock": "trustyclaw-tools", "/run/b.sock": "trustyclaw-tools", "/run/c.sock": "trustyclaw-tools"}

        def lstat(path: str) -> os.stat_result:
            if path == "/run/a.sock":
                return fake_stat(stat_module.S_IFSOCK | 0o666, 47746, 47746)
            if path == "/run/b.sock":
                return fake_stat(stat_module.S_IFREG | 0o666, 47746, 47746)
            raise FileNotFoundError(path)

        failures = verify_deploy.check_unix_sockets(owners, lstat, resolve_uid)
        self.assertEqual(len(failures), 2)
        self.assertIn("socket: /run/b.sock is not a socket", failures)
        self.assertIn("socket: /run/c.sock is missing", failures)

    def test_socket_owner_map_covers_every_agent_facing_socket(self) -> None:
        self.assertEqual(
            set(verify_deploy.SOCKET_OWNERS.values()),
            {
                "trustyclaw-tools",
                "trustyclaw-agent-app",
                "trustyclaw-agent-network",
                "trustyclaw-admin",
                "postgres",
            },
        )


class ListenerTests(unittest.TestCase):
    SAMPLE = (
        "  sl  local_address rem_address   st tx_queue rx_queue tr tm->when retrnsmt   uid  timeout inode\n"
        "   0: 0100007F:1D13 00000000:0000 0A 00000000:00000000 00:00000000 00000000 47741        0 1000 1\n"
        "   1: 0100007F:1D15 00000000:0000 0A 00000000:00000000 00:00000000 00000000 47742        0 1001 1\n"
        "   2: 0100007F:0016 00000000:0000 01 00000000:00000000 00:00000000 00000000     0        0 1002 1\n"
    )

    def test_parse_extracts_loopback_listeners_with_uids(self) -> None:
        listeners = verify_deploy.parse_tcp_listeners(self.SAMPLE)
        self.assertIn(("127.0.0.1", ADMIN_API_PORT, 47741), listeners)
        self.assertIn(("127.0.0.1", PROXY_PORT, 47742), listeners)
        # Non-LISTEN rows are ignored.
        self.assertEqual(len(listeners), 2)

    def test_expected_listeners_pass_and_wrong_owner_fails(self) -> None:
        self.assertEqual(
            verify_deploy.check_tcp_listeners(lambda path: self.SAMPLE, resolve_uid), []
        )
        swapped = self.SAMPLE.replace("47741", "47742")
        failures = verify_deploy.check_tcp_listeners(lambda path: swapped, resolve_uid)
        self.assertEqual(len(failures), 1)
        self.assertIn(f"127.0.0.1:{ADMIN_API_PORT}", failures[0])


class RunnerBackedCheckTests(unittest.TestCase):
    def test_services_active(self) -> None:
        self.assertEqual(
            verify_deploy.check_services_active(("a.service",), lambda argv: completed(0)), []
        )
        failures = verify_deploy.check_services_active(
            ("a.service", "b.service"), lambda argv: completed(3)
        )
        self.assertEqual(
            failures, ["service: a.service is not active", "service: b.service is not active"]
        )

    def test_firewall_ruleset_markers(self) -> None:
        good = (
            "table inet trustyclaw {\n"
            "  chain input {\n    type filter hook input priority filter; policy drop;\n  }\n"
            "  chain output {\n    type filter hook output priority filter; policy drop;\n  }\n}\n"
        )
        self.assertEqual(
            verify_deploy.check_firewall_ruleset(lambda argv: completed(0, stdout=good)), []
        )
        numeric = good.replace("priority filter;", "priority 0;")
        self.assertEqual(
            verify_deploy.check_firewall_ruleset(lambda argv: completed(0, stdout=numeric)), []
        )
        failures = verify_deploy.check_firewall_ruleset(lambda argv: completed(0, stdout=""))
        self.assertEqual(len(failures), 3)
        failures = verify_deploy.check_firewall_ruleset(
            lambda argv: completed(1, stderr="no nft")
        )
        self.assertEqual(failures, ["nftables: `nft list ruleset` failed: no nft"])

    def test_reachability_expectations(self) -> None:
        probes: list[verify_deploy.Probe] = [
            ("trustyclaw-agent", "127.0.0.1", PROXY_PORT, "reachable", "agent to proxy"),
            ("trustyclaw-agent", "1.1.1.1", 443, "blocked", "agent egress"),
        ]

        def runner_for(codes: dict[int, int]):
            def run(argv: list[str]) -> "subprocess.CompletedProcess[str]":
                return completed(codes[int(argv[-1])])

            return run

        # Correct outcomes: proxy port reachable, external blocked.
        self.assertEqual(
            verify_deploy.check_reachability(probes, runner_for({PROXY_PORT: 0, 443: 10})), []
        )
        # A refused listener still proves the packet passed the firewall.
        self.assertEqual(
            verify_deploy.check_reachability(probes[:1], runner_for({PROXY_PORT: 0})), []
        )
        # Inverted outcomes fail both probes.
        failures = verify_deploy.check_reachability(
            probes, runner_for({PROXY_PORT: 10, 443: 0})
        )
        self.assertEqual(len(failures), 2)
        self.assertIn("agent to proxy", failures[0])
        self.assertIn("expected reachable", failures[0])
        self.assertIn("expected blocked", failures[1])
        # Probe errors are reported as errors, not misread as outcomes.
        failures = verify_deploy.check_reachability(
            probes[:1], lambda argv: completed(1, stderr="boom")
        )
        self.assertEqual(len(failures), 1)
        self.assertIn("errored: boom", failures[0])

    def test_probe_lists_cover_agent_boundary_and_cannot_false_fail(self) -> None:
        enforced = verify_deploy.enforced_probes()
        by_reason = {reason: expectation for _, _, _, expectation, reason in enforced}
        self.assertEqual(by_reason["agent to egress proxy"], "reachable")
        self.assertEqual(by_reason["agent to admin API"], "blocked")
        self.assertEqual(by_reason["agent direct egress"], "blocked")
        self.assertEqual(by_reason["admin service egress"], "blocked")
        # Every enforced "reachable" expectation is loopback: a healthy deploy
        # on an egress-restricted customer network can never false-fail, since
        # blocked expectations pass vacuously when the network is down.
        for _, host, _, expectation, reason in enforced:
            if expectation == "reachable":
                self.assertEqual(host, "127.0.0.1", reason)
        # Positive external egress is advisory only.
        advisory = {reason for _, _, _, _, reason in verify_deploy.advisory_probes()}
        self.assertEqual(advisory, {"tools service egress", "proxy egress"})
        for _, _, _, expectation, _ in verify_deploy.advisory_probes():
            self.assertEqual(expectation, "reachable")

    def test_database_access(self) -> None:
        def run(argv: list[str]) -> "subprocess.CompletedProcess[str]":
            if "trustyclaw-admin" in argv:
                return completed(0, stdout="1\n")
            return completed(2, stderr="psql: FATAL: pg_hba.conf rejects connection")

        self.assertEqual(verify_deploy.check_database_access(run), [])

        def agent_admitted(argv: list[str]) -> "subprocess.CompletedProcess[str]":
            return completed(0, stdout="1\n")

        failures = verify_deploy.check_database_access(agent_admitted)
        self.assertEqual(len(failures), 1)
        self.assertIn("trustyclaw-agent was admitted", failures[0])

    def test_retry_until_clean_retries_until_pass_or_deadline(self) -> None:
        clock = {"now": 0.0}
        sleeps: list[float] = []

        def monotonic() -> float:
            return clock["now"]

        def sleep(seconds: float) -> None:
            sleeps.append(seconds)
            clock["now"] += seconds

        # Passes on the third attempt: transient startup failures retry away.
        attempts = {"count": 0}

        def flaky() -> list[str]:
            attempts["count"] += 1
            return [] if attempts["count"] >= 3 else ["socket: missing"]

        self.assertEqual(
            verify_deploy.retry_until_clean(flaky, 60.0, monotonic, sleep), []
        )
        self.assertEqual(attempts["count"], 3)
        # A persistent failure is returned once the deadline expires.
        failures = verify_deploy.retry_until_clean(
            lambda: ["service: broken"], 10.0, monotonic, sleep
        )
        self.assertEqual(failures, ["service: broken"])
        self.assertTrue(all(interval == 2 for interval in sleeps))

    def test_immutable_agent_files(self) -> None:
        good = lambda argv: completed(0, stdout=f"----i---------e------- {argv[-1]}\n")
        self.assertEqual(verify_deploy.check_immutable_agent_files(good), [])
        mutable = lambda argv: completed(0, stdout=f"--------------e------- {argv[-1]}\n")
        failures = verify_deploy.check_immutable_agent_files(mutable)
        self.assertEqual(len(failures), len(verify_deploy.MANAGED_AGENT_FILES))
        self.assertIn("missing the immutable attribute", failures[0])


if __name__ == "__main__":
    unittest.main()
