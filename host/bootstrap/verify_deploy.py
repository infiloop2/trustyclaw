"""End-of-deploy verification, run as root by bootstrap after services start.

Bootstrap applies configuration; this module independently re-checks the
resulting system state and fails the deploy listing every mismatch at once:

- service accounts exist with their pinned uids/gids (host.constants),
- managed paths carry the expected owner, group, and mode,
- the runtime services are active and each Unix socket exists with the
  owner and mode its one serving package binds it with,
- the two loopback TCP listeners are owned by the expected service uids,
- the nftables ruleset is loaded fail-closed, and live probes confirm the
  permission boundary in both directions: allowed paths connect (or are
  refused by a listener, which proves the packet passed the firewall) and
  denied paths are dropped,
- Postgres peer auth admits the admin role and rejects the agent,
- the managed agent-home files are immutable.

The socket paths and account ids come from host.constants — the same
definitions the services themselves use — while the path expectations are
deliberately restated here rather than shared with bootstrap, so a bootstrap
bug cannot silently verify itself.
"""

from __future__ import annotations

import argparse
import glob
import grp
import os
import pwd
import stat as stat_module
from pathlib import Path
import subprocess
import sys
import time
from typing import Callable

from host.constants import (
    ADMIN_API_PORT,
    AGENT_APP_SOCKET_PATH,
    AGENT_NETWORK_SOCKET_PATH,
    APP_BACKEND_ADMIN_SOCKET_PATH,
    PROXY_PORT,
    SERVICE_ACCOUNTS,
    TOOLS_SOCKET_PATH,
)
from host.runtime.core import app_platform

Runner = Callable[[list[str]], "subprocess.CompletedProcess[str]"]

POSTGRES_SOCKET = "/var/run/postgresql/.s.PGSQL.5432"
# A stable anycast address for external probes. Blocked expectations against
# it can never false-fail (a down network also times out); positive
# reachability to it is only ever advisory, because an egress-restricted
# customer network may legitimately block it on a healthy deploy.
EXTERNAL_PROBE_HOST = "1.1.1.1"
PROBE_TIMEOUT_SECONDS = 3

CORE_UNITS = (
    "trustyclaw-postgres.service",
    "trustyclaw-network-proxy.service",
    "trustyclaw-tools.service",
    "trustyclaw-agent-network.service",
    "trustyclaw-agent-app.service",
    "trustyclaw-admin-api.service",
)

MANAGED_AGENT_FILES = (
    "/mnt/trustyclaw-agent/agent-home/AGENTS.md",
    "/mnt/trustyclaw-agent/agent-home/CLAUDE.md",
    "/mnt/trustyclaw-agent/agent-home/.codex/config.toml",
    "/mnt/trustyclaw-agent/agent-home/.claude/settings.json",
    "/mnt/trustyclaw-agent/agent-home/.hermes/config.yaml",
    "/mnt/trustyclaw-agent/agent-home/.hermes/.env",
)

# path, owner, group, mode, is_directory
PathFact = tuple[str, str, str, int, bool]

PATH_FACTS: tuple[PathFact, ...] = (
    ("/mnt/trustyclaw-admin", "root", "root", 0o711, True),
    ("/mnt/trustyclaw-agent", "root", "root", 0o711, True),
    ("/mnt/trustyclaw-admin/postgres", "root", "root", 0o711, True),
    ("/mnt/trustyclaw-admin/admin-state", "trustyclaw-admin", "trustyclaw-admin", 0o700, True),
    ("/mnt/trustyclaw-admin/admin-home", "trustyclaw-admin", "trustyclaw-admin", 0o700, True),
    ("/mnt/trustyclaw-agent/agent-home", "trustyclaw-agent", "trustyclaw-agent", 0o700, True),
    ("/mnt/trustyclaw-agent/agent-home/.codex", "trustyclaw-agent", "trustyclaw-agent", 0o700, True),
    ("/mnt/trustyclaw-agent/agent-home/.claude", "trustyclaw-agent", "trustyclaw-agent", 0o700, True),
    ("/mnt/trustyclaw-agent/agent-home/.hermes", "trustyclaw-agent", "trustyclaw-agent", 0o700, True),
    ("/mnt/trustyclaw-admin/proxy-state", "trustyclaw-proxy", "trustyclaw-proxy", 0o700, True),
    (
        "/mnt/trustyclaw-admin/proxy-state/generated-certs",
        "trustyclaw-proxy",
        "trustyclaw-proxy",
        0o700,
        True,
    ),
    (
        "/mnt/trustyclaw-admin/proxy-state/network_proxy_ca.key",
        "trustyclaw-proxy",
        "trustyclaw-proxy",
        0o600,
        False,
    ),
    (
        "/mnt/trustyclaw-admin/proxy-state/network_proxy_ca.crt",
        "trustyclaw-proxy",
        "trustyclaw-proxy",
        0o644,
        False,
    ),
    ("/mnt/trustyclaw-admin/tools-state", "trustyclaw-tools", "trustyclaw-tools", 0o700, True),
    (
        "/mnt/trustyclaw-admin/tools-state/assets",
        "trustyclaw-tools",
        "trustyclaw-tools",
        0o700,
        True,
    ),
    ("/opt/trustyclaw-host", "root", "root", 0o755, True),
    ("/opt/trustyclaw-host/VERSION", "root", "root", 0o644, False),
    ("/usr/local/lib/trustyclaw-host", "root", "root", 0o755, True),
    ("/usr/local/bin/gh", "root", "root", 0o755, False),
    ("/etc/sudoers.d/trustyclaw-host", "root", "root", 0o440, False),
    ("/etc/codex/requirements.toml", "root", "root", 0o644, False),
    ("/etc/codex/managed_config.toml", "root", "root", 0o644, False),
    ("/mnt/trustyclaw-agent/agent-home/AGENTS.md", "root", "root", 0o644, False),
    ("/mnt/trustyclaw-agent/agent-home/CLAUDE.md", "root", "root", 0o644, False),
    ("/mnt/trustyclaw-agent/agent-home/.codex/config.toml", "root", "root", 0o644, False),
    ("/mnt/trustyclaw-agent/agent-home/.claude/settings.json", "root", "root", 0o644, False),
    ("/mnt/trustyclaw-agent/agent-home/.hermes/config.yaml", "root", "root", 0o644, False),
    ("/mnt/trustyclaw-agent/agent-home/.hermes/.env", "root", "root", 0o644, False),
)

CLOUDFLARE_PATH_FACTS: tuple[PathFact, ...] = (
    ("/etc/trustyclaw/cloudflared.token", "root", "cloudflared", 0o640, False),
    ("/etc/trustyclaw/cloudflare_hostname", "root", "root", 0o644, False),
)

# socket path -> owning service account
SOCKET_OWNERS = {
    TOOLS_SOCKET_PATH: "trustyclaw-tools",
    AGENT_APP_SOCKET_PATH: "trustyclaw-agent-app",
    AGENT_NETWORK_SOCKET_PATH: "trustyclaw-agent-network",
    APP_BACKEND_ADMIN_SOCKET_PATH: "trustyclaw-admin",
    POSTGRES_SOCKET: "postgres",
}


def _run(argv: list[str]) -> "subprocess.CompletedProcess[str]":
    return subprocess.run(argv, capture_output=True, text=True, timeout=60)


def expected_accounts() -> dict[str, int]:
    """Pinned core accounts plus the per-app accounts derived from host_slot."""
    accounts = dict(SERVICE_ACCOUNTS)
    for app in app_platform.installed_apps():
        accounts[app.linux_user] = app.allocation.uid
    return accounts


def check_service_accounts(accounts: dict[str, int]) -> list[str]:
    failures = []
    for name, uid in sorted(accounts.items()):
        try:
            passwd = pwd.getpwnam(name)
        except KeyError:
            failures.append(f"account: user {name} does not exist")
            continue
        if passwd.pw_uid != uid:
            failures.append(f"account: user {name} has uid {passwd.pw_uid}, expected {uid}")
        if passwd.pw_gid != uid:
            failures.append(f"account: user {name} has gid {passwd.pw_gid}, expected {uid}")
        try:
            group = grp.getgrnam(name)
        except KeyError:
            failures.append(f"account: group {name} does not exist")
            continue
        if group.gr_gid != uid:
            failures.append(f"account: group {name} has gid {group.gr_gid}, expected {uid}")
    return failures


def pgdata_path_facts() -> list[PathFact]:
    """The versioned data directory is discovered, not assumed, so a future
    pg_upgrade does not silently skip these checks."""
    facts: list[PathFact] = []
    for pgdata in sorted(glob.glob("/mnt/trustyclaw-admin/postgres/*/main")):
        facts.append((pgdata, "postgres", "postgres", 0o700, True))
        facts.append((f"{pgdata}/postgresql.conf", "postgres", "postgres", 0o600, False))
        facts.append((f"{pgdata}/pg_hba.conf", "postgres", "postgres", 0o600, False))
    if not facts:
        facts.append(("/mnt/trustyclaw-admin/postgres/<major>/main", "postgres", "postgres", 0o700, True))
    return facts


def check_path_facts(
    facts: tuple[PathFact, ...] | list[PathFact],
    lstat: Callable[[str], os.stat_result] = os.lstat,
    resolve_uid: Callable[[str], int] = lambda name: pwd.getpwnam(name).pw_uid,
    resolve_gid: Callable[[str], int] = lambda name: grp.getgrnam(name).gr_gid,
) -> list[str]:
    failures = []
    for path, owner, group, mode, is_directory in facts:
        try:
            info = lstat(path)
        except (FileNotFoundError, NotADirectoryError):
            failures.append(f"path: {path} is missing")
            continue
        if is_directory and not stat_module.S_ISDIR(info.st_mode):
            failures.append(f"path: {path} is not a directory")
            continue
        if not is_directory and not stat_module.S_ISREG(info.st_mode):
            failures.append(f"path: {path} is not a regular file")
            continue
        actual_mode = stat_module.S_IMODE(info.st_mode)
        if actual_mode != mode:
            failures.append(f"path: {path} has mode {actual_mode:o}, expected {mode:o}")
        expected_uid = resolve_uid(owner)
        expected_gid = resolve_gid(group)
        if info.st_uid != expected_uid:
            failures.append(f"path: {path} owned by uid {info.st_uid}, expected {owner} ({expected_uid})")
        if info.st_gid != expected_gid:
            failures.append(f"path: {path} group is gid {info.st_gid}, expected {group} ({expected_gid})")
    return failures


def check_unix_sockets(
    owners: dict[str, str],
    lstat: Callable[[str], os.stat_result] = os.lstat,
    resolve_uid: Callable[[str], int] = lambda name: pwd.getpwnam(name).pw_uid,
) -> list[str]:
    failures = []
    for path, owner in sorted(owners.items()):
        try:
            info = lstat(path)
        except FileNotFoundError:
            failures.append(f"socket: {path} is missing")
            continue
        if not stat_module.S_ISSOCK(info.st_mode):
            failures.append(f"socket: {path} is not a socket")
            continue
        expected_uid = resolve_uid(owner)
        if info.st_uid != expected_uid:
            failures.append(f"socket: {path} owned by uid {info.st_uid}, expected {owner} ({expected_uid})")
    return failures


def parse_tcp_listeners(proc_net_tcp: str) -> set[tuple[str, int, int]]:
    """(ip, port, uid) triples for LISTEN sockets in /proc/net/tcp format."""
    listeners = set()
    for line in proc_net_tcp.splitlines()[1:]:
        fields = line.split()
        if len(fields) < 8 or fields[3] != "0A":
            continue
        address, _, port_hex = fields[1].partition(":")
        octets = [str(int(address[index : index + 2], 16)) for index in (6, 4, 2, 0)]
        listeners.add((".".join(octets), int(port_hex, 16), int(fields[7])))
    return listeners


def check_tcp_listeners(
    read_text: Callable[[str], str] = lambda path: Path(path).read_text(),
    resolve_uid: Callable[[str], int] = lambda name: pwd.getpwnam(name).pw_uid,
) -> list[str]:
    failures = []
    listeners = parse_tcp_listeners(read_text("/proc/net/tcp"))
    for port, owner in ((ADMIN_API_PORT, "trustyclaw-admin"), (PROXY_PORT, "trustyclaw-proxy")):
        uid = resolve_uid(owner)
        if ("127.0.0.1", port, uid) not in listeners:
            matches = sorted(entry for entry in listeners if entry[1] == port)
            failures.append(
                f"listener: expected 127.0.0.1:{port} listening as {owner} ({uid}); found {matches or 'nothing'}"
            )
    return failures


def check_services_active(units: tuple[str, ...], run: Runner = _run) -> list[str]:
    failures = []
    for unit in units:
        result = run(["systemctl", "is-active", "--quiet", unit])
        if result.returncode != 0:
            failures.append(f"service: {unit} is not active")
    return failures


def check_firewall_ruleset(run: Runner = _run) -> list[str]:
    result = run(["nft", "list", "ruleset"])
    if result.returncode != 0:
        return [f"nftables: `nft list ruleset` failed: {result.stderr.strip()}"]
    ruleset = result.stdout
    failures = []
    if "table inet trustyclaw" not in ruleset:
        failures.append("nftables: table inet trustyclaw is not loaded")
    for marker in (
        "type filter hook input priority filter; policy drop;",
        "type filter hook output priority filter; policy drop;",
    ):
        # nft prints the canonical form; accept the numeric form too so an
        # nft output-format change degrades to a probe-only check, not a
        # false deploy failure.
        numeric = marker.replace("priority filter;", "priority 0;")
        if marker not in ruleset and numeric not in ruleset:
            failures.append(f"nftables: missing fail-closed hook: {marker!r}")
    return failures


# who, target host, port, expectation ("reachable" | "blocked"), reason
Probe = tuple[str, str, int, str, str]

_PROBE_SNIPPET = """
import errno, socket, sys
target = (sys.argv[1], int(sys.argv[2]))
sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
sock.settimeout({timeout})
try:
    sock.connect(target)
except socket.timeout:
    sys.exit(10)
except OSError as exc:
    if exc.errno == errno.ECONNREFUSED:
        sys.exit(0)
    if exc.errno in (errno.EPERM, errno.ENETUNREACH, errno.EHOSTUNREACH):
        sys.exit(10)
    print(exc, file=sys.stderr)
    sys.exit(1)
sys.exit(0)
""".format(timeout=PROBE_TIMEOUT_SECONDS)


def enforced_probes() -> list[Probe]:
    """Probes that fail the deploy. Every "blocked" expectation is safe against
    environment trouble by construction: blocked means the connection timed
    out, and a down or filtered network also times out, so a healthy deploy
    can never false-fail here. The one "reachable" expectation is loopback."""
    return [
        # The agent's entire network world is the loopback proxy port.
        ("trustyclaw-agent", "127.0.0.1", PROXY_PORT, "reachable", "agent to egress proxy"),
        ("trustyclaw-agent", "127.0.0.1", ADMIN_API_PORT, "blocked", "agent to admin API"),
        ("trustyclaw-agent", EXTERNAL_PROBE_HOST, 443, "blocked", "agent direct egress"),
        ("trustyclaw-agent", EXTERNAL_PROBE_HOST, 53, "blocked", "agent direct DNS"),
        ("trustyclaw-admin", EXTERNAL_PROBE_HOST, 443, "blocked", "admin service egress"),
        ("trustyclaw-agent-app", EXTERNAL_PROBE_HOST, 443, "blocked", "agent-app egress"),
        ("trustyclaw-agent-network", EXTERNAL_PROBE_HOST, 443, "blocked", "agent-network egress"),
        (
            "trustyclaw-agent-network",
            "127.0.0.1",
            PROXY_PORT,
            "blocked",
            "agent-network to egress proxy (loopback drop)",
        ),
    ]


def advisory_probes() -> list[Probe]:
    """Positive external egress is liveness, not the trust boundary: an
    egress-restricted customer network may legitimately block the probe
    target while the deploy is healthy, so these warn instead of failing."""
    return [
        ("trustyclaw-tools", EXTERNAL_PROBE_HOST, 443, "reachable", "tools service egress"),
        ("trustyclaw-proxy", EXTERNAL_PROBE_HOST, 443, "reachable", "proxy egress"),
    ]


def check_reachability(probes: list[Probe], run: Runner = _run) -> list[str]:
    failures = []
    for user, host, port, expectation, reason in probes:
        result = run(
            ["runuser", "-u", user, "--", "python3", "-c", _PROBE_SNIPPET, host, str(port)]
        )
        if result.returncode == 0:
            outcome = "reachable"
        elif result.returncode == 10:
            outcome = "blocked"
        else:
            failures.append(
                f"probe: {reason} ({user} -> {host}:{port}) errored: {result.stderr.strip()}"
            )
            continue
        if outcome != expectation:
            failures.append(
                f"probe: {reason} ({user} -> {host}:{port}) is {outcome}, expected {expectation}"
            )
    return failures


def check_database_access(run: Runner = _run) -> list[str]:
    """Peer auth to and fro: the admin role connects, the agent has no path."""
    failures = []
    admin = run(
        ["runuser", "-u", "trustyclaw-admin", "--", "psql", "-d", "trustyclaw_admin", "-tAc", "SELECT 1"]
    )
    if admin.returncode != 0 or admin.stdout.strip() != "1":
        failures.append(f"database: trustyclaw-admin cannot query trustyclaw_admin: {admin.stderr.strip()}")
    agent = run(
        ["runuser", "-u", "trustyclaw-agent", "--", "psql", "-d", "trustyclaw_admin", "-tAc", "SELECT 1"]
    )
    if agent.returncode == 0:
        failures.append("database: trustyclaw-agent was admitted to trustyclaw_admin; pg_hba must reject it")
    return failures


def check_immutable_agent_files(run: Runner = _run) -> list[str]:
    failures = []
    for path in MANAGED_AGENT_FILES:
        result = run(["lsattr", "-d", path])
        if result.returncode != 0:
            failures.append(f"immutable: lsattr failed for {path}: {result.stderr.strip()}")
            continue
        flags = result.stdout.split()[0] if result.stdout.split() else ""
        if "i" not in flags:
            failures.append(f"immutable: {path} is missing the immutable attribute (flags {flags!r})")
    return failures


def retry_until_clean(
    check: Callable[[], list[str]],
    timeout_seconds: float = 120.0,
    monotonic: Callable[[], float] = time.monotonic,
    sleep: Callable[[float], None] = time.sleep,
) -> list[str]:
    """Re-run a check until it passes or the deadline expires, returning the
    last failures. Bootstrap runs verification right after `systemctl start`
    returns, and a service binds its socket a moment after systemd reports it
    started, so point-in-time checks against service state would flake on a
    healthy deploy. Static facts (accounts, paths) are checked once."""
    deadline = monotonic() + timeout_seconds
    while True:
        failures = check()
        if not failures or monotonic() >= deadline:
            return failures
        sleep(2)


def run_all_checks(cloudflare_enabled: bool, run: Runner = _run) -> list[str]:
    path_facts: list[PathFact] = list(PATH_FACTS) + pgdata_path_facts()
    if cloudflare_enabled:
        path_facts.extend(CLOUDFLARE_PATH_FACTS)
    units = CORE_UNITS + (("trustyclaw-cloudflared.service",) if cloudflare_enabled else ())
    failures: list[str] = []
    failures += check_service_accounts(expected_accounts())
    failures += check_path_facts(path_facts)
    failures += retry_until_clean(
        lambda: check_unix_sockets(SOCKET_OWNERS)
        + check_tcp_listeners()
        + check_services_active(units, run)
    )
    failures += check_firewall_ruleset(run)
    failures += retry_until_clean(lambda: check_reachability(enforced_probes(), run))
    failures += check_database_access(run)
    failures += check_immutable_agent_files(run)
    return failures


def advisory_warnings(run: Runner = _run) -> list[str]:
    return check_reachability(advisory_probes(), run)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--cloudflare",
        choices=("yes", "no"),
        required=True,
        help="whether a cloudflare_access operator connection was configured",
    )
    args = parser.parse_args(argv)
    failures = run_all_checks(cloudflare_enabled=args.cloudflare == "yes")
    for warning in advisory_warnings():
        print(f"warning (deploy continues): {warning}", file=sys.stderr)
    if failures:
        print(f"deploy verification failed with {len(failures)} finding(s):", file=sys.stderr)
        for failure in failures:
            print(f"  - {failure}", file=sys.stderr)
        return 1
    print("deploy verification passed: accounts, paths, sockets, listeners, services, firewall, database")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
