#!/usr/bin/env bash
# Run a command inside the CI sandbox image with no network access. The
# repository is mounted read-only and copied to a writable workspace, so the
# command can neither reach the internet nor modify the checkout.
set -euo pipefail

if [[ "$#" -ne 2 ]]; then
  echo "Usage: $0 <image-tag> <command>" >&2
  exit 1
fi

image_tag="$1"
command_string="$2"
workspace_root="${GITHUB_WORKSPACE:-${PWD}}"
host_sandbox_root="$(mktemp -d "${RUNNER_TEMP:-/tmp}/ci-sandbox.XXXXXX")"

cleanup() {
  rm -rf "${host_sandbox_root}"
}
trap cleanup EXIT

mkdir -p "${host_sandbox_root}/home"
chmod -R 0777 "${host_sandbox_root}"

docker run \
  --rm \
  --network none \
  --cap-drop ALL \
  --security-opt no-new-privileges:true \
  --mount "type=bind,src=${workspace_root},dst=/src,readonly" \
  --mount "type=bind,src=${host_sandbox_root},dst=/sandbox" \
  --workdir /sandbox \
  --user "$(id -u):$(id -g)" \
  --env HOME=/sandbox/home \
  "${image_tag}" bash -c '
set -euo pipefail
rsync -a --delete --no-owner --no-group /src/ /sandbox/repo/
chmod -R u+w /sandbox/repo || true
cd /sandbox/repo
bash -c "$1"
' _ "${command_string}"
