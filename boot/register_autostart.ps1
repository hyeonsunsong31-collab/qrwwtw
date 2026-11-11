<#
  register_autostart.ps1 -- start JARVIS automatically at every logon (requirements 2, 4).

  Creates the Scheduled Task "JARVIS", which runs the supervisor (boot\jarvis_service.py)
  with pythonw.exe (no console window) as soon as the user logs on, restarts it if it
  fails, and never times out. This is what makes JARVIS keep working once installed.

  The task is registered for the person logged on at the console. When the installer is
  elevated with a different administrator account, $env:USERNAME would be that admin, so
  the console user is looked up instead.

  Usage:
    powershell -ExecutionPolicy Bypass -File register_autostart.ps1              # register
    powershell -ExecutionPolicy Bypass -File register_autostart.ps1 -Unregister  # remove
#>
param([switch]$Unregister)

$ErrorActionPreference = "Stop"
$TaskName = "JARVIS"
$Root = Split-Path -Parent (Split-Path -Parent $MyInvocation.MyCommand.Path)

if ($Unregister) {
    if (Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue) {
        Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false
        Write-Host "JARVIS auto-start removed."
    } else {
        Write-Host "JARVIS auto-start was not registered."
    }
    return
}

$User = (Get-CimInstance Win32_ComputerSystem).UserName
if (-not $User) { $User = "$env:USERDOMAIN\$env:USERNAME" }

$Pythonw = Join-Path $Root ".venv\Scripts\pythonw.exe"
if (-not (Test-Path $Pythonw)) {
    $cmd = Get-Command pythonw.exe -ErrorAction SilentlyContinue
    if ($cmd) { $Pythonw = $cmd.Source } else { throw "pythonw.exe not found - run INSTALL_JARVIS.bat first." }
}
$Script = Join-Path $Root "boot\jarvis_service.py"

$Action    = New-ScheduledTaskAction -Execute $Pythonw -Argument "`"$Script`"" -WorkingDirectory $Root
$Trigger   = New-ScheduledTaskTrigger -AtLogOn -User $User
$Principal = New-ScheduledTaskPrincipal -UserId $User -LogonType Interactive -RunLevel Limited
$Settings  = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries `
             -StartWhenAvailable -MultipleInstances IgnoreNew `
             -RestartCount 3 -RestartInterval (New-TimeSpan -Minutes 1) `
             -ExecutionTimeLimit ([TimeSpan]::Zero)

Register-ScheduledTask -TaskName $TaskName -Action $Action -Trigger $Trigger `
    -Principal $Principal -Settings $Settings -Force `
    -Description "JARVIS local agentic AI - always-on background assistant." | Out-Null

Write-Host "JARVIS will start automatically when $User logs on."
