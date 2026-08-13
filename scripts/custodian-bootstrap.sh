#!/bin/sh
set -eu

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
if [ -n "$data_override" ]; then
    data_root=$data_override
else
    data_root=${XDG_DATA_HOME:-"$HOME/.local/share"}/research-automation-supervisor
fi
runtime_root=$data_root/runtime
managed_venv=$runtime_root/venv
backend_log=$data_root/custodian-state/backend.log
install_stamp=$runtime_root/installed-commit
url=http://127.0.0.1:$port/
readiness=$data_root/custodian-state/backend-readiness.json
evidence_root=$data_root/custodian-state/launcher-evidence
evidence=$evidence_root/$readiness_instance.json
core_socket=/run/research-supervisor-core/authority.sock

mkdir -p "$runtime_root" "$data_root/custodian-state" "$evidence_root"
chmod 700 "$data_root" "$runtime_root" "$data_root/custodian-state" "$evidence_root"

if command -v python3 >/dev/null 2>&1; then
    system_python=$(command -v python3)
else
    exit 3
fi

if [ ! -x "$managed_venv/bin/python" ]; then
    "$system_python" -m venv "$managed_venv"
fi

current_commit=$("$system_python" -c 'import hashlib,pathlib,sys; root=pathlib.Path(sys.argv[1]).resolve(); selected=[root/"pyproject.toml",*(root/"src").rglob("*.py"),*(root/"scripts").glob("*.sh"),*(root/"scripts").glob("*.service")]; digest=hashlib.sha256(); [(digest.update(str(path.relative_to(root)).encode()+b"\0"),digest.update(hashlib.sha256(path.read_bytes()).digest())) for path in sorted(selected) if path.is_file()]; print(digest.hexdigest())' "$project_root")
installed_commit=$(sed -n '1p' "$install_stamp" 2>/dev/null || true)
if [ "$launch_mode" = first-run ] || [ "$current_commit" != "$installed_commit" ]; then
    "$managed_venv/bin/python" -m pip install --disable-pip-version-check "$project_root" >>"$backend_log" 2>&1
    stamp_tmp=$runtime_root/.installed-commit.tmp
    printf '%s\n' "$current_commit" >"$stamp_tmp"
    mv "$stamp_tmp" "$install_stamp"
fi

health_matches_any() {
    "$managed_venv/bin/python" -c 'import json,sys,urllib.request; value=json.load(urllib.request.urlopen(sys.argv[1]+"api/health",timeout=1)); instance=value.get("readiness_instance"); raise SystemExit(0 if value.get("application")=="Research Automation Supervisor" and value.get("qualified_commit")==sys.argv[2] and isinstance(instance,str) and len(instance)==64 else 1)' "$url" "$current_commit" >/dev/null 2>&1
}

health_matches_instance() {
    "$managed_venv/bin/python" -c 'import json,sys,urllib.request; value=json.load(urllib.request.urlopen(sys.argv[1]+"api/health",timeout=1)); raise SystemExit(0 if value.get("application")=="Research Automation Supervisor" and value.get("qualified_commit")==sys.argv[2] and value.get("readiness_instance")==sys.argv[3] else 1)' "$url" "$current_commit" "$readiness_instance" >/dev/null 2>&1
}

write_evidence() {
    reused=$1
    observed=$2
    "$managed_venv/bin/python" -c 'import json,os,pathlib,platform,sys,tempfile; destination=pathlib.Path(sys.argv[1]); value={"schema_version":1,"launcher":"Research Supervisor.vbs","windows_execution_path":True,"wsl_backend":True,"wsl_distro":os.environ.get("WSL_DISTRO_NAME",""),"kernel":platform.release(),"backend_reused":sys.argv[2]=="true","requested_readiness_instance":sys.argv[3],"observed_readiness_instance":sys.argv[4],"qualified_commit":sys.argv[5],"url":sys.argv[6],"browser_open_delegated_to_windows_launcher":True}; descriptor,name=tempfile.mkstemp(prefix=".launcher-evidence.",dir=destination.parent); handle=os.fdopen(descriptor,"w",encoding="utf-8"); json.dump(value,handle,sort_keys=True); handle.write("\n"); handle.flush(); os.fsync(handle.fileno()); handle.close(); os.replace(name,destination); directory=os.open(destination.parent,os.O_RDONLY|getattr(os,"O_DIRECTORY",0)); os.fsync(directory); os.close(directory)' "$evidence" "$reused" "$readiness_instance" "$observed" "$current_commit" "$url"
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

old_pid=$("$managed_venv/bin/python" -c 'import json,sys; value=json.load(open(sys.argv[1],encoding="utf-8")); pid=value.get("pid"); print(pid if isinstance(pid,int) and pid>1 else "")' "$readiness" 2>/dev/null || true)
if [ -n "$old_pid" ] && [ -r "/proc/$old_pid/cmdline" ]; then
    old_command=$(tr '\000' ' ' <"/proc/$old_pid/cmdline")
    case "$old_command" in
        *research-supervisor-custodian*) kill -TERM "$old_pid" 2>/dev/null || true ;;
    esac
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
RAS_MANAGED_RUNTIME=1 RAS_QUALIFIED_COMMIT="$current_commit" nohup "$@" \
    >>"$backend_log" 2>&1 </dev/null &

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
