# Install the Lordnine bots on a Windows PC. Run INSTALL.bat, not this.
#
# Runs the bots NATIVELY -- no WSL, no administrator, no reboot. WSL would
# have meant an elevated PowerShell, a reboot and a second run, which is
# more than the person doing this can be asked to carry and more than
# anyone can talk them through without seeing their screen.
#
# What makes native Windows possible: bot.py used to force Asia/Manila
# with time.tzset(), which does not exist on Windows. Times are anchored
# to BOT_TZ at each conversion with ZoneInfo now, so the host's own clock
# no longer decides when a boss spawned.
#
# Idempotent: re-running repairs an install rather than duplicating it,
# and is also how the bots are updated.

$ErrorActionPreference = 'Stop'

$Root    = $PSScriptRoot
$AppDir  = Join-Path $env:LOCALAPPDATA 'fb-timer-bot'
$EnvFile = Join-Path $Root '.env'
$Task    = 'fb-timer-bot'
$Zip     = 'https://github.com/nariokeith/fb-timer-bot/archive/refs/heads/main.zip'

# Every credential the three bots refuse to start without. Missing any
# one means exit 78 and three bots left stopped, which reads on the far
# end as "the installer just did nothing".
$Required = @(
    'DISCORD_TOKEN',
    'ATTENDANCE_DISCORD_TOKEN',
    'ITEMS_DISCORD_TOKEN',
    'SHEET_ID',
    'ITEMS_SHEET_ID',
    'GOOGLE_SERVICE_ACCOUNT_JSON',
    'GEMINI_API_KEY'
)

# Everything printed here also lands in a file that can be sent back.
# Nobody else can see this machine, and the person at it cannot be asked
# to read a stack trace off a console before it scrolls away.
Start-Transcript -Path (Join-Path $Root 'install-log.txt') -Force | Out-Null

function Say  ($m) { Write-Host "  $m" }
function Halt ($m) {
    Write-Host ""
    Write-Host "  STOPPED: $m" -ForegroundColor Red
    Write-Host "  Send install-log.txt back to whoever gave you this."
    Write-Host ""
    Stop-Transcript | Out-Null
    exit 1
}

try {
    # Windows PowerShell 5.1 can still default to TLS 1.0, which GitHub
    # refuses outright.
    [Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12

    Write-Host ""
    Write-Host "Installing the Lordnine bots..."
    Write-Host ""

    # -- 1. Credentials, before anything is downloaded ------------------
    if (-not (Test-Path $EnvFile)) {
        Halt "no .env file next to this installer.`n  Expected: $EnvFile`n  Ask whoever sent you this for it, put it in this folder, and run INSTALL.bat again."
    }

    $envMap = @{}
    foreach ($line in Get-Content $EnvFile) {
        if ($line -match '^\s*([A-Z_][A-Z0-9_]*)\s*=\s*(.*)$') {
            $envMap[$Matches[1]] = $Matches[2].Trim()
        }
    }
    $absent = $Required | Where-Object { -not $envMap[$_] }
    if ($absent) { Halt "the .env file is missing: $($absent -join ', ')" }

    # The single most expensive mistake available here: a service account
    # key pasted across several lines is not valid JSON, and the bots
    # only say so at runtime, in a log nobody is watching.
    try   { $envMap['GOOGLE_SERVICE_ACCOUNT_JSON'] | ConvertFrom-Json | Out-Null }
    catch { Halt "GOOGLE_SERVICE_ACCOUNT_JSON is not valid JSON. It must be the whole key on ONE line." }
    Say "Credentials look complete"

    # -- 2. Python ------------------------------------------------------
    # 3.13 specifically: requirements.txt pins audioop-lts, which declares
    # requires-python >= 3.13 (audioop left the standard library in 3.13,
    # and this is the backport discord.py needs).
    #
    # Candidates are argument ARRAYS, not strings: splitting "python" on a
    # space yields one element, and passing the missing second element
    # through & sends an empty argument that python then rejects.
    # Select-Object -Skip 1, never $cand[1..($cand.Count-1)]: for a
    # one-element array that index is the DESCENDING range 1..0, which
    # returns $null and then the command's own name -- so `python` became
    # `python python --version` and detection silently failed.
    $pyExe = $null
    $pyArgs = @()
    foreach ($cand in @(@('py', '-3.13'), @('python3.13'), @('python'))) {
        if (-not (Get-Command $cand[0] -ErrorAction SilentlyContinue)) { continue }
        $rest = @($cand | Select-Object -Skip 1)
        $v = & $cand[0] @rest --version 2>$null
        if ($LASTEXITCODE -eq 0 -and $v -match 'Python 3\.(1[3-9]|[2-9]\d)') {
            $pyExe = $cand[0]
            $pyArgs = $rest
            break
        }
    }

    if (-not $pyExe) {
        Say "Python 3.13 not found; installing it (a few minutes)"
        if (Get-Command winget -ErrorAction SilentlyContinue) {
            winget install -e --id Python.Python.3.13 --scope user `
                --accept-package-agreements --accept-source-agreements | Out-Null
        } else {
            $inst = Join-Path $env:TEMP 'python-3.13.exe'
            Invoke-WebRequest -UseBasicParsing -OutFile $inst `
                'https://www.python.org/ftp/python/3.13.4/python-3.13.4-amd64.exe'
            # Per-user, so this never needs an administrator.
            Start-Process -Wait $inst -ArgumentList `
                '/quiet','InstallAllUsers=0','PrependPath=1','Include_pip=1'
        }
        $env:Path = [Environment]::GetEnvironmentVariable('Path', 'User') + ';' + $env:Path
        if (Get-Command py -ErrorAction SilentlyContinue) { $pyExe = 'py'; $pyArgs = @('-3.13') }
        elseif (Get-Command python -ErrorAction SilentlyContinue) { $pyExe = 'python'; $pyArgs = @() }
        else { Halt "Python installed but is not on PATH yet. Restart the PC and run INSTALL.bat again." }
    }
    Say "Python: $(& $pyExe @pyArgs --version)"

    # -- 3. The code ----------------------------------------------------
    # A zip rather than git, so nothing else has to be installed first.
    Say "Downloading the bots"
    $tmpZip = Join-Path $env:TEMP 'fb-timer-bot.zip'
    Invoke-WebRequest -UseBasicParsing -OutFile $tmpZip $Zip
    $staging = Join-Path $env:TEMP 'fb-timer-bot-unzip'
    if (Test-Path $staging) { Remove-Item -Recurse -Force $staging }
    Expand-Archive -Path $tmpZip -DestinationPath $staging -Force
    $unpacked = @(Get-ChildItem $staging -Directory)[0]
    if (-not $unpacked) { Halt "the download did not unpack as expected." }

    # Stop a running copy first: on Windows an executable cannot be
    # replaced while it is open, and a re-run is the update path.
    Stop-ScheduledTask -TaskName $Task -ErrorAction SilentlyContinue

    if (Test-Path $AppDir) { Remove-Item -Recurse -Force $AppDir -ErrorAction SilentlyContinue }
    New-Item -ItemType Directory -Force -Path $AppDir | Out-Null
    Copy-Item -Recurse -Force (Join-Path $unpacked.FullName '*') $AppDir
    Say "Installed to $AppDir"

    # -- 4. Dependencies -------------------------------------------------
    Say "Installing dependencies (several minutes the first time)"
    & $pyExe @pyArgs -m venv (Join-Path $AppDir '.venv')
    $venvPy = Join-Path $AppDir '.venv\Scripts\python.exe'
    if (-not (Test-Path $venvPy)) { Halt "could not build the Python environment." }
    & $venvPy -m pip install --quiet --upgrade pip
    & $venvPy -m pip install --quiet -r (Join-Path $AppDir 'requirements.txt')
    if ($LASTEXITCODE -ne 0) { Halt "installing dependencies failed." }

    Copy-Item -Force $EnvFile (Join-Path $AppDir '.env')
    Say "Credentials copied in"

    # -- 5. Autostart ----------------------------------------------------
    # At logon rather than at startup, because a per-user task needs a
    # session -- and this deliberately never asks for an administrator.
    $action = New-ScheduledTaskAction -Execute $venvPy `
        -Argument '-u supervisor.py' -WorkingDirectory $AppDir
    $trigger = New-ScheduledTaskTrigger -AtLogOn
    # ExecutionTimeLimit zero: without it Task Scheduler kills the bots
    # after three days, which would look like a random outage.
    $settings = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries `
        -DontStopIfGoingOnBatteries -StartWhenAvailable `
        -ExecutionTimeLimit ([TimeSpan]::Zero)
    Register-ScheduledTask -TaskName $Task -Action $action -Trigger $trigger `
        -Settings $settings -Force `
        -Description 'Lordnine field boss timer, attendance and item bots' | Out-Null
    Say "Set to start automatically at every logon"

    # -- 6. Try to stop the PC sleeping ---------------------------------
    # Reported honestly: powercfg can be refused by policy or by lack of
    # elevation, and claiming success when the machine still sleeps would
    # send someone hunting for a bug that is really a power setting.
    powercfg /change standby-timeout-ac 0 2>&1 | Out-Null
    $sleepOk = ($LASTEXITCODE -eq 0)
    powercfg /change hibernate-timeout-ac 0 2>&1 | Out-Null
    if ($sleepOk) {
        Say "Sleep turned off while plugged in"
    } else {
        Say "COULD NOT turn sleep off -- set Settings > System > Power to Never"
    }

    Start-ScheduledTask -TaskName $Task
    Start-Sleep -Seconds 25

    $state = (Get-ScheduledTask -TaskName $Task).State
    Write-Host ""
    if ($state -eq 'Running') {
        Write-Host "  DONE - the bots are running." -ForegroundColor Green
        Write-Host ""
        Write-Host "  Leave this PC on and plugged in. They start again on"
        Write-Host "  their own every time you log in."
        Write-Host ""
        Write-Host "  Check in Discord that all three bots show as online."
    } else {
        Write-Host "  Installed, but they are not running (state: $state)." -ForegroundColor Yellow
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
    Stop-Transcript -ErrorAction SilentlyContinue | Out-Null
}
