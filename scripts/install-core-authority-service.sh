#!/bin/sh
# Protected release payload only. Never execute this source-checkout copy with sudo;
# the supported first privileged byte is the distribution-installed /usr/libexec helper.
set -eu
umask 022
IFS=' '
PATH=/usr/sbin:/usr/bin:/sbin:/bin
LANG=C.UTF-8
LC_ALL=C.UTF-8
export PATH LANG LC_ALL

effective_uid=$(/usr/bin/id -u)
if [ "$effective_uid" -ne 0 ]; then
    if [ "$#" -eq 1 ] && [ "$1" = "--qualification-import-probe" ]; then
        actual_installer=$(/usr/bin/readlink -f -- "$0") || exit 2
        scripts_directory=$(/usr/bin/dirname -- "$actual_installer")
        simulated_release_root=$(/usr/bin/readlink -f -- "$scripts_directory/..") || \
            exit 2
        if [ "$actual_installer" != \
             "$simulated_release_root/scripts/install-core-authority-service.sh" ] || \
           [ -L "$0" ]; then
            echo "Qualification payload selection is invalid." >&2
            exit 2
        fi
        exec /bin/sh "$simulated_release_root/scripts/run-protected-python.sh" \
            --qualification-import-probe
    fi
    echo "This protected release payload is invoked only by the distribution-installed release authority." >&2
    exit 2
fi

release_root=/opt/research-supervisor-release
expected_installer=$release_root/scripts/install-core-authority-service.sh
release_verifier=/usr/libexec/research-supervisor/verify-protected-release
protected_python_launcher=$release_root/scripts/run-protected-python.sh
actual_installer=$(/usr/bin/readlink -f -- "$0") || exit 2
if [ "$actual_installer" != "$expected_installer" ] || [ -L "$0" ]; then
    echo "Run only the administrator-staged Core installer at $expected_installer." >&2
    exit 2
fi
for protected_directory in / /opt "$release_root" "$release_root/scripts"; do
    if [ "$(/usr/bin/stat -c '%u:%g:%a:%F' -- "$protected_directory")" != \
         "0:0:755:directory" ]; then
        echo "Protected release ancestry is missing or unsafe." >&2
        exit 2
    fi
done
if [ -L "$release_verifier" ] || \
   [ "$(/usr/bin/stat -c '%u:%g:%a:%F' -- "$release_verifier")" != \
     "0:0:755:regular file" ]; then
    echo "Distribution release verifier is missing or unsafe." >&2
    exit 2
fi
"$release_verifier"
cd "$release_root"
/bin/sh "$protected_python_launcher" verify

if [ "$#" -ne 1 ]; then
    echo "Exactly one existing ordinary operator account is required." >&2
    exit 2
fi
operator_name=$1
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
release_package=$release_root/artifacts/research_automation_supervisor-0.2.0-py3-none-any.whl
release_wheelhouse=$release_root/wheelhouse
if [ ! -f "$release_package" ] || [ -L "$release_package" ] || \
   [ ! -d "$release_wheelhouse" ] || [ -L "$release_wheelhouse" ]; then
    echo "The protected offline Python release payload is incomplete." >&2
    exit 2
fi
if [ ! -x "$venv_root/bin/python" ]; then
    /usr/bin/env -i \
        PATH=/usr/sbin:/usr/bin:/sbin:/bin LANG=C.UTF-8 LC_ALL=C.UTF-8 \
        HOME=/nonexistent \
        /usr/bin/python3 -I -S -B -m venv "$venv_root"
fi
/usr/bin/env -i \
    PATH=/usr/sbin:/usr/bin:/sbin:/bin LANG=C.UTF-8 LC_ALL=C.UTF-8 \
    HOME=/nonexistent PIP_CONFIG_FILE=/dev/null \
    "$venv_root/bin/python" -I -B -m pip install \
    --disable-pip-version-check --no-index --only-binary=:all: \
    --find-links "$release_wheelhouse" --upgrade "$release_package"
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
/bin/sh "$protected_python_launcher" bind-home "$operator_name"
environment_file=/etc/research-supervisor-core/service.env
temporary_environment=/etc/research-supervisor-core/.service.env.tmp
printf 'OPERATOR_UID=%s\n' "$operator_uid" >"$temporary_environment"
chown root:root "$temporary_environment"
chmod 0644 "$temporary_environment"
mv "$temporary_environment" "$environment_file"
install -o root -g root -m 0644 \
    "$release_root/scripts/research-supervisor-core-authority.service" \
    /etc/systemd/system/research-supervisor-core-authority.service
systemctl daemon-reload
systemctl enable research-supervisor-core-authority.service
systemctl restart research-supervisor-core-authority.service

echo "Core Authority Service installed. Sign out and back in once before ordinary use."
