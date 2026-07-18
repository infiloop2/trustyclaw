"""CLI orchestration for host lifecycle commands.

Provisioning artifacts (payload, bootstrap script, runtime code archive) are
rendered by ``host.bootstrap.render`` and delivered one of two ways:

1. **SSH delivery** (default): EC2 user data creates the
   ``trustyclaw-operator`` account with a single-use deploy key and stages the
   provisioning payload, then the CLI copies the local checkout's runtime code
   archive to the instance over SSH, where the same
   ``host.bootstrap.self_provision`` entry the GitHub delivery uses renders
   and runs bootstrap to completion. Bootstrap removes the
   deploy key when it finishes; the CLI's only step after bootstrap is
   closing the provisioning SSH ingress when the operator endpoints do not
   keep it. This delivery ships whatever is in the local checkout, so it also
   serves development against unpublished code.
2. **GitHub delivery** (``--bootstrap-from-github COMMIT_SHA``): EC2 user
   data carries the provisioning payload and a fetch script; the instance
   fetches the pinned commit of the fixed public repository
   (``host.constants.PUBLIC_GITHUB_REPOSITORY``) and runs the same bootstrap
   on itself (``host.bootstrap.self_provision``). The CLI first reads the
   pinned commit's ``VERSION`` from GitHub; that version, not the local
   checkout's, is the operation's target, and the CLI asks for confirmation
   before proceeding (non-interactive callers pipe the confirmation into
   stdin). Any GitHub failure or a declined confirmation aborts before
   anything in AWS is touched. No deploy key
   exists and the CLI returns once the instance is launched with its volumes
   attached; bootstrap outcome is observable through the operator endpoints
   coming up.

Security-group access is derived the same way on both deliveries, before
launch: deploy and reconfigure derive it from the input operator connections,
and upgrade and recover reapply the previous converged security-group state.
The GitHub delivery launches with that state and never changes it; the SSH
delivery additionally opens SSH at launch for the deploy key.

The CLI never handles the admin password. Deploy and reconfigure require
``--admin-password-sha256`` with the SHA-256 hex digest of the operator's
chosen password; the host stores only that hash, so neither the CLI process,
its result files, nor anything on the instance ever contains the cleartext.
"""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile
import urllib.request

from host.bootstrap.render import _bootstrap_payload, _render_github_user_data, _render_ssh_user_data
from host.config import (
    ConfigError,
    InputConfig,
    RuntimeOperatorConnection,
    build_input_config,
    build_operator_connections,
    public_operator_connections,
)
from host.constants import ADMIN_API_PORT, OPERATOR_TUNNEL_TOKEN_ENV_NAME, PUBLIC_GITHUB_REPOSITORY
from host.cli.lifecycle_aws import (
    _attach_storage_volumes,
    _aws,
    _aws_env,
    _default_network,
    _ensure_security_group,
    _ensure_storage_volumes,
    _existing_storage_roles,
    _existing_storage_volume_availability_zone,
    _find_available_storage_volume,
    _find_existing_instances,
    _find_storage_volume,
    _launch_instance,
    _preserve_attached_volume_on_instance_termination,
    _preserve_existing_storage_volumes_on_instance_termination,
    _security_group_access_state,
    _set_security_group_ssh_ingress,
    _subnet_has_public_ipv4_route,
    _tag_spec,
    _terminate_instances,
    _ubuntu_ami,
    _volume_tag_spec,
    _wait_for_instance,
)
from host.cli.lifecycle_bootstrap import _generate_deploy_key, _provision_over_ssh
from host.cli.lifecycle_checks import _check_existing_version_hints, _validate_command_preflight, _version_hint_error
from host.cli.lifecycle_constants import SSH_USER
from host.cli.lifecycle_logging import _log
from host.cli.lifecycle_types import LifecycleCommand
from host.version import compare_versions, parse_version, repo_version


_COMMIT_SHA_RE = re.compile(r"^[0-9a-f]{40}$")
# First release carrying host.bootstrap.self_provision; older pins would pass
# the VERSION preflight and then fail bootstrap on the host, after the
# instance and volumes were already mutated.
_MIN_GITHUB_DELIVERY_VERSION = "0.35.0"
_SHA256_HEX_RE = re.compile(r"^[0-9a-f]{64}$")
# SHA-256 of the empty string: a syntactically valid digest that installs an
# empty admin password. A caller hashing an unset env var lands exactly here.
_EMPTY_PASSWORD_SHA256 = "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855"


def _parse_args(mode: str, argv: list[str] | None) -> LifecycleCommand:
    if mode not in {"deploy", "upgrade", "recover", "reconfigure"}:
        raise ValueError(f"unsupported lifecycle mode: {mode}")
    descriptions = {
        "deploy": "Create a new TrustyClaw host with no existing instance or data volumes",
        "upgrade": "Upgrade preserved TrustyClaw state without changing admin password or operator access",
        "recover": "Create a replacement host from preserved data volumes and existing operator access",
        "reconfigure": "Replace operator access and refresh the admin password for preserved TrustyClaw state",
    }
    parser = argparse.ArgumentParser(
        prog=f"python3 -m host.cli.{mode}",
        description=descriptions[mode],
    )
    parser.add_argument(
        "--agent-name",
        required=True,
        help="Stable host name: 1-50 characters of letters, numbers, '-' or '_'.",
    )
    parser.add_argument(
        "--bootstrap-from-github",
        nargs="?",
        const="",
        metavar="COMMIT_SHA",
        help=(
            f"Provision the instance from a pinned {PUBLIC_GITHUB_REPOSITORY} commit via "
            "EC2 user data instead of pushing the local checkout over SSH; without a value, "
            "the latest main commit is pinned. The CLI reads the commit's VERSION from GitHub "
            "first and asks for confirmation. The command returns once the instance is "
            "launched with its volumes attached; bootstrap completes on the host."
        ),
    )
    if mode in {"deploy", "reconfigure"}:
        parser.add_argument(
            "--operator-ssh-public-key",
            metavar="OPENSSH_PUBLIC_KEY",
            help=(
                "Operator SSH endpoint: the ssh-ed25519 or ssh-rsa public key content to "
                "install. At least one operator endpoint is required."
            ),
        )
        parser.add_argument(
            "--operator-cloudflare-hostname",
            metavar="HOSTNAME",
            help=(
                "Operator Cloudflare Access endpoint: the exact protected hostname. The tunnel "
                f"token is read from {OPERATOR_TUNNEL_TOKEN_ENV_NAME}. At least one operator "
                "endpoint is required."
            ),
        )
        parser.add_argument(
            "--admin-password-sha256",
            required=True,
            metavar="HEX_DIGEST",
            help=(
                "SHA-256 hex digest of the admin password to install. The host stores only "
                "this hash and the CLI never sees the password itself. Compute it locally, "
                "for example: printf %%s 'your-password' | sha256sum"
            ),
        )
    if mode == "recover":
        parser.add_argument(
            "--allow-upgrade",
            action="store_true",
            help="Allow recovery to also advance preserved state to the target VERSION.",
        )
    args = parser.parse_args(argv)
    # Only deploy and reconfigure define the flag; upgrade, recover, start,
    # and stop never install a password and reject it as an unknown argument.
    admin_password_sha256: str | None = None
    if mode in {"deploy", "reconfigure"}:
        digest = str(args.admin_password_sha256).strip().lower()
        if not _SHA256_HEX_RE.fullmatch(digest):
            parser.error("--admin-password-sha256 must be a 64-character hex SHA-256 digest")
        if digest == _EMPTY_PASSWORD_SHA256:
            parser.error(
                "--admin-password-sha256 is the SHA-256 of an empty password; set a non-empty "
                "admin password (a caller hashing an unset environment variable lands here)"
            )
        admin_password_sha256 = digest
    # None: SSH delivery. "": GitHub delivery pinning the latest main commit.
    github_commit_sha = args.bootstrap_from_github
    if github_commit_sha is not None:
        github_commit_sha = github_commit_sha.strip()
        if github_commit_sha and not _COMMIT_SHA_RE.fullmatch(github_commit_sha):
            parser.error("--bootstrap-from-github must be a full 40-character lowercase hex commit sha")
    return LifecycleCommand(
        mode=mode,
        agent_name=args.agent_name,
        admin_password_sha256=admin_password_sha256,
        allow_upgrade=bool(getattr(args, "allow_upgrade", False)),
        github_commit_sha=github_commit_sha,
        operator_ssh_public_key=getattr(args, "operator_ssh_public_key", None),
        operator_cloudflare_hostname=getattr(args, "operator_cloudflare_hostname", None),
    )


def main_for_mode(mode: str, argv: list[str] | None = None) -> int:
    command = _parse_args(mode, argv)

    try:
        config = build_input_config(command.agent_name, _aws_region_from_env())
        github_commit_sha = command.github_commit_sha
        if github_commit_sha is not None:
            # The pinned commit is the code that will run, so its VERSION is
            # the operation's target; the local checkout only orchestrates.
            github_commit_sha, target_version = _resolve_github_pin(github_commit_sha)
        else:
            target_version = repo_version()
        admin_password_sha256 = command.admin_password_sha256
        replacement_operator_connections: tuple[RuntimeOperatorConnection, ...] | None = None
        if command.mode in {"deploy", "reconfigure"}:
            replacement_operator_connections = build_operator_connections(
                command.operator_ssh_public_key,
                command.operator_cloudflare_hostname,
                os.environ.get(OPERATOR_TUNNEL_TOKEN_ENV_NAME)
                if command.operator_cloudflare_hostname is not None
                else None,
            )
        aws_env = _aws_env(config)
        _log(
            f"region {config.aws_region}; preparing {command.mode} for "
            f"'{config.agent_name}' at TrustyClaw {target_version}"
        )
        preferred_availability_zone = _existing_storage_volume_availability_zone(config, aws_env)
        existing = _find_existing_instances(config, aws_env)
        storage_roles = _existing_storage_roles(config, aws_env)
        _validate_command_preflight(command, config, existing, storage_roles)
        _check_existing_version_hints(command, config, aws_env, existing, target_version)

        network = _default_network(
            config,
            aws_env,
            preferred_availability_zone=preferred_availability_zone,
        )
        vpc_id, _subnet_id, availability_zone = network
        ssh_ingress, cloudflare_egress = _launch_access_state(
            config, aws_env, vpc_id, replacement_operator_connections
        )
        if existing:
            _preserve_existing_storage_volumes_on_instance_termination(config, aws_env, existing)
            _log(f"terminating existing instance(s): {', '.join(existing)}")
            _terminate_instances(existing, aws_env)
        created_storage_volumes: list[str] = []
        instance_id: str | None = None
        try:
            storage_volumes = _ensure_storage_volumes(
                config,
                aws_env,
                availability_zone=availability_zone,
                wait_for_detach=bool(existing),
                created_storage_volumes=created_storage_volumes,
            )
            with tempfile.TemporaryDirectory() as workdir_name:
                workdir = Path(workdir_name)
                # Both deliveries stage the same payload through user data;
                # they differ only in code delivery.
                payload = _bootstrap_payload(
                    config,
                    admin_password_sha256,
                    replacement_operator_connections,
                    storage_volumes,
                    mode=command.mode,
                    target_version=target_version,
                    allow_upgrade=command.allow_upgrade,
                )
                deploy_key: Path | None = None
                if github_commit_sha is not None:
                    user_data = _render_github_user_data(payload, github_commit_sha)
                else:
                    deploy_key = _generate_deploy_key(workdir)
                    user_data = _render_ssh_user_data(
                        payload, deploy_key.with_suffix(".pub").read_text().strip()
                    )
                _log("launching EC2 instance")
                instance_id, security_group_id = _launch_instance(
                    config,
                    user_data,
                    workdir,
                    aws_env,
                    target_version=target_version,
                    network=network,
                    ssh_ingress=ssh_ingress or deploy_key is not None,
                    cloudflare_egress=cloudflare_egress,
                )
                _log(f"launched {instance_id}; waiting for it to reach 'running'")
                instance = _wait_for_instance(instance_id, aws_env)
                public_dns = instance["PublicDnsName"]
                _log(f"instance running at {public_dns}")
                _attach_storage_volumes(aws_env, instance_id=instance_id, volumes=storage_volumes)
                if deploy_key is not None:
                    _provision_over_ssh(public_dns, deploy_key, workdir)
                    # The one step after bootstrap: the deploy key needed SSH
                    # open during provisioning; close it when the operator
                    # endpoints do not keep it.
                    if not ssh_ingress:
                        _set_security_group_ssh_ingress(aws_env, security_group_id, enabled=False)
                    _log("provisioning complete")
                else:
                    _log(
                        "instance launched with volumes attached; bootstrap continues on the host "
                        "from the pinned commit. Operator endpoints come up when it succeeds."
                    )
        except BaseException:
            # A failure here can leave a running instance with temporary
            # provisioning access still open. Tear it down so a retry starts
            # clean and nothing is exposed. The same invariant holds on the
            # GitHub delivery after the CLI returns: a host-side provisioning
            # failure shuts the instance down, which terminates it.
            if instance_id is not None:
                _log(f"provisioning failed; terminating {instance_id} to avoid a half-provisioned, exposed host")
                try:
                    _terminate_instances([instance_id], aws_env)
                except Exception as cleanup_exc:  # noqa: BLE001 — best-effort cleanup
                    _log(f"warning: could not terminate {instance_id}: {cleanup_exc}")
            if created_storage_volumes:
                _log(
                    "provisioning failed after creating data volume(s) "
                    f"{', '.join(created_storage_volumes)}; leaving them in place. "
                    "A later deploy retry will refuse existing data volumes. If this was a failed first install "
                    "and those volumes contain no initialized TrustyClaw state, delete the tagged volumes before "
                    "retrying deploy."
                )
            raise
        result = {
            "agent_name": config.agent_name,
            "instance_id": instance_id,
            "region": config.aws_region,
            "public_dns": public_dns,
            "ssh_user": SSH_USER,
            "admin_ui_local_url": f"http://127.0.0.1:{ADMIN_API_PORT}",
            "admin_volume_id": storage_volumes["admin"],
            "agent_volume_id": storage_volumes["agent"],
            "version": target_version,
        }
        if github_commit_sha is not None:
            result["github_source"] = f"{PUBLIC_GITHUB_REPOSITORY}@{github_commit_sha}"
        if replacement_operator_connections is not None:
            result["operator_connections"] = public_operator_connections(
                [connection.to_json() for connection in replacement_operator_connections]
            )
        # stdout carries only this result JSON; all progress went to stderr.
        print(json.dumps(result, indent=2, sort_keys=True))
        return 0
    except ConfigError as exc:
        print(f"config error: {exc}", file=sys.stderr)
        return 2
    except subprocess.CalledProcessError as exc:
        stderr = exc.stderr.decode() if isinstance(exc.stderr, bytes) else exc.stderr
        print(f"deploy command failed: {stderr or exc}", file=sys.stderr)
        return 1


def _launch_access_state(
    config: InputConfig,
    env: dict[str, str],
    vpc_id: str,
    replacement_operator_connections: tuple[RuntimeOperatorConnection, ...] | None,
) -> tuple[bool, bool]:
    """Decide the desired final (ssh_ingress, cloudflare_egress) state.

    One derivation serves both deliveries: deploy and reconfigure derive it
    from the input operator connections; upgrade and recover reapply the
    previous converged security-group state, because their operator
    connections are preserved on the admin volume and unchanged by the
    operation. The SSH delivery additionally opens SSH at launch for the
    single-use deploy key and closes it after bootstrap when this state says
    so; the GitHub delivery launches with this state directly.
    """
    if replacement_operator_connections is not None:
        return (
            any(connection.mode == "ssh" for connection in replacement_operator_connections),
            any(connection.mode == "cloudflare_access" for connection in replacement_operator_connections),
        )
    captured = _security_group_access_state(config, env, vpc_id)
    if captured is not None:
        return captured
    _log(
        "no existing security group records the operator access state; launching with SSH "
        "ingress closed and the Cloudflare connector egress open"
    )
    return False, True


def _resolve_github_pin(commit_sha: str) -> tuple[str, str]:
    """Resolve the pinned commit and its VERSION from the public repository,
    returning (commit_sha, target_version) after the operator confirms.

    An empty commit_sha pins the latest main commit. Fails closed before
    anything in AWS is touched: on any GitHub read failure and on a declined
    or impossible confirmation. Non-interactive callers pipe the confirmation
    into stdin. The host-side gate in self_provision then re-checks the
    fetched checkout against this version authoritatively.
    """
    if not commit_sha:
        head_url = f"https://api.github.com/repos/{PUBLIC_GITHUB_REPOSITORY}/commits/main"
        request = urllib.request.Request(
            head_url,
            headers={"User-Agent": "trustyclaw-cli", "Accept": "application/vnd.github+json"},
        )
        try:
            with urllib.request.urlopen(request, timeout=20) as response:
                head = json.loads(response.read().decode("utf-8"))
        except Exception as exc:
            raise ConfigError(f"could not read the latest main commit from {head_url}: {exc}") from exc
        head_sha = head.get("sha") if isinstance(head, dict) else None
        if not isinstance(head_sha, str) or not _COMMIT_SHA_RE.fullmatch(head_sha):
            raise ConfigError(f"{head_url} returned an invalid head commit")
        commit_sha = head_sha
        _log(f"latest {PUBLIC_GITHUB_REPOSITORY} main commit is {commit_sha}")
    url = f"https://raw.githubusercontent.com/{PUBLIC_GITHUB_REPOSITORY}/{commit_sha}/VERSION"
    try:
        with urllib.request.urlopen(url, timeout=20) as response:
            pinned_version = response.read().decode("utf-8").strip()
    except Exception as exc:
        raise ConfigError(f"could not read the pinned commit's VERSION from {url}: {exc}") from exc
    try:
        parse_version(pinned_version)
    except ValueError as exc:
        raise ConfigError(f"pinned commit has an invalid VERSION {pinned_version!r}") from exc
    if compare_versions(pinned_version, _MIN_GITHUB_DELIVERY_VERSION) < 0:
        raise ConfigError(
            f"pinned commit is TrustyClaw {pinned_version}, but the GitHub delivery requires "
            f"{_MIN_GITHUB_DELIVERY_VERSION} or newer; use the SSH delivery for older versions"
        )
    _log(f"pinned commit {commit_sha} deploys TrustyClaw {pinned_version} from {PUBLIC_GITHUB_REPOSITORY}")
    try:
        print(f"Proceed with TrustyClaw {pinned_version}? [y/N]: ", end="", file=sys.stderr, flush=True)
        answer = input()
    except EOFError as exc:
        raise ConfigError(
            "no terminal to confirm the fetched version; pipe 'y' into stdin to confirm"
        ) from exc
    if answer.strip().lower() not in {"y", "yes"}:
        raise ConfigError("aborted at version confirmation")
    return commit_sha, pinned_version


def _aws_region_from_env() -> str:
    region = os.environ.get("AWS_REGION") or os.environ.get("AWS_DEFAULT_REGION")
    if not region:
        raise ConfigError("set AWS_REGION to the agent's AWS region")
    return region


