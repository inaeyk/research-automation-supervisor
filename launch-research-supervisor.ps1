param(
    [switch]$FirstRun,
    [string]$DataRoot = "",
    [int]$Port = 8765,
    [string]$AcceptanceScenario = "",
    [string]$AcceptanceBrowserPath = "",
    [int]$AcceptanceBrowserDebugPort = 0,
    [string]$AcceptanceBrowserDebugAddress = "127.0.0.1",
    [int]$AcceptanceBrowserRelayPort = 0,
    [string]$AcceptanceBrowserProfile = "",
    [string]$WslExecutable = "wsl.exe",
    [string]$FailureEvidence = ""
)
$ErrorActionPreference = "Stop"

function Show-SetupMessage([string]$Title, [string]$Message) {
    if ($FailureEvidence) {
        @{ title = $Title; message = $Message } | ConvertTo-Json | Set-Content -LiteralPath $FailureEvidence -Encoding UTF8
        return
    }
    Add-Type -AssemblyName PresentationFramework
    [System.Windows.MessageBox]::Show(
        $Message,
        $Title,
        [System.Windows.MessageBoxButton]::OK,
        [System.Windows.MessageBoxImage]::Information
    ) | Out-Null
}

function Convert-ToWslPath([string]$WindowsPath, [string]$Label) {
    if ($WindowsPath -match '^\\\\wsl(?:\.localhost|\$)\\[^\\]+(?<LinuxPath>\\.*)$') {
        return $Matches['LinuxPath'].Replace('\', '/')
    }
    $Converted = (& $WslExecutable wslpath -u -- $WindowsPath 2>&1 | Out-String).Trim()
    if ($LASTEXITCODE -ne 0 -or -not $Converted) {
        throw "$Label could not be mapped into WSL."
    }
    return $Converted
}

try {
    $null = & $WslExecutable --status 2>&1
    if ($LASTEXITCODE -ne 0) {
        Show-SetupMessage "WSL setup needed" "Windows Subsystem for Linux is not ready. Install or enable WSL, then double-click this launcher again. No campaign was started."
        exit 2
    }
    $Distros = @(& $WslExecutable --list --quiet 2>$null | Where-Object { $_.Trim().Length -gt 0 })
    if ($LASTEXITCODE -ne 0 -or $Distros.Count -eq 0) {
        Show-SetupMessage "WSL setup needed" "No supported WSL Linux installation was found. Install WSL, then double-click Research Supervisor again. No campaign was started."
        exit 2
    }
    $ProjectRoot = $PSScriptRoot
    if (-not $ProjectRoot) {
        throw "The trusted launcher folder could not be identified."
    }
    $Mode = if ($FirstRun) { "first-run" } else { "normal" }
    $ReadinessInstance = ([System.Guid]::NewGuid().ToString("N") + [System.Guid]::NewGuid().ToString("N"))
    $LinuxProjectRoot = Convert-ToWslPath $ProjectRoot "The trusted launcher folder"
    $LinuxDataRoot = ""
    if ($DataRoot) {
        $LinuxDataRoot = Convert-ToWslPath $DataRoot "The application data folder"
    }
    $LinuxScenario = ""
    if ($AcceptanceScenario) {
        $LinuxScenario = Convert-ToWslPath $AcceptanceScenario "The qualification scenario"
    }
    $Bootstrap = "$LinuxProjectRoot/scripts/custodian-bootstrap.sh"
    $Result = @(& $WslExecutable --exec /bin/sh $Bootstrap $LinuxProjectRoot $Mode $ReadinessInstance $LinuxDataRoot $LinuxScenario $Port 2>&1)
    if ($LASTEXITCODE -ne 0) {
        $Detail = ($Result -join "`n")
        Show-SetupMessage "Research Supervisor needs attention" "The WSL backend could not start safely. Try Research Supervisor again or ask your administrator for help. No scientific campaign state changed.`n`nTechnical detail: $Detail"
        exit $LASTEXITCODE
    }
    $Ready = $Result | Where-Object { $_ -like "RAS_LAUNCH_READY|*" } | Select-Object -Last 1
    if (-not $Ready) {
        Show-SetupMessage "Research Supervisor needs attention" "The local application did not provide verified readiness. No scientific campaign state changed."
        exit 4
    }
    $Parts = $Ready.Split('|')
    if ($Parts.Count -ne 4 -or $Parts[2] -ne $ReadinessInstance) {
        Show-SetupMessage "Research Supervisor needs attention" "The local application readiness identity did not match this launch. No scientific campaign state changed."
        exit 4
    }
    $Url = $Parts[1]
    if ($AcceptanceBrowserPath) {
        if (-not (Test-Path -LiteralPath $AcceptanceBrowserPath -PathType Leaf)) {
            Show-SetupMessage "Browser setup needed" "The acceptance browser executable is unavailable. No campaign was started."
            exit 5
        }
        if ($AcceptanceBrowserDebugPort -le 0 -or -not $AcceptanceBrowserProfile) {
            Show-SetupMessage "Browser setup needed" "The acceptance browser configuration is incomplete. No campaign was started."
            exit 5
        }
        if ($AcceptanceBrowserRelayPort -gt 0) {
            $Relay = Join-Path $PSScriptRoot "scripts\windows-cdp-relay.ps1"
            Start-Process -WindowStyle Hidden -FilePath "powershell.exe" -ArgumentList @(
                "-NoProfile",
                "-ExecutionPolicy", "Bypass",
                "-File", $Relay,
                "-ListenAddress", $AcceptanceBrowserDebugAddress,
                "-ListenPort", $AcceptanceBrowserRelayPort,
                "-TargetPort", $AcceptanceBrowserDebugPort
            )
        }
        New-Item -ItemType Directory -Force -Path $AcceptanceBrowserProfile | Out-Null
        Start-Process -FilePath $AcceptanceBrowserPath -ArgumentList @(
            "--remote-debugging-address=127.0.0.1",
            "--remote-debugging-port=$AcceptanceBrowserDebugPort",
            "--user-data-dir=$AcceptanceBrowserProfile",
            "--no-first-run",
            "--no-default-browser-check",
            $Url
        )
    } else {
        Start-Process $Url
    }
} catch {
    Show-SetupMessage "Research Supervisor needs attention" "The local application could not be launched through WSL. No scientific campaign state changed.`n`nTechnical detail: $($_.Exception.Message)"
    exit 2
}
