"""Host-side provisioning entry, shared by both deliveries.

Runs as root with a checkout of the code to install on PYTHONPATH: the
GitHub delivery fetches the pinned public commit from EC2 user data, and the
SSH delivery extracts the scp'd code archive. It enforces the version gate
(checkout VERSION must equal the operation's target), renders the bootstrap
script and the runtime code archive from the checkout, and runs the
bootstrap. On the GitHub delivery the lifecycle CLI has already returned and
output lands in the cloud-init log and EC2 console output; on the SSH
delivery the output streams back through the CLI's SSH session.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import shutil
import subprocess
import sys

from host.bootstrap.render import _render_bootstrap, _write_runtime_code_archive

BOOTSTRAP_PATH = Path("/tmp/trustyclaw_bootstrap.sh")
CODE_ARCHIVE_PATH = Path("/tmp/trustyclaw-host-code.tar.gz")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python3 -m host.bootstrap.self_provision")
    parser.add_argument("--payload", required=True, help="Path to the staged provisioning payload JSON")
    parser.add_argument("--checkout", required=True, help="Path to the fetched pinned checkout")
    args = parser.parse_args(argv)

    payload = json.loads(Path(args.payload).read_text())
    target_version = payload["operation"]["target_version"]
    checkout = Path(args.checkout)
    checkout_version = (checkout / "VERSION").read_text().strip()
    if checkout_version != target_version:
        print(
            f"pinned commit has VERSION {checkout_version}, but the operation targets {target_version}; "
            "the commit you pin must be the code that launched the instance",
            file=sys.stderr,
        )
        return 2

    BOOTSTRAP_PATH.write_text(_render_bootstrap())
    # On the SSH delivery this path already holds the operator-owned archive
    # the CLI scp'd; `fs.protected_regular` blocks even root from truncating a
    # file it does not own in a world-writable sticky directory like /tmp, so
    # remove it first and write a fresh root-owned archive. On the GitHub
    # delivery the path does not exist and this is a no-op.
    CODE_ARCHIVE_PATH.unlink(missing_ok=True)
    _write_runtime_code_archive(CODE_ARCHIVE_PATH)
    completed = subprocess.run(["bash", str(BOOTSTRAP_PATH)])
    if completed.returncode != 0:
        print(f"bootstrap failed on the host (exit {completed.returncode})", file=sys.stderr)
        return 1
    # The checkout holds no secrets, but a stray full source tree on the root
    # volume serves nothing after the runtime install; remove it on success.
    shutil.rmtree(checkout, ignore_errors=True)
    print("TrustyClaw self-provision complete")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
