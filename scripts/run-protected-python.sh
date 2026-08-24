#!/bin/sh
# Protected release payload only. This is the sole privileged Python launch boundary.
set -eu
umask 022
IFS=' '
PATH=/usr/sbin:/usr/bin:/sbin:/bin
LANG=C.UTF-8
LC_ALL=C.UTF-8
export PATH LANG LC_ALL

production_release_root=/opt/research-supervisor-release
production_launcher=$production_release_root/scripts/run-protected-python.sh
production_verifier=/usr/libexec/research-supervisor/verify-protected-release
protected_python=/usr/bin/python3
qualification_probe=false

die() {
    printf 'ERROR: %s\n' "$*" >&2
    exit 2
}

effective_uid=$(/usr/bin/id -u)
if [ "$effective_uid" -eq 0 ]; then
    release_root=$production_release_root
    expected_launcher=$production_launcher
    case "$#:$1" in
        1:install|1:verify) ;;
        2:bind-home) ;;
        *) die "Unknown protected managed-Codex Python operation." ;;
    esac
else
    if [ "$#" -ne 1 ] || [ "$1" != "--qualification-import-probe" ]; then
        die "This protected Python launcher is invoked only by protected release payloads."
    fi
    qualification_probe=true
    actual_launcher=$(/usr/bin/readlink -f -- "$0") || die "Launcher path is unavailable."
    scripts_directory=$(/usr/bin/dirname -- "$actual_launcher")
    release_root=$(/usr/bin/readlink -f -- "$scripts_directory/..") || \
        die "Qualification release root is unavailable."
    expected_launcher=$release_root/scripts/run-protected-python.sh
    protected_python=${RAS_PROTECTED_PYTHON_TEST_ONLY_EXECUTABLE:-$protected_python}
fi

actual_launcher=$(/usr/bin/readlink -f -- "$0") || die "Launcher path is unavailable."
if [ "$actual_launcher" != "$expected_launcher" ] || [ -L "$0" ]; then
    die "Protected Python launcher selection is invalid."
fi

if [ "$qualification_probe" = false ]; then
    for protected_directory in / /opt "$release_root" "$release_root/scripts" \
        "$release_root/src" "$release_root/src/research_automation_supervisor"; do
        if [ "$(/usr/bin/stat -c '%u:%g:%a:%F' -- "$protected_directory")" != \
             "0:0:755:directory" ]; then
            die "Protected Python release ancestry is missing or unsafe."
        fi
    done
    if [ -L "$production_verifier" ] || \
       [ "$(/usr/bin/stat -c '%u:%g:%a:%F' -- "$production_verifier")" != \
         "0:0:755:regular file" ]; then
        die "Distribution release verifier is missing or unsafe."
    fi
    "$production_verifier"
else
    simulated_owner=$effective_uid
    simulated_group=$(/usr/bin/id -g)
    for protected_directory in "$release_root" "$release_root/scripts" \
        "$release_root/src" "$release_root/src/research_automation_supervisor"; do
        if [ "$(/usr/bin/stat -c '%u:%g:%a:%F' -- "$protected_directory")" != \
             "$simulated_owner:$simulated_group:755:directory" ]; then
            die "Simulated protected Python release ancestry is missing or unsafe."
        fi
    done
fi

case "$protected_python" in
    /*) ;;
    *) die "Protected Python interpreter path is not absolute." ;;
esac
if [ ! -x "$protected_python" ]; then
    die "Protected Python interpreter is missing."
fi
resolved_python=$(/usr/bin/readlink -f -- "$protected_python") || \
    die "Protected Python interpreter is unavailable."
case "$resolved_python" in
    /usr/bin/python3.[0-9]*) ;;
    *) die "Protected Python interpreter target is outside the fixed system contract." ;;
esac
if [ "$qualification_probe" = false ]; then
    expected_system_owner=0
    expected_system_group=0
else
    expected_system_owner=$(/usr/bin/stat -Lc '%u' -- /usr)
    expected_system_group=$(/usr/bin/stat -Lc '%g' -- /usr)
fi
for system_directory in /usr /usr/bin; do
    if [ "$(/usr/bin/stat -Lc '%u:%g:%a:%F' -- "$system_directory")" != \
         "$expected_system_owner:$expected_system_group:755:directory" ]; then
        die "Protected Python interpreter ancestry is unsafe."
    fi
done
if [ "$(/usr/bin/stat -Lc '%u:%g:%a:%F' -- "$protected_python")" != \
     "$expected_system_owner:$expected_system_group:755:regular file" ]; then
    die "Protected Python interpreter metadata is unsafe."
fi

entrypoint=$release_root/scripts/protected-managed-codex-entry.py
if [ ! -f "$entrypoint" ] || [ -L "$entrypoint" ]; then
    die "Protected Python application entrypoint is missing or unsafe."
fi
if [ "$qualification_probe" = false ]; then
    expected_entrypoint_status=0:0:755:regular\ file
else
    expected_entrypoint_status=$simulated_owner:$simulated_group:755:regular\ file
fi
if [ "$(/usr/bin/stat -c '%u:%g:%a:%F' -- "$entrypoint")" != \
     "$expected_entrypoint_status" ]; then
    die "Protected Python application entrypoint metadata is unsafe."
fi

cd "$release_root"
if [ "$qualification_probe" = true ]; then
    exec /usr/bin/env -i \
        PATH=/usr/sbin:/usr/bin:/sbin:/bin LANG=C.UTF-8 LC_ALL=C.UTF-8 \
        RAS_PROTECTED_IMPORT_QUALIFICATION=1 \
        "$protected_python" -I -B "$entrypoint" --qualification-import-probe
fi
exec /usr/bin/env -i \
    PATH=/usr/sbin:/usr/bin:/sbin:/bin LANG=C.UTF-8 LC_ALL=C.UTF-8 \
    "$protected_python" -I -B "$entrypoint" "$@"
