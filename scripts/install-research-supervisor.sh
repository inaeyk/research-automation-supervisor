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

release_root=/opt/research-supervisor-release
expected_installer=$release_root/scripts/install-research-supervisor.sh
release_verifier=/usr/libexec/research-supervisor/verify-protected-release

die() {
    printf 'ERROR: %s\n' "$*" >&2
    exit 2
}

if [ "$(id -u)" -ne 0 ]; then
    die "This protected release payload is invoked only by the distribution-installed release authority."
fi
if [ "$#" -ne 1 ]; then
    die "Exactly one existing ordinary operator account is required."
fi
operator_name=$1
actual_installer=$(/usr/bin/readlink -f -- "$0") || die "Installer path is unavailable."
if [ "$actual_installer" != "$expected_installer" ] || [ -L "$0" ]; then
    die "Protected release installer selection is invalid."
fi
for protected_directory in / /opt "$release_root" "$release_root/scripts"; do
    if [ "$(/usr/bin/stat -c '%u:%g:%a:%F' -- "$protected_directory")" != \
         "0:0:755:directory" ]; then
        die "Protected release ancestry is missing or unsafe."
    fi
done
if [ -L "$release_verifier" ] || \
   [ "$(/usr/bin/stat -c '%u:%g:%a:%F' -- "$release_verifier")" != \
     "0:0:755:regular file" ]; then
    die "Distribution release verifier is missing or unsafe."
fi

"$release_verifier"
/bin/sh "$release_root/scripts/install-managed-codex.sh"
/bin/sh "$release_root/scripts/install-core-authority-service.sh" "$operator_name"

echo "Research Supervisor one-time administrator setup is complete."
echo "The operator must sign out and back in once, then use explicit first-time setup."
