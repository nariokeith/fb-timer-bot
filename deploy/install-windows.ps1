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
$GetPipUrl = 'https://bootstrap.pypa.io/get-pip.py'
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
    # No Stop-Transcript here. `exit` inside the try still runs the finally
    # at the bottom, and calling it twice makes the second one complain
    # that "the host is not currently transcribing" -- printed in red,
    # directly under a message asking the reader to send a log, where it
    # looks like the real fault.
    exit 1
}
# Run an external program, returning its exit code and its output.
#
# Windows PowerShell 5.1 -- which INSTALL.bat invokes, not pwsh 7 -- turns
# a native command's stderr into a NativeCommandError, and with
# $ErrorActionPreference = 'Stop' that error TERMINATES the script. So
# `python -m pip --version` on a Python with no pip did not return a
# non-zero exit code to be checked: it threw, and surfaced to the host as
# "FAILED: ...python.exe: No module named pip".
#
# Programs write ordinary progress to stderr all the time, so every
# external call here goes through this. The preference is relaxed for the
# duration of the call only, and the exit code is what gets checked.
#
# Not reproducible from a Mac: pwsh 7 does not behave this way.
function Run ($exe, [string[]]$argv) {
    $prev = $ErrorActionPreference
    $ErrorActionPreference = 'Continue'
    try {
        $out = & $exe @argv 2>&1 | Out-String
        [pscustomobject]@{ Code = $LASTEXITCODE; Output = $out }
    } finally {
        $ErrorActionPreference = $prev
    }
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
    # Stop whatever is already running, by either mechanism. Windows will
    # not replace files that are open, and a re-run is the update path.
    Stop-ScheduledTask -TaskName $Task -ErrorAction SilentlyContinue
    Get-Process -Name 'pythonw','python' -ErrorAction SilentlyContinue |
        Where-Object { $_.Path -and $_.Path.StartsWith($PyDir, 'OrdinalIgnoreCase') } |
        Stop-Process -Force -ErrorAction SilentlyContinue
    Start-Sleep -Seconds 2

    Say "Downloading the bots"
    $codeZip = Join-Path $env:TEMP 'fb-timer-bot.zip'
    Fetch $Code $codeZip
    $staging = Join-Path $env:TEMP 'fb-timer-bot-unzip'
    if (Test-Path $staging) { Remove-Item -Recurse -Force $staging }
    Expand-Archive -Path $codeZip -DestinationPath $staging -Force
    $unpacked = @(Get-ChildItem $staging -Directory) | Select-Object -First 1
    if (-not $unpacked) { Halt "the code download did not unpack as expected." }

    # -- 2. A newer copy of this script ---------------------------------
    # Only the bot code comes from GitHub; this file is whatever the host
    # already has. So an installer bug cannot be fixed by re-running it --
    # it needs a file sent over chat, which is the one thing this whole
    # design exists to avoid. Since the repo is downloaded anyway, take
    # the newer copy of ourselves out of it.
    #
    # Replaced and then stopped, rather than re-executed: relaunching is
    # process juggling that cannot be tested from a Mac, and getting it
    # wrong breaks the only thing the host is able to do. One more
    # double-click is cheap, and only ever happens when this file changed.
    $shippedPs1 = Join-Path $unpacked.FullName 'deploy\install-windows.ps1'
    if ((Test-Path $shippedPs1) -and $PSCommandPath -and (Test-Path $PSCommandPath)) {
        $newer = (Get-FileHash $shippedPs1 -Algorithm SHA256).Hash
        $mine  = (Get-FileHash $PSCommandPath -Algorithm SHA256).Hash
        if ($newer -ne $mine) {
            # PowerShell parses the whole script before running it, so
            # overwriting the file under ourselves is safe; exit at once
            # regardless.
            Copy-Item -Force $shippedPs1 $PSCommandPath

            # INSTALL.bat is deliberately NOT replaced. cmd.exe keeps a
            # byte OFFSET into the batch file it is running and re-reads
            # from it after each command, so overwriting that file
            # mid-execution makes cmd resume inside different content and
            # run whatever fragment lands there -- observed as
            # "'o' is not recognized", the tail of an echo. This script is
            # safe to replace because PowerShell parses a script fully
            # before executing it; a .bat is not.
            #
            # It costs nothing: the .bat is a fifteen-line stub that
            # forwards to this file and has no reason to change.
            Write-Host ""
            Write-Host "  The installer updated itself to a newer version." -ForegroundColor Cyan
            Write-Host "  Nothing is broken - this is normal."
            Write-Host ""
            Write-Host "  Please double-click INSTALL.bat once more to finish."
            Write-Host ""
            # No Stop-Transcript here: `exit` inside a try still runs the
            # finally below, and a second call errors with "the host is
            # not currently transcribing" -- printed in red, alarming, and
            # entirely meaningless to whoever is reading it.
            exit 0
        }
    }

    # -- 3. Python, unpacked rather than installed ----------------------
    # Re-running this script IS the update path, so it must not spend ten
    # megabytes and several minutes rebuilding an interpreter that is
    # already sitting there working.
    $python = Join-Path $PyDir 'python.exe'
    $pythonUsable = $false
    if (Test-Path $python) {
        $ver = Run $python @('--version')
        if ($ver.Code -eq 0 -and $ver.Output -match 'Python 3\.13') {
            $pythonUsable = ((Run $python @('-m','pip','--version')).Code -eq 0)
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

        # Two names, not one differing only in case: PowerShell variable
        # names are case-INSENSITIVE, so a URL constant and a destination
        # path spelled the same way are a single variable. The second
        # assignment overwrote the URL, and Invoke-WebRequest was asked to
        # download from a local file that did not exist yet.
        $getPipFile = Join-Path $env:TEMP 'get-pip.py'
        Fetch $GetPipUrl $getPipFile
        $bootstrap = Run $python @($getPipFile, '--no-warn-script-location')
        if ((Run $python @('-m','pip','--version')).Code -ne 0) {
            Halt "pip could not be set up inside the embedded Python.`n$($bootstrap.Output)"
        }
        Say "Python ready (used only by these bots, nothing else on this PC changes)"
    }

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

    # The embeddable Python finds nothing next to a script it runs.
    #
    # A ._pth file switches the interpreter into isolated mode: sys.path
    # becomes exactly the lines in that file, PYTHONPATH is ignored, and
    # the script's own directory is NOT added -- documented, and tracked
    # as bpo-34841. So `pythonw -u bot.py` executed bot.py happily and
    # then failed on `import channel_guard`, which sits beside it.
    #
    # Rewritten on every run rather than only on a fresh install, because
    # the reuse path keeps a ._pth written by an older version of this
    # script. Absolute, because this file knows the path exactly and a
    # relative one only invites questions about what it is relative to.
    $pth = Get-ChildItem $PyDir -Filter 'python*._pth' | Select-Object -First 1
    if (-not $pth) { Halt "the embedded Python has no ._pth file to configure." }
    @(
        'python313.zip'
        '.'
        $CodeDir
        'import site'
    ) | Set-Content -Path $pth.FullName -Encoding ASCII
    Say "Python can see the bots"

    # -- 4. Dependencies -------------------------------------------------
    Say "Installing dependencies (a few minutes; only this once)"
    $deps = Run $python @('-m','pip','install','--quiet','--no-warn-script-location',
                          '-r', (Join-Path $CodeDir 'requirements.txt'))
    if ($deps.Code -ne 0) { Halt "installing dependencies failed:`n$($deps.Output)" }

    Copy-Item -Force $EnvFile (Join-Path $CodeDir '.env')
    Say "Credentials copied in"

    # -- 5. Prove it can actually start ---------------------------------
    # The supervisor stays up even when all three children exit 78, so
    # "the task is running" is not evidence the bots are. Importing them
    # is: a bad token, a broken key or a missing package fails here,
    # where the message can still be read, rather than silently later.
    Say "Checking the bots can start"
    # Deliberately NO sys.path injection. An earlier version added the
    # code directory itself, which made the check pass while every bot
    # failed on import -- it was testing a path the bots never get. A
    # check that arranges its own success is worse than none, because it
    # certifies the install and hides the fault until the log is read.
    #
    # This now relies on the ._pth written above, which is exactly what
    # the running bots rely on.
    $probe = "import bot, items_bot, attendance_bot; print('ok')"
    $check = Run $python @('-c', $probe)
    if ($check.Code -ne 0 -or $check.Output -notmatch 'ok') {
        Halt "the bots could not load:`n$($check.Output)"
    }

    # -- 6. Autostart ----------------------------------------------------
    # At logon rather than at startup, because a per-user task needs a
    # session -- and this deliberately never asks for an administrator.
    # ExecutionTimeLimit zero, or Task Scheduler kills them after 3 days.
    # pythonw.exe, not python.exe. An at-logon task runs in the user's own
    # interactive session, so a console executable parks a black window on
    # their desktop at every logon -- forever, saying nothing, in front of
    # someone with every reason to close it. Closing it kills all three
    # bots. pythonw is the same interpreter without the console, and it
    # costs nothing: the output that matters goes to the log file, print()
    # is a no-op when there is no stdout, and the children's output is
    # read through pipes the supervisor owns rather than its own stdout.
    $pythonw = Join-Path $PyDir 'pythonw.exe'
    if (-not (Test-Path $pythonw)) { Halt "the embedded Python has no pythonw.exe." }
    # Two mechanisms, because the first one is not always permitted.
    # Register-ScheduledTask returned "Access is denied" on the host's PC
    # even un-elevated and registering only for themselves -- whether from
    # policy, an ACL on the task folder, or security software, none of
    # which can be diagnosed from here or asked about over chat.
    #
    # A shortcut in the user's own Startup folder cannot be refused: it is
    # an ordinary file in their own profile. It gives up Task Scheduler's
    # restart-on-failure, which costs nothing here, because supervisor.py
    # is the thing that restarts bots and it restarts itself only when
    # Windows starts it -- which is exactly once, at logon, either way.
    $useTask = $false
    try {
        $action = New-ScheduledTaskAction -Execute $pythonw `
            -Argument '-u supervisor.py' -WorkingDirectory $CodeDir
        $trigger = New-ScheduledTaskTrigger -AtLogOn
        $settings = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries `
            -DontStopIfGoingOnBatteries -StartWhenAvailable `
            -ExecutionTimeLimit ([TimeSpan]::Zero)
        Register-ScheduledTask -TaskName $Task -Action $action -Trigger $trigger `
            -Settings $settings -Force `
            -Description 'Lordnine field boss timer, attendance and item bots' | Out-Null
        $useTask = $true
        Say "Set to start automatically at every logon"
    } catch {
        $startup = [Environment]::GetFolderPath('Startup')
        $lnkPath = Join-Path $startup 'Lordnine bots.lnk'
        try {
            $shell = New-Object -ComObject WScript.Shell
            $lnk = $shell.CreateShortcut($lnkPath)
            $lnk.TargetPath       = $pythonw
            $lnk.Arguments        = '-u supervisor.py'
            $lnk.WorkingDirectory = $CodeDir
            $lnk.Description      = 'Lordnine field boss timer, attendance and item bots'
            $lnk.WindowStyle      = 7   # minimised; pythonw shows nothing anyway
            $lnk.Save()
        } catch {
            Halt "could not set the bots to start automatically:`n$($_.Exception.Message)"
        }
        if (-not (Test-Path $lnkPath)) { Halt "the startup shortcut was not created." }
        Say "Set to start automatically at every logon (via the Startup folder)"
    }

    # -- 7. Try to stop the PC sleeping ---------------------------------
    # Reported honestly: powercfg can be refused by policy, and claiming
    # success when the machine still sleeps would send someone hunting
    # for a bug that is really a power setting.
    $sleepOk = ((Run 'powercfg' @('/change','standby-timeout-ac','0')).Code -eq 0)
    Run 'powercfg' @('/change','hibernate-timeout-ac','0') | Out-Null
    if ($sleepOk) { Say "Sleep turned off while plugged in" }
    else { Say "COULD NOT turn sleep off -- set Settings > System > Power to Never" }

    # Start it now so nobody has to log out and back in.
    if ($useTask) {
        Start-ScheduledTask -TaskName $Task
    } else {
        Start-Process -FilePath $pythonw -ArgumentList '-u supervisor.py' `
            -WorkingDirectory $CodeDir -WindowStyle Hidden
    }

    # Verified from the supervisor's own log, not from "the task is
    # Running": the supervisor stays up even when all three children exit
    # 78, so its being alive was never evidence the bots were. The log
    # says what actually happened.
    $logFile = Join-Path $CodeDir 'logs\supervisor.log'
    $started = $false
    foreach ($attempt in 1..30) {
        Start-Sleep -Seconds 1
        if (Test-Path $logFile) {
            $tail = Get-Content $logFile -Tail 40 -ErrorAction SilentlyContinue
            if ($tail -match 'logged in|State restored|restored state') { $started = $true; break }
        }
    }

    Write-Host ""
    if ($started) {
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
        Write-Host "  Installed, but the bots did not report logging in." -ForegroundColor Yellow
        Write-Host "  Send install-log.txt back to whoever gave you this."
        if (Test-Path $logFile) {
            Write-Host ""
            Write-Host "  Last lines of the bot log:"
            Get-Content $logFile -Tail 15 | ForEach-Object { Write-Host "    $_" }
        }
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
