#!/usr/bin/env bash
# Install native Docker Engine inside WSL2 (Ubuntu) for Omniscient's prototype sandboxes.
#
# Why: AgentRun refuses Docker Desktop's sandbox (unconfined seccomp) and Desktop boxes get
# no network. A native engine has the default seccomp profile, network, and a data dir
# AgentRun can see. Method: Docker's official apt repository
# (https://docs.docker.com/engine/install/ubuntu/).
#
# BEFORE running: Docker Desktop -> Settings -> Resources -> WSL integration -> turn OFF
# your Ubuntu distro -> Apply & restart. Then in PowerShell: `wsl --shutdown`, reopen WSL.
# Docker Desktop keeps working on Windows; inside WSL, `docker` becomes the native engine.
#
# Run: sudo bash scripts/install-docker-wsl.sh
set -euo pipefail

if [ "$(id -u)" -ne 0 ]; then
  echo "Run with sudo: sudo bash $0" >&2
  exit 1
fi
TARGET_USER="${SUDO_USER:?run through sudo so the right user joins the docker group}"

if readlink -f /usr/bin/docker 2>/dev/null | grep -q "/mnt/wsl/docker-desktop"; then
  echo "Docker Desktop's WSL integration is still active for this distro." >&2
  echo "Turn it off (Settings > Resources > WSL integration), run 'wsl --shutdown'" >&2
  echo "from PowerShell, reopen WSL, then run this script again." >&2
  exit 2
fi

echo "== Removing conflicting distro packages (if any)"
for pkg in docker.io docker-doc docker-compose podman-docker containerd runc; do
  apt-get remove -y "$pkg" >/dev/null 2>&1 || true
done

echo "== Adding Docker's official apt repository"
apt-get update -qq
apt-get install -y -qq ca-certificates curl
install -m 0755 -d /etc/apt/keyrings
curl -fsSL https://download.docker.com/linux/ubuntu/gpg -o /etc/apt/keyrings/docker.asc
chmod a+r /etc/apt/keyrings/docker.asc
# shellcheck source=/dev/null
. /etc/os-release
echo "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/docker.asc] \
https://download.docker.com/linux/ubuntu ${UBUNTU_CODENAME:-$VERSION_CODENAME} stable" \
  > /etc/apt/sources.list.d/docker.list

echo "== Installing Docker Engine"
apt-get update -qq
apt-get install -y -qq docker-ce docker-ce-cli containerd.io docker-buildx-plugin docker-compose-plugin

echo "== Starting the engine and adding ${TARGET_USER} to the docker group"
systemctl enable --now docker
usermod -aG docker "$TARGET_USER"

echo "== Checks"
docker version --format 'engine {{.Server.Version}}'
if docker info --format '{{json .SecurityOptions}}' | grep -q 'seccomp'; then
  echo "seccomp: on (default profile)"
else
  echo "WARNING: seccomp not reported; AgentRun may still refuse the sandbox" >&2
fi
docker run --rm busybox:1.36 wget -q -O /dev/null https://pypi.org \
  && echo "container network: ok" \
  || echo "WARNING: container could not reach pypi.org" >&2

echo
echo "Done. Close and reopen your WSL terminal (docker group), then tell Claude to"
echo "switch AgentRun's sandbox to the native engine."
