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
expected_installer=$release_root/scripts/install-managed-codex.sh
release_verifier=/usr/libexec/research-supervisor/verify-protected-release
protected_python_launcher=$release_root/scripts/run-protected-python.sh

die() {
    printf 'ERROR: %s\n' "$*" >&2
    exit 2
}

effective_uid=$(/usr/bin/id -u)
if [ "$effective_uid" -ne 0 ]; then
    if [ "$#" -eq 1 ] && [ "$1" = "--qualification-import-probe" ]; then
        actual_installer=$(/usr/bin/readlink -f -- "$0") || \
            die "Installer path is unavailable."
        scripts_directory=$(/usr/bin/dirname -- "$actual_installer")
        simulated_release_root=$(/usr/bin/readlink -f -- "$scripts_directory/..") || \
            die "Qualification release root is unavailable."
        if [ "$actual_installer" != \
             "$simulated_release_root/scripts/install-managed-codex.sh" ] || \
           [ -L "$0" ]; then
            die "Qualification payload selection is invalid."
        fi
        exec /bin/sh "$simulated_release_root/scripts/run-protected-python.sh" \
            --qualification-import-probe
    fi
    die "This protected release payload is invoked only by the distribution-installed release authority."
fi
if [ "$#" -ne 0 ]; then
    die "Managed Codex identity comes from the protected release approval, not caller arguments."
fi
actual_installer=$(/usr/bin/readlink -f -- "$0") || die "Installer path is unavailable."
if [ "$actual_installer" != "$expected_installer" ] || [ -L "$0" ]; then
    die "Run only the administrator-staged installer at $expected_installer."
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
cd "$release_root"
exec /bin/sh "$protected_python_launcher" install
