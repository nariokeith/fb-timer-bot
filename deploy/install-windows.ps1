# Install the Lordnine bots on a Windows PC. Run INSTALL.bat, not this.
#
# Runs the bots NATIVELY -- no WSL, no admin, no reboot. WSL would have
# meant an elevated PowerShell, a reboot and a second run, which is more
# than the person doing this can be asked to carry and more than they can
# be talked through when nobody can see their screen.
#
# What makes native Windows possible: bot.py used to force Asia/Manila
# with time.tzset(), which does not exist on Windows. Times are now
# anchored to BOT_TZ at each conversion with ZoneInfo instead, so the
# host's own clock no longer decides when a boss spawned.
#
# Idempotent: re-running repairs an install rather than duplicating it.

$ErrorActionPreference = 'Stop'

$Root    = $PSScriptRoot
$AppDir  = Join-Path $env:LOCALAPPDATA 'fb-timer-bot'
$EnvFile = Join-Path $Root '.env'
$Task    = 'fb-timer-bot'
$Zip     = 'https://github.com/nariokeith/fb-timer-bot/archive/refs/heads/main.zip'

# Everything this prints lands in a file the operator can send back.
# Nobody here can see this machine, and the person running it cannot be
# asked to read a stack trace off the screen before it scrolls away.
Start-Transcript -Path (Join-Path $Root 'install-log.txt') -Force | Out-Null

function Say($m) { Write-Host "  $m" }

try {
    Write-Host ""
    Write-Host "Installing the Lordnine bots..."
    Write-Host ""

    # -- 1. Credentials, before anything is downloaded -------------------
    # Without them the bots exit 78 and the supervisor leaves them
    # stopped, which reads as a crash rather than a missing step.
    if (-not (Test-Path $EnvFile)) {
        Write-Host "STOP: no .env file found next to this installer." -ForegroundColor Red
        Write-Host ""
        Write-Host "  Expected: $EnvFile"
        Write-Host "  Ask whoever sent you this for it, put it in this"
        Write-Host "  folder, and run INSTALL.bat again."
        exit 1
    }
    Say ".env found"

    # -- 2. Python ------------------------------------------------------
    # 3.13 specifically: requirements.txt pins audioop-lts, which
    # declares requires-python >= 3.13 (audioop left the standard
    # library in 3.13 and this is the backport discord.py needs).
    $py = $null
    foreach ($c in @('py -3.13', 'python3.13', 'python')) {
        $exe, $arg = $c.Split(' ', 2)
        if (Get-Command $exe -ErrorAction SilentlyContinue) {
            $v = & $exe $arg --version 2>$null
            if ($v -match 'Python 3\.(1[3-9]|[2-9]\d)') { $py = $c; break }
        }
    }

    if (-not $py) {
        Say "Python 3.13 not found; installing it (this takes a few minutes)"
        if (Get-Command winget -ErrorAction SilentlyContinue) {
            winget install -e --id Python.Python.3.13 --scope user `
                --accept-package-agreements --accept-source-agreements | Out-Null
        } else {
            $inst = Join-Path $env:TEMP 'python-3.13.exe'
            Invoke-WebRequest -UseBasicParsing -OutFile $inst `
                'https://www.python.org/ftp/python/3.13.4/python-3.13.4-amd64.exe'
            # Per-user install so this never needs an administrator.
            Start-Process -Wait $inst -ArgumentList `
                '/quiet','InstallAllUsers=0','PrependPath=1','Include_pip=1'
        }
        $env:Path = [Environment]::GetEnvironmentVariable('Path', 'User') + ';' + $env:Path
        $py = 'py -3.13'
        if (-not (& py -3.13 --version 2>$null)) {
            Write-Host "STOP: Python installed but is not on PATH yet." -ForegroundColor Red
            Write-Host "  Restart the PC and run INSTALL.bat again."
            exit 1
        }
    }
    Say "Python: $(& ($py.Split(' ')[0]) ($py.Split(' ')[1]) --version)"

    # -- 3. The code ----------------------------------------------------
    # A zip rather than git, so the PC needs nothing installed for this.
    Say "Downloading the bots"
    $tmpZip = Join-Path $env:TEMP 'fb-timer-bot.zip'
    Invoke-WebRequest -UseBasicParsing -OutFile $tmpZip $Zip
    $staging = Join-Path $env:TEMP 'fb-timer-bot-unzip'
    if (Test-Path $staging) { Remove-Item -Recurse -Force $staging }
    Expand-Archive -Path $tmpZip -DestinationPath $staging -Force

    if (Test-Path $AppDir) { Remove-Item -Recurse -Force $AppDir }
    New-Item -ItemType Directory -Force -Path $AppDir | Out-Null
    Copy-Item -Recurse -Force `
        (Join-Path (Get-ChildItem $staging -Directory)[0].FullName '*') $AppDir
    Say "Installed to $AppDir"

    # -- 4. Dependencies -------------------------------------------------
    Say "Installing dependencies (a few minutes the first time)"
    $exe, $arg = $py.Split(' ', 2)
    & $exe $arg -m venv (Join-Path $AppDir '.venv')
    $venvPy = Join-Path $AppDir '.venv\Scripts\python.exe'
    & $venvPy -m pip install --quiet --upgrade pip
    & $venvPy -m pip install --quiet -r (Join-Path $AppDir 'requirements.txt')

    Copy-Item -Force $EnvFile (Join-Path $AppDir '.env')
    Say "Credentials copied in"

    # -- 5. Autostart ----------------------------------------------------
    # At logon rather than at startup: a per-user task needs a session,
    # and this deliberately never asks for an administrator.
    $action = New-ScheduledTaskAction -Execute $venvPy `
        -Argument '-u supervisor.py' -WorkingDirectory $AppDir
    $trigger = New-ScheduledTaskTrigger -AtLogOn
    $settings = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries `
        -DontStopIfGoingOnBatteries -StartWhenAvailable `
        -ExecutionTimeLimit ([TimeSpan]::Zero) -RestartInterval (New-TimeSpan -Minutes 1) `
        -RestartCount 999
    Register-ScheduledTask -TaskName $Task -Action $action -Trigger $trigger `
        -Settings $settings -Force `
        -Description 'Lordnine field boss timer, attendance and item bots' | Out-Null
    Say "Set to start automatically when you log in"

    # -- 6. Stop the PC sleeping ----------------------------------------
    # A sleeping PC runs no bots, and Windows sleeps by default. Display
    # sleep is left alone -- only the machine has to stay awake.
    powercfg /change standby-timeout-ac 0 2>$null
    powercfg /change hibernate-timeout-ac 0 2>$null
    Say "Sleep disabled while plugged in"

    Restart-ScheduledTask -TaskName $Task -ErrorAction SilentlyContinue
    Start-ScheduledTask -TaskName $Task
    Start-Sleep -Seconds 20

    Write-Host ""
    if ((Get-ScheduledTask -TaskName $Task).State -eq 'Running') {
        Write-Host "  DONE - the bots are running." -ForegroundColor Green
        Write-Host ""
        Write-Host "  Leave this PC on and plugged in. They start again on"
        Write-Host "  their own whenever you log in."
    } else {
        Write-Host "  Installed, but they are not running yet." -ForegroundColor Yellow
        Write-Host "  Send install-log.txt back to whoever gave you this."
    }
    Write-Host ""
}
catch {
    Write-Host ""
    Write-Host "  FAILED: $($_.Exception.Message)" -ForegroundColor Red
    Write-Host "  Send install-log.txt back to whoever gave you this."
    Write-Host ""
}
finally {
    Stop-Transcript | Out-Null
}
