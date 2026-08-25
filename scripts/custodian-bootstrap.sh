#!/bin/sh
set -eu
umask 077
PATH=/usr/bin:/bin
export PATH

project_root=${1:?project root is required}
launch_mode=${2:-normal}
readiness_instance=${3:?readiness instance is required}
data_override=${4:-}
acceptance_scenario=${5:-}
port=${6:-8765}
case "$readiness_instance" in
    *[!A-Fa-f0-9]*|'') exit 2 ;;
esac
case "$port" in
    *[!0-9]*|'') exit 2 ;;
esac
if [ "$port" -lt 1024 ] || [ "$port" -gt 65535 ]; then
    exit 2
fi
managed_codex_helper=$project_root/scripts/prepare-managed-codex-home.py
url=http://127.0.0.1:$port/
core_socket=/run/research-supervisor-core/authority.sock

if [ -x /usr/bin/python3 ]; then
    system_python=/usr/bin/python3
else
    exit 3
fi
if [ ! -f "$managed_codex_helper" ]; then
    echo "Managed Codex sign-in storage setup is unavailable." >&2
    exit 7
fi
if [ -n "$acceptance_scenario" ]; then
    if [ -z "$data_override" ]; then
        exit 5
    fi
    managed_codex_home=$("$system_python" "$managed_codex_helper" \
        acceptance-test "$data_override") || exit $?
else
    if [ -n "$data_override" ]; then
        echo "Qualified application data cannot be redirected." >&2
        exit 7
    fi
    if [ "$launch_mode" = first-run ]; then
        managed_home_operation=initialize
    else
        managed_home_operation=verify
    fi
    managed_codex_home=$("$system_python" "$managed_codex_helper" \
        "$managed_home_operation") || exit $?
fi
case "$managed_codex_home" in
    */codex-home) data_root=${managed_codex_home%/codex-home} ;;
    *) exit 7 ;;
esac
runtime_root=$data_root/runtime
managed_venv=$runtime_root/venv
backend_log=$data_root/custodian-state/backend.log
install_stamp=$runtime_root/installed-commit
readiness=$data_root/custodian-state/backend-readiness.json
evidence_root=$data_root/custodian-state/launcher-evidence
evidence=$evidence_root/$readiness_instance.json
managed_codex_home_id=$("$system_python" -c 'import hashlib,sys; print(hashlib.sha256(sys.argv[1].encode()).hexdigest())' "$managed_codex_home")

mkdir -p "$data_root/custodian-state" "$evidence_root"
chmod 700 "$data_root/custodian-state" "$evidence_root"

if [ ! -x "$managed_venv/bin/python" ]; then
    "$system_python" -m venv "$managed_venv"
fi

current_commit=$("$system_python" -c 'import hashlib,pathlib,sys; root=pathlib.Path(sys.argv[1]).resolve(); selected=[root/"pyproject.toml",*(root/"src").rglob("*.py"),*(root/"scripts").glob("*.py"),*(root/"scripts").glob("*.sh"),*(root/"scripts").glob("*.service")]; digest=hashlib.sha256(); [(digest.update(str(path.relative_to(root)).encode()+b"\0"),digest.update(hashlib.sha256(path.read_bytes()).digest())) for path in sorted(selected) if path.is_file()]; print(digest.hexdigest())' "$project_root")
installed_commit=$(sed -n '1p' "$install_stamp" 2>/dev/null || true)
if [ "$launch_mode" = first-run ] || [ "$current_commit" != "$installed_commit" ]; then
    "$managed_venv/bin/python" -m pip install --disable-pip-version-check "$project_root" >>"$backend_log" 2>&1
    stamp_tmp=$runtime_root/.installed-commit.tmp
    printf '%s\n' "$current_commit" >"$stamp_tmp"
    mv "$stamp_tmp" "$install_stamp"
fi

lifecycle_lock=$runtime_root/custodian-launch.lock
: >"$lifecycle_lock"
chmod 600 "$lifecycle_lock"
exec 9>"$lifecycle_lock"
/usr/bin/flock -x 9

health_matches_any() {
    "$managed_venv/bin/python" -c 'import json,sys,urllib.request; value=json.load(urllib.request.urlopen(sys.argv[1]+"api/health",timeout=1)); instance=value.get("readiness_instance"); raise SystemExit(0 if value.get("application")=="Research Automation Supervisor" and value.get("qualified_commit")==sys.argv[2] and value.get("managed_codex_home_id")==sys.argv[3] and isinstance(instance,str) and len(instance)==64 else 1)' "$url" "$current_commit" "$managed_codex_home_id" >/dev/null 2>&1
}

health_matches_instance() {
    "$managed_venv/bin/python" -c 'import json,sys,urllib.request; value=json.load(urllib.request.urlopen(sys.argv[1]+"api/health",timeout=1)); raise SystemExit(0 if value.get("application")=="Research Automation Supervisor" and value.get("qualified_commit")==sys.argv[2] and value.get("readiness_instance")==sys.argv[3] and value.get("managed_codex_home_id")==sys.argv[4] else 1)' "$url" "$current_commit" "$readiness_instance" "$managed_codex_home_id" >/dev/null 2>&1
}

write_evidence() {
    reused=$1
    observed=$2
    "$managed_venv/bin/python" -c 'import json,os,pathlib,platform,sys,tempfile; destination=pathlib.Path(sys.argv[1]); value={"schema_version":1,"launcher":"Research Supervisor.vbs","windows_execution_path":True,"wsl_backend":True,"wsl_distro":os.environ.get("WSL_DISTRO_NAME",""),"kernel":platform.release(),"backend_reused":sys.argv[2]=="true","requested_readiness_instance":sys.argv[3],"observed_readiness_instance":sys.argv[4],"qualified_commit":sys.argv[5],"url":sys.argv[6],"managed_codex_home_id":sys.argv[7],"browser_open_delegated_to_windows_launcher":True}; descriptor,name=tempfile.mkstemp(prefix=".launcher-evidence.",dir=destination.parent); handle=os.fdopen(descriptor,"w",encoding="utf-8"); json.dump(value,handle,sort_keys=True); handle.write("\n"); handle.flush(); os.fsync(handle.fileno()); handle.close(); os.replace(name,destination); directory=os.open(destination.parent,os.O_RDONLY|getattr(os,"O_DIRECTORY",0)); os.fsync(directory); os.close(directory)' "$evidence" "$reused" "$readiness_instance" "$observed" "$current_commit" "$url" "$managed_codex_home_id"
}

observed_instance() {
    "$managed_venv/bin/python" -c 'import json,sys,urllib.request; print(json.load(urllib.request.urlopen(sys.argv[1]+"api/health",timeout=1))["readiness_instance"])' "$url"
}

if health_matches_any; then
    observed=$(observed_instance)
    write_evidence true "$observed"
    printf 'RAS_LAUNCH_READY|%s|%s|%s\n' "$url" "$readiness_instance" "$evidence"
    exit 0
fi

if [ -n "$acceptance_scenario" ]; then
    acceptance_backend=$project_root/tests/pa5c4_acceptance_backend.py
    if [ ! -f "$acceptance_backend" ] || [ ! -f "$acceptance_scenario" ]; then
        exit 5
    fi
    set -- "$managed_venv/bin/python" "$acceptance_backend" \
        --data-dir "$data_root" --host 127.0.0.1 --port "$port" \
        --readiness-instance "$readiness_instance" \
        --acceptance-scenario "$acceptance_scenario"
else
    if [ ! -S "$core_socket" ] || [ ! -w "$core_socket" ]; then
        echo "The Core Authority Service needs one-time administrator setup." >&2
        exit 6
    fi
    set -- "$managed_venv/bin/research-supervisor-custodian" \
        --data-dir "$data_root" --host 127.0.0.1 --port "$port" \
        --readiness-instance "$readiness_instance" --core-socket "$core_socket"
fi
if ! "$managed_venv/bin/python" -I -m \
    research_automation_supervisor.custodian_lifecycle \
    --data-root "$data_root" \
    --working-directory "$project_root" \
    --backend-log "$backend_log" \
    --codex-home "$managed_codex_home" \
    --qualified-commit "$current_commit" \
    -- "$@"; then
    exit 4
fi

attempt=0
while [ "$attempt" -lt 120 ]; do
    if health_matches_instance; then
        write_evidence false "$readiness_instance"
        printf 'RAS_LAUNCH_READY|%s|%s|%s\n' "$url" "$readiness_instance" "$evidence"
        exit 0
    fi
    attempt=$((attempt + 1))
    sleep 0.25
done

exit 4
