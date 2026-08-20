# Install the Lordnine bots on a Windows PC. Run INSTALL.bat, not this.
#
# Nothing has to be set up on the host first. No administrator, no
# reboot, no PATH changes, no Python to install, no WSL. Everything --
# interpreter, packages, code -- lands in one folder under the user's own
# profile and is deleted by removing it.
#
# Python comes from the EMBEDDABLE distribution: a zip that runs where it
# is unpacked. Installing Python properly was the biggest failure point
# in this script, because it wants winget or a downloaded installer,
# rewrites PATH, and can need a reboot before the new PATH is visible --
# none of which someone can be walked through by message.
#
# Every dependency resolves to a prebuilt win_amd64 cp313 wheel, verified
# with `pip download --platform win_amd64 --only-binary=:all:`, so no
# compiler is needed either.
#
# Idempotent: re-running repairs an install rather than duplicating it,
# and is also how the bots are updated.

$ErrorActionPreference = 'Stop'

$Root    = $PSScriptRoot
$AppDir  = Join-Path $env:LOCALAPPDATA 'fb-timer-bot'
$PyDir   = Join-Path $AppDir 'python'
$CodeDir = Join-Path $AppDir 'app'
$EnvFile = Join-Path $Root '.env'
$Task    = 'fb-timer-bot'

$PyZip  = 'https://www.python.org/ftp/python/3.13.4/python-3.13.4-embed-amd64.zip'
$GetPip = 'https://bootstrap.pypa.io/get-pip.py'
$Code   = 'https://github.com/nariokeith/fb-timer-bot/archive/refs/heads/main.zip'

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
function Fetch ($url, $dest) {
    # UseBasicParsing: without it Invoke-WebRequest wants Internet
    # Explorer's engine, which is gone from current Windows.
    Invoke-WebRequest -UseBasicParsing -Uri $url -OutFile $dest
}

try {
    # Windows PowerShell 5.1 can still default to TLS 1.0, which
    # python.org and GitHub both refuse outright.
    [Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12

    Write-Host ""
    Write-Host "Installing the Lordnine bots. Nothing else needs to be set up."
    Write-Host ""

    # -- 1. Credentials, before anything is downloaded ------------------
    $installedEnv = Join-Path $CodeDir '.env'
    if (-not (Test-Path $EnvFile)) {
        # An update should not mean re-sending live tokens over chat, nor
        # keeping a folder full of them on someone's desktop forever.
        if (Test-Path $installedEnv) {
            Say "Using the credentials already installed"
            $EnvFile = $installedEnv
        } else {
            Halt "no .env file next to this installer.`n  Expected: $EnvFile`n  Ask whoever sent you this for it, put it in this folder, and run INSTALL.bat again."
        }
    }
    $envMap = @{}
    foreach ($line in Get-Content $EnvFile) {
        if ($line -match '^\s*([A-Z_][A-Z0-9_]*)\s*=\s*(.*)$') {
            $envMap[$Matches[1]] = $Matches[2].Trim()
        }
    }
    $absent = $Required | Where-Object { -not $envMap[$_] }
    if ($absent) { Halt "the .env file is missing: $($absent -join ', ')" }

    # A service account key pasted across several lines is not valid JSON,
    # and the bots only say so at runtime, in a log nobody is watching.
    try   { $envMap['GOOGLE_SERVICE_ACCOUNT_JSON'] | ConvertFrom-Json | Out-Null }
    catch { Halt "GOOGLE_SERVICE_ACCOUNT_JSON is not valid JSON. It must be the whole key on ONE line." }
    Say "Credentials look complete"

    # An earlier copy must stop before its files can be replaced: Windows
    # will not overwrite a running executable. This is the update path too.
    Stop-ScheduledTask -TaskName $Task -ErrorAction SilentlyContinue
    Start-Sleep -Seconds 2

    # -- 2. Python, unpacked rather than installed ----------------------
    # Re-running this script IS the update path, so it must not spend ten
    # megabytes and several minutes rebuilding an interpreter that is
    # already sitting there working.
    $python = Join-Path $PyDir 'python.exe'
    $pythonUsable = $false
    if (Test-Path $python) {
        $v = & $python --version 2>$null
        if ($LASTEXITCODE -eq 0 -and $v -match 'Python 3\.13') {
            & $python -m pip --version 2>&1 | Out-Null
            $pythonUsable = ($LASTEXITCODE -eq 0)
        }
    }

    if ($pythonUsable) {
        Say "Reusing the Python already installed"
    } else {
        Say "Setting up Python (about 10 MB, once)"
        if (Test-Path $PyDir) { Remove-Item -Recurse -Force $PyDir }
        New-Item -ItemType Directory -Force -Path $PyDir | Out-Null
        $pyZipPath = Join-Path $env:TEMP 'python-embed.zip'
        Fetch $PyZip $pyZipPath
        Expand-Archive -Path $pyZipPath -DestinationPath $PyDir -Force
        if (-not (Test-Path $python)) { Halt "the Python download did not unpack correctly." }

    # The embeddable build ships python313._pth with `import site`
    # commented out, which leaves site-packages off the path -- pip can
    # neither install nor import anything until this is on.
        $pth = Get-ChildItem $PyDir -Filter 'python*._pth' | Select-Object -First 1
        if (-not $pth) { Halt "the embedded Python has no ._pth file to configure." }
        (Get-Content $pth.FullName) -replace '^\s*#\s*import site\s*$', 'import site' |
            Set-Content $pth.FullName
        if (-not (Select-String -Path $pth.FullName -Pattern '^import site' -Quiet)) {
            Halt "could not enable site-packages in the embedded Python."
        }

        $getPip = Join-Path $env:TEMP 'get-pip.py'
        Fetch $GetPip $getPip
        & $python $getPip --no-warn-script-location 2>&1 | Out-Null
        & $python -m pip --version | Out-Null
        if ($LASTEXITCODE -ne 0) { Halt "pip could not be set up inside the embedded Python." }
        Say "Python ready (used only by these bots, nothing else on this PC changes)"
    }

    # -- 3. The code ----------------------------------------------------
    Say "Downloading the bots"
    $codeZip = Join-Path $env:TEMP 'fb-timer-bot.zip'
    Fetch $Code $codeZip
    $staging = Join-Path $env:TEMP 'fb-timer-bot-unzip'
    if (Test-Path $staging) { Remove-Item -Recurse -Force $staging }
    Expand-Archive -Path $codeZip -DestinationPath $staging -Force
    $unpacked = @(Get-ChildItem $staging -Directory) | Select-Object -First 1
    if (-not $unpacked) { Halt "the code download did not unpack as expected." }

    # supervisor.py writes its log beside itself, so refreshing the code
    # would delete the history -- precisely when an update is being made
    # because something went wrong. Carry it across.
    $logs = Join-Path $CodeDir 'logs'
    $logsKeep = Join-Path $env:TEMP 'fb-timer-bot-logs'
    if (Test-Path $logsKeep) { Remove-Item -Recurse -Force $logsKeep }
    if (Test-Path $logs) { Move-Item $logs $logsKeep }

    # Likewise the credentials, when this run is reusing the installed set.
    $envKeep = $null
    if ($EnvFile -eq $installedEnv -and (Test-Path $installedEnv)) {
        $envKeep = Join-Path $env:TEMP 'fb-timer-bot-env'
        Copy-Item -Force $installedEnv $envKeep
        $EnvFile = $envKeep
    }

    if (Test-Path $CodeDir) { Remove-Item -Recurse -Force $CodeDir }
    New-Item -ItemType Directory -Force -Path $CodeDir | Out-Null
    Copy-Item -Recurse -Force (Join-Path $unpacked.FullName '*') $CodeDir
    if (Test-Path $logsKeep) { Move-Item $logsKeep $logs }

    # -- 4. Dependencies -------------------------------------------------
    Say "Installing dependencies (a few minutes; only this once)"
    & $python -m pip install --quiet --no-warn-script-location `
        -r (Join-Path $CodeDir 'requirements.txt')
    if ($LASTEXITCODE -ne 0) { Halt "installing dependencies failed." }

    Copy-Item -Force $EnvFile (Join-Path $CodeDir '.env')
    Say "Credentials copied in"

    # -- 5. Prove it can actually start ---------------------------------
    # The supervisor stays up even when all three children exit 78, so
    # "the task is running" is not evidence the bots are. Importing them
    # is: a bad token, a broken key or a missing package fails here,
    # where the message can still be read, rather than silently later.
    Say "Checking the bots can start"
    $check = & $python -c "import bot, items_bot, attendance_bot; print('ok')" 2>&1
    if ($LASTEXITCODE -ne 0 -or $check -notmatch 'ok') {
        Halt "the bots could not load:`n$check"
    }

    # -- 6. Autostart ----------------------------------------------------
    # At logon rather than at startup, because a per-user task needs a
    # session -- and this deliberately never asks for an administrator.
    # ExecutionTimeLimit zero, or Task Scheduler kills them after 3 days.
    $action = New-ScheduledTaskAction -Execute $python `
        -Argument '-u supervisor.py' -WorkingDirectory $CodeDir
    $trigger = New-ScheduledTaskTrigger -AtLogOn
    $settings = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries `
        -DontStopIfGoingOnBatteries -StartWhenAvailable `
        -ExecutionTimeLimit ([TimeSpan]::Zero)
    Register-ScheduledTask -TaskName $Task -Action $action -Trigger $trigger `
        -Settings $settings -Force `
        -Description 'Lordnine field boss timer, attendance and item bots' | Out-Null
    Say "Set to start automatically at every logon"

    # -- 7. Try to stop the PC sleeping ---------------------------------
    # Reported honestly: powercfg can be refused by policy, and claiming
    # success when the machine still sleeps would send someone hunting
    # for a bug that is really a power setting.
    powercfg /change standby-timeout-ac 0 2>&1 | Out-Null
    $sleepOk = ($LASTEXITCODE -eq 0)
    powercfg /change hibernate-timeout-ac 0 2>&1 | Out-Null
    if ($sleepOk) { Say "Sleep turned off while plugged in" }
    else { Say "COULD NOT turn sleep off -- set Settings > System > Power to Never" }

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
        Write-Host "  Ask whoever sent you this to check Discord and confirm"
        Write-Host "  all three bots show as online."
        Write-Host ""
        Write-Host "  If they ever ask you for the bot logs, paste this into"
        Write-Host "  the File Explorer address bar:"
        Write-Host "      %LOCALAPPDATA%\fb-timer-bot\app\logs" -ForegroundColor Cyan
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
