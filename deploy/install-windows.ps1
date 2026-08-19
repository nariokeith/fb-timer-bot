# Keep the bots running on a Windows PC, by keeping WSL2 running.
#
#   Right-click -> Run with PowerShell, or:
#   powershell -ExecutionPolicy Bypass -File deploy\install-windows.ps1
#   powershell -ExecutionPolicy Bypass -File deploy\install-windows.ps1 -Stop
#
# The bots do NOT run natively on Windows: bot.py calls time.tzset(),
# which Python documents as Unix-only, so the timer would crash on
# import. They run inside WSL2, where deploy/setup.sh and the systemd
# unit apply unchanged -- one deployment path instead of two, the second
# of which nobody would ever run.
#
# This script's whole job is therefore to make sure the distro is awake.
# systemd inside it starts the supervisor; the supervisor starts the
# bots. WSL2 does not boot on its own, so a logon task nudges it.
#
# Idempotent: re-running re-registers the task cleanly.

param([switch]$Stop)

$ErrorActionPreference = 'Stop'

$Distro   = 'Ubuntu-24.04'
$TaskName = 'fb-timer-bot-wsl'

if ($Stop) {
    Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false -ErrorAction SilentlyContinue
    wsl.exe --terminate $Distro 2>$null
    Write-Host "Removed the autostart task and shut $Distro down."
    Write-Host "The bots stay installed inside WSL; re-run this script to start again."
    exit 0
}

if (-not (Get-Command wsl.exe -ErrorAction SilentlyContinue)) {
    Write-Error "WSL is not installed. Run:  wsl --install -d $Distro   then reboot."
}

$installed = (wsl.exe --list --quiet) -replace "`0", "" | ForEach-Object { $_.Trim() }
if ($installed -notcontains $Distro) {
    Write-Error "$Distro is not installed. Run:  wsl --install -d $Distro   then reboot."
}

# systemd is off by default in WSL2, and without it `systemctl enable`
# in setup.sh has nothing to enable.
# try/catch, not just 2>$null: PowerShell 7.3+ can be configured to turn
# a failing native command into a terminating error, and a missing
# /etc/wsl.conf is the normal first-run case rather than a fault.
$conf = ''
try   { $conf = (wsl.exe -d $Distro -u root -- cat /etc/wsl.conf 2>$null) -join "`n" }
catch { $conf = '' }
if ($conf -notmatch 'systemd\s*=\s*true') {
    Write-Warning "systemd is not enabled in $Distro. Inside WSL, run:"
    Write-Warning "  printf '[boot]\nsystemd=true\n' | sudo tee /etc/wsl.conf"
    Write-Warning "then, back in PowerShell:  wsl --shutdown"
    Write-Warning "Re-run this script afterwards."
    exit 1
}

# Starting the distro is enough: systemd comes up with it and starts the
# service that setup.sh enabled. Running /bin/true keeps this from
# holding a process open -- the WSL VM stays up on its own once systemd
# has services running in it.
$action  = New-ScheduledTaskAction -Execute 'wsl.exe' `
    -Argument "-d $Distro -u root -- /bin/true"
$trigger = New-ScheduledTaskTrigger -AtLogOn
# Battery settings matter on a laptop, and StartWhenAvailable catches a
# logon that happened while the machine was still settling.
$settings = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries `
    -DontStopIfGoingOnBatteries -StartWhenAvailable

Register-ScheduledTask -TaskName $TaskName -Action $action -Trigger $trigger `
    -Settings $settings -Description 'Start WSL so the Lordnine bots run' -Force | Out-Null

Start-ScheduledTask -TaskName $TaskName

Write-Host ""
Write-Host "Registered '$TaskName' to start $Distro at logon."
Write-Host ""
Write-Host "Check the bots with:"
Write-Host "  wsl -d $Distro -- journalctl -u fb-timer-bot -f"
Write-Host ""
Write-Host "If that says the unit does not exist, you have not run setup.sh yet:"
Write-Host "  wsl -d $Distro"
Write-Host "  sudo bash /opt/fb-timer-bot/deploy/setup.sh"
