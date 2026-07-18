"""SSH delivery for TrustyClaw host provisioning.

The artifacts themselves (payload, bootstrap script, runtime code archive)
are rendered by ``host.bootstrap.render``, shared with the GitHub delivery;
this module owns only the SSH transport: the single-use deploy key, copying
the artifacts to the instance, and running bootstrap over the connection.
"""

from __future__ import annotations

from pathlib import Path
import subprocess
import sys
import time
from typing import Any

from host.bootstrap.render import _write_runtime_code_archive
from host.config import ConfigError
from host.cli.lifecycle_constants import SSH_USER, SSH_WAIT_ATTEMPTS, SSH_WAIT_SECONDS
from host.cli.lifecycle_logging import _log


def _generate_deploy_key(workdir: Path) -> Path:
    key_path = workdir / "deploy_key"
    subprocess.run(
        ["ssh-keygen", "-t", "ed25519", "-N", "", "-q", "-C", "trustyclaw-deploy", "-f", str(key_path)],
        check=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    return key_path


def _provision_over_ssh(
    public_dns: str,
    deploy_key: Path,
    workdir: Path,
) -> None:
    """Push the local checkout's code archive to the instance and hand off to
    host.bootstrap.self_provision there, exactly like the GitHub delivery
    after its fetch. The provisioning payload was already staged by user
    data; only code delivery differs between the deliveries."""
    code_path = workdir / "trustyclaw-host-code.tar.gz"
    _write_runtime_code_archive(code_path)

    ssh = [
        "ssh",
        "-i",
        str(deploy_key),
        "-o",
        "StrictHostKeyChecking=accept-new",
        "-o",
        f"UserKnownHostsFile={workdir / 'known_hosts'}",
        "-o",
        "ConnectTimeout=10",
        "-o",
        "ServerAliveInterval=15",
    ]
    target = f"{SSH_USER}@{public_dns}"
    _log("waiting for SSH on the new instance (it is still booting)")
    _wait_for_ssh(ssh, target)
    _log("SSH is up; copying runtime code and bootstrap script to the host")
    subprocess.run(
        ["scp", *ssh[1:], str(code_path), f"{target}:/tmp/"],
        check=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    _log("running bootstrap: apt + security updates, Postgres + schema migrations, npm,")
    _log("agent CLIs, services. This takes several minutes; the host's own output streams below.")
    print("-" * 70, file=sys.stderr, flush=True)
    _run_bootstrap(ssh, target)
    print("-" * 70, file=sys.stderr, flush=True)


# The delivered archive becomes the checkout self_provision renders and
# bootstraps from; fixed paths only, nothing user-controlled is interpolated.
_REMOTE_SELF_PROVISION = (
    "sudo bash -c '"
    "rm -rf /tmp/trustyclaw-checkout && mkdir -p /tmp/trustyclaw-checkout && "
    "tar -xzf /tmp/trustyclaw-host-code.tar.gz -C /tmp/trustyclaw-checkout && "
    "PYTHONPATH=/tmp/trustyclaw-checkout python3 -m host.bootstrap.self_provision "
    "--payload /tmp/trustyclaw_payload.json --checkout /tmp/trustyclaw-checkout"
    "'"
)


def _run_bootstrap(ssh: list[str], target: str) -> None:
    process = subprocess.Popen(
        [*ssh, target, _REMOTE_SELF_PROVISION],
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
    )
    if process.stdout is None:
        raise ConfigError("could not read bootstrap output")
    for line in process.stdout:
        sys.stderr.write(line)
        sys.stderr.flush()
    returncode = process.wait()
    if returncode != 0:
        raise ConfigError(f"bootstrap failed on the host (exit {returncode}); see the output above")


def _wait_for_ssh(ssh: list[str], target: str) -> None:
    for attempt in range(SSH_WAIT_ATTEMPTS):
        result = subprocess.run(
            [*ssh, target, "true"],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        if result.returncode == 0:
            return
        if attempt % 3 == 0:
            _log(f"  still waiting for SSH ({(attempt + 1) * SSH_WAIT_SECONDS}s elapsed)")
        time.sleep(SSH_WAIT_SECONDS)
    raise ConfigError(f"could not reach {target} over SSH after {SSH_WAIT_ATTEMPTS * SSH_WAIT_SECONDS} seconds")
