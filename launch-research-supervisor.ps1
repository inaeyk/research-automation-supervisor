param([switch]$FirstRun)
$ErrorActionPreference = "Stop"

function Show-SetupMessage([string]$Title, [string]$Message) {
    Add-Type -AssemblyName PresentationFramework
    [System.Windows.MessageBox]::Show(
        $Message,
        $Title,
        [System.Windows.MessageBoxButton]::OK,
        [System.Windows.MessageBoxImage]::Information
    ) | Out-Null
}

try {
    $null = & wsl.exe --status 2>&1
    if ($LASTEXITCODE -ne 0) {
        Show-SetupMessage "WSL setup needed" "Windows Subsystem for Linux is not ready. Install or enable WSL, then double-click this launcher again. No campaign was started."
        exit 2
    }
    $ProjectRoot = Split-Path -Parent $MyInvocation.MyCommand.Path
    $Mode = if ($FirstRun) { "first-run" } else { "normal" }
    & wsl.exe sh -lc 'project_root=$(wslpath -u "$1"); exec sh "$project_root/scripts/custodian-bootstrap.sh" "$project_root" "$2"' sh $ProjectRoot $Mode
    if ($LASTEXITCODE -ne 0) {
        Show-SetupMessage "Research Supervisor needs attention" "Automatic setup could not finish. Open Technical details from the launcher folder or ask your administrator for help. No scientific campaign state changed."
        exit $LASTEXITCODE
    }
} catch {
    Show-SetupMessage "Research Supervisor needs attention" "The local application could not be launched. No scientific campaign state changed."
    exit 2
}
