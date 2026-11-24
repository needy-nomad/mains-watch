# Install and enable mains-watch on Windows (Task Scheduler, runs at boot as SYSTEM).
#   .\install.ps1                  install; finish setup in the browser (opens by itself)
#   .\install.ps1 -SensorIp 1.2.3.4   install and skip the browser setup
#   .\install.ps1 -Check           will it work after a restart?
#   .\install.ps1 -Uninstall
# Not administrator? Windows asks for permission (UAC) and restarts this script elevated.
param([string]$SensorIp = "", [switch]$Uninstall, [switch]$Check)
$ErrorActionPreference = "Stop"
$self = $MyInvocation.MyCommand.Path
$here = Split-Path -Parent $self
$dest = Join-Path $env:ProgramFiles "mains-watch"
$data = Join-Path $env:ProgramData "mains-watch"
$conf = Join-Path $data "mains-watch.conf"
$taskName = "mains-watch"
$files = "mains_watch.py", "gpu_guard.py", "dashboard.py", "onboard.py", "netutil.py", "selfcheck.py"

$principal = [Security.Principal.WindowsPrincipal][Security.Principal.WindowsIdentity]::GetCurrent()
if (-not $principal.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)) {
    Write-Host "Administrator rights are needed to run at boot and control the GPU. Windows will ask for permission..."
    $argList = @("-NoProfile", "-ExecutionPolicy", "Bypass", "-File", "`"$self`"")
    if ($SensorIp) { $argList += @("-SensorIp", $SensorIp) }
    if ($Uninstall) { $argList += "-Uninstall" }
    if ($Check) { $argList += "-Check" }
    Start-Process powershell -Verb RunAs -ArgumentList $argList -Wait
    exit
}

function Find-Python {
    $candidates = @()
    $py = Get-Command py -ErrorAction SilentlyContinue
    if ($py) { $candidates += (& py -3 -c "import sys; print(sys.executable)" 2>$null) }
    $p = Get-Command python -ErrorAction SilentlyContinue
    if ($p) { $candidates += $p.Source }
    foreach ($c in $candidates) {
        if ($c -and (Test-Path $c) -and ($c -notmatch "WindowsApps")) { return $c }
    }
    throw "Python 3.8+ not found. Install it from python.org (tick 'Install for all users') and run this again."
}

try {
    if ($Check) {
        & (Find-Python) (Join-Path $dest "mains_watch.py") --check
        return
    }

    if ($Uninstall) {
        $py = Find-Python
        if (Test-Path (Join-Path $dest "mains_watch.py")) { & $py (Join-Path $dest "mains_watch.py") --release }
        Stop-ScheduledTask -TaskName $taskName -ErrorAction SilentlyContinue
        Unregister-ScheduledTask -TaskName $taskName -Confirm:$false -ErrorAction SilentlyContinue
        Remove-Item -Recurse -Force $dest -ErrorAction SilentlyContinue
        Remove-Item -Force (Join-Path $env:ProgramData "Microsoft\Windows\Start Menu\Programs\Mains Watch.url") -ErrorAction SilentlyContinue
        Write-Host "Removed. Config and history in $data were kept."
        return
    }

    $python = Find-Python
    $pythonw = Join-Path (Split-Path $python) "pythonw.exe"
    if (-not (Test-Path $pythonw)) { $pythonw = $python }
    if ($python -match "\\Users\\") {
        throw "Python is installed for one user only ($python). The task runs as SYSTEM and cannot use it. Reinstall Python for all users."
    }
    & $python -c "import sys; sys.exit(sys.version_info < (3, 8))"
    if ($LASTEXITCODE -ne 0) { throw "Python 3.8+ is needed." }

    New-Item -ItemType Directory -Force $dest, $data | Out-Null
    foreach ($f in $files) { Copy-Item (Join-Path $here $f) $dest -Force }

    $lines = @()
    if (Test-Path $conf) { $lines = @(Get-Content $conf) }
    function Set-Key($key, $value) {
        $found = $false
        $script:lines = @($script:lines | ForEach-Object {
            if ($_ -match "^\s*$key\s*=") { $found = $true; "$key=$value" } else { $_ }
        })
        if (-not $found) { $script:lines += "$key=$value" }
    }
    if ($SensorIp) {
        if ($SensorIp -notmatch '^\d{1,3}(\.\d{1,3}){3}$') { throw "Not an IPv4 address: $SensorIp" }
        Set-Key "SENSOR_IP" $SensorIp
    }
    Set-Key "LOG_FILE" (Join-Path $data "mains-watch.log")   # pythonw has no console
    Set-Content -Path $conf -Value $lines

    $action = New-ScheduledTaskAction -Execute $pythonw -Argument "`"$dest\mains_watch.py`"" -WorkingDirectory $dest
    $trigger = New-ScheduledTaskTrigger -AtStartup
    $taskPrincipal = New-ScheduledTaskPrincipal -UserId "SYSTEM" -LogonType ServiceAccount -RunLevel Highest
    $settings = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries `
        -StartWhenAvailable -RestartCount 999 -RestartInterval (New-TimeSpan -Minutes 1) `
        -ExecutionTimeLimit ([TimeSpan]::Zero) -MultipleInstances IgnoreNew
    Stop-ScheduledTask -TaskName $taskName -ErrorAction SilentlyContinue
    Register-ScheduledTask -TaskName $taskName -Action $action -Trigger $trigger -Principal $taskPrincipal `
        -Settings $settings -Description "Halts the GPU on mains loss, resumes when power returns" -Force | Out-Null
    Start-ScheduledTask -TaskName $taskName

    $url = "http://127.0.0.1:8099"
    Set-Content -Path (Join-Path $env:ProgramData "Microsoft\Windows\Start Menu\Programs\Mains Watch.url") `
        -Value "[InternetShortcut]`r`nURL=$url"
    Start-Sleep -Seconds 4
    Write-Host "`n---- restart check ----"
    & $python (Join-Path $dest "mains_watch.py") --check
    Write-Host "`nDashboard / setup: $url  (also 'Mains Watch' in the Start menu)"
    if (-not (Select-String -Path $conf -Pattern '^SENSOR_IP=.+' -Quiet)) { Start-Process $url }
}
catch {
    Write-Host "ERROR: $($_.Exception.Message)" -ForegroundColor Red
}
if ([Environment]::UserInteractive) { Read-Host "`nPress Enter to close" | Out-Null }
