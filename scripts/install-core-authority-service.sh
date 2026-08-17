#!/bin/sh
set -eu
umask 022

if [ "$(id -u)" -ne 0 ]; then
    echo "Run this one-time installer with administrator authorization." >&2
    exit 2
fi

project_root=${1:?trusted project root is required}
operator_name=${2:-${SUDO_USER:-}}
if [ -z "$operator_name" ] || ! getent passwd "$operator_name" >/dev/null; then
    echo "An existing ordinary operator account is required." >&2
    exit 2
fi
operator_uid=$(id -u "$operator_name")
case "$operator_uid" in
    *[!0-9]*|'') exit 2 ;;
esac
if [ "$operator_uid" -eq 0 ]; then
    echo "The Custodian must run as an ordinary user, not root." >&2
    exit 2
fi

if ! getent group research-supervisor-custodian >/dev/null; then
    groupadd --system research-supervisor-custodian
fi
if ! getent passwd research-supervisor-core >/dev/null; then
    useradd --system --home-dir /nonexistent --no-create-home \
        --shell /usr/sbin/nologin research-supervisor-core
fi
usermod -a -G research-supervisor-custodian "$operator_name"
usermod -a -G research-supervisor-custodian research-supervisor-core

install -d -o root -g root -m 0755 /opt/research-supervisor-core
venv_root=/opt/research-supervisor-core/venv
if [ ! -x "$venv_root/bin/python" ]; then
    python3 -m venv "$venv_root"
fi
"$venv_root/bin/python" -m pip install \
    --disable-pip-version-check --upgrade "$project_root"
find "$venv_root" -xdev -exec chown -h root:root {} +
find "$venv_root" -xdev -type d -exec chmod 0755 {} +
find "$venv_root" -xdev -type f -perm /0111 -exec chmod 0755 {} +
find "$venv_root" -xdev -type f ! -perm /0111 -exec chmod 0644 {} +

install -d -o research-supervisor-core -g research-supervisor-custodian -m 0711 \
    /var/lib/research-supervisor-core
install -d -o research-supervisor-core -g research-supervisor-custodian -m 0700 \
    /var/lib/research-supervisor-core/authority
install -d -o research-supervisor-core -g research-supervisor-custodian -m 0710 \
    /var/lib/research-supervisor-core/snapshots
install -d -o research-supervisor-core -g research-supervisor-custodian -m 2710 \
    /var/lib/research-supervisor-core/snapshots/workspaces
install -d -o root -g root -m 0755 /etc/research-supervisor-core
environment_file=/etc/research-supervisor-core/service.env
temporary_environment=/etc/research-supervisor-core/.service.env.tmp
printf 'OPERATOR_UID=%s\n' "$operator_uid" >"$temporary_environment"
chown root:root "$temporary_environment"
chmod 0644 "$temporary_environment"
mv "$temporary_environment" "$environment_file"
install -o root -g root -m 0644 \
    "$project_root/scripts/research-supervisor-core-authority.service" \
    /etc/systemd/system/research-supervisor-core-authority.service
systemctl daemon-reload
systemctl enable research-supervisor-core-authority.service
systemctl restart research-supervisor-core-authority.service

echo "Core Authority Service installed. Sign out and back in once before ordinary use."
