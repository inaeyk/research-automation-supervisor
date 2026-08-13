#!/bin/sh
set -eu

project_root=${1:?project root is required}
launch_mode=${2:-normal}
data_root=${XDG_DATA_HOME:-"$HOME/.local/share"}/research-automation-supervisor
runtime_root=$data_root/runtime
managed_venv=$runtime_root/venv
backend_log=$data_root/custodian-state/backend.log
install_stamp=$runtime_root/installed-commit
url=http://127.0.0.1:8765/
readiness=$data_root/custodian-state/backend-readiness.json

mkdir -p "$runtime_root" "$data_root/custodian-state"

if command -v python3 >/dev/null 2>&1; then
    system_python=$(command -v python3)
else
    exit 3
fi

if [ ! -x "$managed_venv/bin/python" ]; then
    "$system_python" -m venv "$managed_venv"
fi

current_commit=$(git -C "$project_root" rev-parse HEAD 2>/dev/null || true)
installed_commit=$(sed -n '1p' "$install_stamp" 2>/dev/null || true)
if [ "$launch_mode" = first-run ] || [ "$current_commit" != "$installed_commit" ]; then
    "$managed_venv/bin/python" -m pip install --disable-pip-version-check "$project_root" >>"$backend_log" 2>&1
    stamp_tmp=$runtime_root/.installed-commit.tmp
    printf '%s\n' "$current_commit" >"$stamp_tmp"
    mv "$stamp_tmp" "$install_stamp"
fi

health_matches() {
    "$managed_venv/bin/python" -c 'import json,sys,urllib.request; value=json.load(urllib.request.urlopen("http://127.0.0.1:8765/api/health",timeout=1)); raise SystemExit(0 if value.get("application")=="Research Automation Supervisor" and value.get("qualified_commit")==sys.argv[1] else 1)' "$current_commit" >/dev/null 2>&1
}

if health_matches; then
    explorer.exe "$url" >/dev/null 2>&1 || true
    exit 0
fi

old_pid=$("$managed_venv/bin/python" -c 'import json,sys; value=json.load(open(sys.argv[1],encoding="utf-8")); pid=value.get("pid"); print(pid if isinstance(pid,int) and pid>1 else "")' "$readiness" 2>/dev/null || true)
if [ -n "$old_pid" ] && [ -r "/proc/$old_pid/cmdline" ]; then
    old_command=$(tr '\000' ' ' <"/proc/$old_pid/cmdline")
    case "$old_command" in
        *research-supervisor-custodian*) kill -TERM "$old_pid" 2>/dev/null || true ;;
    esac
fi

RAS_MANAGED_RUNTIME=1 RAS_QUALIFIED_COMMIT="$current_commit" nohup "$managed_venv/bin/research-supervisor-custodian" \
    --data-dir "$data_root" --host 127.0.0.1 --port 8765 \
    >>"$backend_log" 2>&1 </dev/null &

attempt=0
while [ "$attempt" -lt 120 ]; do
    if health_matches; then
        explorer.exe "$url" >/dev/null 2>&1 || true
        exit 0
    fi
    attempt=$((attempt + 1))
    sleep 0.25
done

exit 4
