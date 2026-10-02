#!/usr/bin/env bash
# Run a command in a disposable, unprivileged claude-realms test container.
#
#   test/docker/run.sh [--name NAME] [--detach] [--kvm] [--] COMMAND...
#
# Isolation: runs as uid 1000 with every capability dropped and
# no-new-privileges; no --privileged, no host namespaces, no host mounts other
# than the read-only plugin checkout. seccomp and the /proc mask are relaxed
# only so bubblewrap can create the user/pid namespaces the driver sandbox
# needs (the same thing it does on a desktop). Everything the test writes
# lives in the container's own tmpfs/volume and disappears with it.
set -euo pipefail
here=$(cd "$(dirname "$0")" && pwd)
root=$(cd "$here/../.." && pwd)
name="crr-$$"; detach=(); devices=(); extra=()
while [[ $# -gt 0 ]]; do
  case "$1" in
    --name) name=$2; shift 2;;
    --detach) detach=(-d); shift;;
    --kvm) devices=(--device /dev/kvm); shift;;
    --mount) extra+=(-v "$2"); shift 2;;
    --env) extra+=(-e "$2"); shift 2;;
    --) shift; break;;
    *) break;;
  esac
done
exec docker run --rm "${detach[@]}" --name "$name" --init \
  -u 1000:1000 --cap-drop ALL --security-opt no-new-privileges \
  --security-opt seccomp=unconfined --security-opt systempaths=unconfined \
  --tmpfs /run/user/1000:uid=1000,gid=1000,mode=0700,exec \
  --tmpfs /tmp:exec,mode=1777 \
  --shm-size 256m \
  -e HOME=/home/tester -e XDG_RUNTIME_DIR=/run/user/1000 -e USER=tester \
  -e UV_PROJECT_ENVIRONMENT=/home/tester/venv -e UV_CACHE_DIR=/home/tester/.cache/uv \
  -e REALMS_HOME=/home/tester/realms-data \
  -v "$root":/plugin:ro \
  -v crr-uv-cache:/home/tester/.cache/uv \
  "${devices[@]}" "${extra[@]}" \
  -w /plugin claude-realms-rig:latest "$@"
