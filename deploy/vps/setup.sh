#!/usr/bin/env bash
# shizzle VPS bootstrap — idempotent. Run as root on the VPS:
#   ssh root@72.60.173.171 'bash -s' < deploy/vps/setup.sh
# Safe to re-run: every step checks before it changes.
set -euo pipefail
export DEBIAN_FRONTEND=noninteractive

log() { printf '\n==> %s\n' "$*"; }

# ---------------------------------------------------------------- timezone
log "timezone -> UTC"
if [ "$(timedatectl show -p Timezone --value)" != "Etc/UTC" ] \
   && [ "$(timedatectl show -p Timezone --value)" != "UTC" ]; then
  timedatectl set-timezone UTC
fi
timedatectl show -p Timezone --value

# ---------------------------------------------------------------- apt update/upgrade
log "apt update + upgrade (non-interactive, keep existing confs)"
apt-get update -q
apt-get upgrade -y -q \
  -o Dpkg::Options::=--force-confdef \
  -o Dpkg::Options::=--force-confold

log "base packages"
apt-get install -y -q ca-certificates curl gnupg ufw fail2ban unattended-upgrades

# ---------------------------------------------------------------- unattended-upgrades
log "unattended-upgrades enabled"
cat > /etc/apt/apt.conf.d/20auto-upgrades <<'EOF'
APT::Periodic::Update-Package-Lists "1";
APT::Periodic::Unattended-Upgrade "1";
EOF
systemctl enable --now unattended-upgrades

# ---------------------------------------------------------------- fail2ban (sshd)
log "fail2ban jail for sshd"
cat > /etc/fail2ban/jail.local <<'EOF'
[DEFAULT]
bantime  = 1h
findtime = 10m
maxretry = 5
backend  = systemd

[sshd]
enabled = true
EOF
systemctl enable --now fail2ban
systemctl restart fail2ban

# ---------------------------------------------------------------- Docker (official repo)
if ! command -v docker >/dev/null 2>&1; then
  log "installing Docker Engine from download.docker.com"
  install -m 0755 -d /etc/apt/keyrings
  curl -fsSL https://download.docker.com/linux/ubuntu/gpg -o /etc/apt/keyrings/docker.asc
  chmod a+r /etc/apt/keyrings/docker.asc
  echo "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/docker.asc] \
https://download.docker.com/linux/ubuntu $(. /etc/os-release && echo "$VERSION_CODENAME") stable" \
    > /etc/apt/sources.list.d/docker.list
  apt-get update -q
  apt-get install -y -q docker-ce docker-ce-cli containerd.io docker-buildx-plugin docker-compose-plugin
else
  log "docker already installed: $(docker --version)"
fi
systemctl enable --now docker

# ---------------------------------------------------------------- firewall
# NOTE: rules are added BEFORE enable so SSH is never cut off. Verify with a
# second SSH session after this script finishes, before relying on it.
log "ufw: allow 22, 80, 443; enable"
ufw allow 22/tcp
ufw allow 80/tcp
ufw allow 443/tcp
ufw --force enable
ufw status verbose

# ---------------------------------------------------------------- deploy user
log "deploy user 'shizzle' (docker group, root's SSH key)"
if ! id shizzle >/dev/null 2>&1; then
  useradd -m -s /bin/bash shizzle
fi
usermod -aG docker shizzle
install -d -m 700 -o shizzle -g shizzle /home/shizzle/.ssh
if [ -f /root/.ssh/authorized_keys ]; then
  cp /root/.ssh/authorized_keys /home/shizzle/.ssh/authorized_keys
  chown shizzle:shizzle /home/shizzle/.ssh/authorized_keys
  chmod 600 /home/shizzle/.ssh/authorized_keys
fi
# TODO (post-migration, do not enforce yet): once deploys run as 'shizzle',
# set PermitRootLogin no in /etc/ssh/sshd_config and restart sshd.

# ---------------------------------------------------------------- stack dir
log "/opt/shizzle"
install -d -o shizzle -g shizzle /opt/shizzle

log "bootstrap complete"
docker --version
docker compose version
fail2ban-client status sshd || true
