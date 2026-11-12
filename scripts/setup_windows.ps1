<#
  setup_windows.ps1 -- one-shot installer run by INSTALL_JARVIS.bat (double-click).

  Installs everything JARVIS needs, then starts it, so one double-click gives a JARVIS
  that runs now and at every logon afterwards:
    1. preflight checks (Windows version, disk space, folder location, unblock files),
       and stop a JARVIS that is still running from an earlier install
    2. Python 3.11 + virtual environment + Python packages (+ the Visual C++ runtime)
    3. the on-screen display: Electron, three.js, MediaPipe (downloaded and verified by
       boot\overlay_runtime.py -- no Node.js needed; without it JARVIS has nothing to show,
       so a failure here fails the install instead of being a footnote)
    4. FFmpeg (ffplay, for music playback)
    5. Ollama + a language model sized to this PC (GPU VRAM / RAM)
    6. voices and speech models (British Piper voice, Vosk wake-phrase model,
       openWakeWord, Whisper, hand-tracking model, Windows en-GB/ko-KR voices)
    7. folder permissions + auto-start at logon
    8. self-test (scripts/healthcheck.py)
    9. launch as the logged-on user, then wait until JARVIS is really on screen

  Exit codes: 0 = installed and on screen, 2 = installed but it did not come up (the
  reason is printed), 1 = setup failed.

  Every step is idempotent: running the installer again skips what is already done and
  repairs what isn't. Network steps retry. Full log: data\install.log.
  This file is plain ASCII on purpose (Windows PowerShell 5.1 reads BOM-less scripts in the
  system code page).
#>
param([switch]$NoLaunch)

$ErrorActionPreference = "Continue"
$ProgressPreference = "SilentlyContinue"      # Invoke-WebRequest is far faster without the bar
try {
    [Net.ServicePointManager]::SecurityProtocol = [Net.ServicePointManager]::SecurityProtocol -bor [Net.SecurityProtocolType]::Tls12
} catch {}

$Root      = Split-Path -Parent (Split-Path -Parent $MyInvocation.MyCommand.Path)
$Data      = Join-Path $Root "data"
$Tools     = Join-Path $Root "tools"
$Downloads = Join-Path $Data "downloads"
$Voices    = Join-Path $Root "voices"
$Models    = Join-Path $Root "models"
New-Item -ItemType Directory -Force -Path $Data, $Tools, $Downloads, $Voices, $Models | Out-Null
Set-Location $Root
try { Start-Transcript -Path (Join-Path $Data "install.log") -Append | Out-Null } catch {}

$script:Warnings = New-Object System.Collections.Generic.List[string]
$script:Fatal = $false

function Section([int]$n, [string]$title) { Write-Host ""; Write-Host "==== [$n/9] $title ====" -ForegroundColor Cyan }
function Ok([string]$m)   { Write-Host "  [ok]   $m" -ForegroundColor Green }
function Info([string]$m) { Write-Host "  $m" }
function Warn([string]$m) { Write-Host "  [warn] $m" -ForegroundColor Yellow; $script:Warnings.Add($m) }
function Have([string]$c) { return [bool](Get-Command $c -ErrorAction SilentlyContinue) }

function Refresh-Path {
    $env:Path = [Environment]::GetEnvironmentVariable("Path", "Machine") + ";" +
                [Environment]::GetEnvironmentVariable("Path", "User")
}

function Retry([scriptblock]$Action, [int]$Tries = 3, [string]$What = "step") {
    for ($i = 1; $i -le $Tries; $i++) {
        $global:LASTEXITCODE = 0
        $failed = $false
        try { & $Action | Out-Host } catch { $failed = $true; Info "    error: $($_.Exception.Message)" }
        if (-not $failed -and $global:LASTEXITCODE -eq 0) { return $true }
        if ($i -lt $Tries) { Info "    $What failed (attempt $i of $Tries), retrying..."; Start-Sleep -Seconds (5 * $i) }
    }
    return $false
}

function Download([string]$Url, [string]$Out) {
    if ((Test-Path $Out) -and ((Get-Item $Out).Length -gt 0)) { return $true }
    $tmp = "$Out.part"
    $name = Split-Path $Out -Leaf
    Info "  downloading $name ..."
    $ok = Retry { Invoke-WebRequest -Uri $Url -OutFile $tmp -UseBasicParsing -TimeoutSec 900 } 3 "download of $name"
    if ($ok -and (Test-Path $tmp)) { Move-Item -Force $tmp $Out; return $true }
    Remove-Item -Force $tmp -ErrorAction SilentlyContinue
    return $false
}

function Winget-Install([string]$Id, [string]$Scope = "") {
    if (-not (Have "winget")) { return $false }
    Info "  installing $Id with winget ..."
    $wgArgs = @("install", "--id", $Id, "-e", "--silent", "--accept-source-agreements",
                "--accept-package-agreements", "--disable-interactivity")
    if ($Scope) { $wgArgs += @("--scope", $Scope) }
    & winget @wgArgs | Out-Host
    $code = $LASTEXITCODE
    Refresh-Path
    return ($code -eq 0 -or $code -eq -1978335189)    # 0x8A15002B: already installed
}

# ---------------------------------------------------------------------------- 1
Section 1 "Preflight"
$build = [Environment]::OSVersion.Version.Build
if ($build -lt 17763) { Warn "Windows build $build is old; Windows 10 1809 or newer is recommended." } else { Ok "Windows build $build" }
try {
    $drive = (Split-Path $Root -Qualifier).TrimEnd(':')
    $freeGB = [math]::Round((Get-PSDrive -Name $drive).Free / 1GB)
    if ($freeGB -lt 15) { Warn "Only $freeGB GB free on ${drive}: - JARVIS needs about 10-20 GB for models." } else { Ok "$freeGB GB free on ${drive}:" }
} catch {}
if ($Root -like "$env:ProgramFiles*" -or $Root -like "${env:ProgramFiles(x86)}*") {
    Warn "JARVIS is inside Program Files; a folder in your user profile (e.g. C:\JARVIS) works better."
}
$consoleUser = (Get-CimInstance Win32_ComputerSystem).UserName
$me = "$env:USERDOMAIN\$env:USERNAME"
if ($consoleUser -and $consoleUser -ne $me) {
    Warn "Installing as $me for $consoleUser. Python is installed for all users; if JARVIS later reports the language engine offline, run Ollama's installer once from $consoleUser's account."
}
foreach ($od in @($env:OneDrive, $env:OneDriveConsumer, $env:OneDriveCommercial)) {
    if ($od -and $Root.StartsWith($od, [System.StringComparison]::OrdinalIgnoreCase)) {
        Warn "JARVIS is inside OneDrive. Syncing its program files slows it down and can lock them; a plain folder such as C:\JARVIS works better."
        break
    }
}
if ($Root -match '[^\x00-\x7F]') {
    Warn "The folder path contains non-English characters; some speech models cannot be loaded from such a path. A folder such as C:\JARVIS avoids that."
}
Get-ChildItem -Path $Root -Recurse -File -ErrorAction SilentlyContinue | Unblock-File -ErrorAction SilentlyContinue
Ok "files unblocked"

# A JARVIS that is still running (an earlier version, or this one) would keep files locked
# and keep the old program in memory. Stop it; step 9 starts the new one.
function Stop-Jarvis {
    $vpy = Join-Path $Root ".venv\Scripts\python.exe"
    if (Test-Path $vpy) {
        try { & $vpy (Join-Path $Root "boot\launcher.py") --stop | Out-Null } catch {}
    }
    try {
        Get-CimInstance Win32_Process | Where-Object {
            @('python.exe', 'pythonw.exe', 'electron.exe', 'node.exe') -contains $_.Name -and
            $_.CommandLine -and $_.CommandLine.Contains($Root)
        } | ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }
    } catch {}
}
Stop-Jarvis
Ok "no JARVIS left running from an earlier start"

# ---------------------------------------------------------------------------- 2
Section 2 "Python 3.11 and Python packages"
function Find-Python311 {
    if (Have "py") {
        $p = & py -3.11 -c "import sys; print(sys.executable)" 2>$null | Select-Object -Last 1
        if ($LASTEXITCODE -eq 0 -and $p -and (Test-Path $p.Trim())) { return $p.Trim() }
    }
    foreach ($c in @("$env:ProgramFiles\Python311\python.exe",
                     "$env:LOCALAPPDATA\Programs\Python\Python311\python.exe")) {
        if (Test-Path $c) { return $c }
    }
    return $null
}
$Py = Find-Python311
if (-not $Py) { Winget-Install "Python.Python.3.11" "machine" | Out-Null; $Py = Find-Python311 }
if (-not $Py) {
    $inst = Join-Path $Downloads "python-3.11.9-amd64.exe"
    if (Download "https://www.python.org/ftp/python/3.11.9/python-3.11.9-amd64.exe" $inst) {
        Info "  running the Python 3.11 installer (quiet) ..."
        Start-Process -FilePath $inst -ArgumentList "/quiet InstallAllUsers=1 PrependPath=1 Include_launcher=1 Include_test=0" -Wait
        Refresh-Path
        $Py = Find-Python311
    }
}
$VPy = Join-Path $Root ".venv\Scripts\python.exe"
if (-not $Py) {
    Warn "Python 3.11 could not be installed; the JARVIS backend cannot run."
    $script:Fatal = $true
} else {
    Ok "Python: $Py"
    if (Test-Path $VPy) {
        & $VPy -c "import sys" 2>$null
        if ($LASTEXITCODE -ne 0) {
            Info "  the existing Python environment is broken; rebuilding it"
            Remove-Item -Recurse -Force (Join-Path $Root ".venv") -ErrorAction SilentlyContinue
        }
    }
    if (-not (Test-Path $VPy)) { & $Py -m venv (Join-Path $Root ".venv") }
    & $VPy -m pip install --upgrade pip --disable-pip-version-check -q | Out-Host
    $okPip = Retry { & $VPy -m pip install --prefer-binary --disable-pip-version-check -r (Join-Path $Root "requirements.txt") } 3 "pip install"
    & $VPy -c "import fastapi, uvicorn, numpy, vosk, faster_whisper, piper, sounddevice, webrtcvad; print('core packages import ok')"
    if ($okPip -and $LASTEXITCODE -eq 0) { Ok "Python packages installed" }
    else { Warn "Some Python packages failed to install (see the log)."; $script:Fatal = $true }
}

# The speech packages (onnxruntime, ctranslate2) need the Visual C++ runtime. Most PCs
# already have it; add it where it is missing so voice does not fail with "DLL load failed".
$sys32 = Join-Path $env:SystemRoot "System32"
if (-not ((Test-Path (Join-Path $sys32 "msvcp140.dll")) -and (Test-Path (Join-Path $sys32 "vcruntime140_1.dll")))) {
    Info "  installing the Microsoft Visual C++ runtime ..."
    if (-not (Winget-Install "Microsoft.VCRedist.2015+.x64")) {
        $vc = Join-Path $Downloads "vc_redist.x64.exe"
        if (Download "https://aka.ms/vs/17/release/vc_redist.x64.exe" $vc) {
            Start-Process -FilePath $vc -ArgumentList "/install /quiet /norestart" -Wait
        }
    }
    if (Test-Path (Join-Path $sys32 "msvcp140.dll")) { Ok "Visual C++ runtime installed" }
    else { Warn "The Visual C++ runtime could not be installed; voice features may not load until it is." }
}

# ---------------------------------------------------------------------------- 3
Section 3 "Holographic overlay (Electron, three.js, MediaPipe)"
# Downloaded straight from the npm registry and Electron's releases and verified against
# package-lock.json / Electron's checksums. Node.js is not needed.
$overlayOk = $false
if (Test-Path $VPy) {
    $overlayOk = Retry { & $VPy (Join-Path $Root "boot\overlay_runtime.py") } 2 "overlay download"
    if (-not $overlayOk) {
        $npm = $null
        if (Have "npm") { $npm = (Get-Command npm).Source }
        elseif (Test-Path "$env:ProgramFiles\nodejs\npm.cmd") { $npm = "$env:ProgramFiles\nodejs\npm.cmd" }
        if ($npm) {
            Info "  trying npm as a second route ..."
            Push-Location $Root
            Retry { & $npm install --no-audit --no-fund --loglevel=error } 1 "npm install" | Out-Null
            Pop-Location
            & $VPy (Join-Path $Root "boot\overlay_runtime.py") --check | Out-Host
            $overlayOk = ($LASTEXITCODE -eq 0)
        }
    }
}
if ($overlayOk) { Ok "on-screen display installed (Electron, three.js, MediaPipe)" }
else {
    Warn "The on-screen display could not be downloaded (github.com / registry.npmjs.org unreachable?). Without it JARVIS has nothing to show."
    $script:Fatal = $true
}

# ---------------------------------------------------------------------------- 4
Section 4 "FFmpeg (music playback)"
function Find-FFplay {
    if (Have "ffplay") { return (Get-Command ffplay).Source }
    $f = Get-ChildItem -Path $Tools -Recurse -Filter "ffplay.exe" -ErrorAction SilentlyContinue | Select-Object -First 1
    if ($f) { return $f.FullName }
    return $null
}
if (-not (Find-FFplay)) { Winget-Install "Gyan.FFmpeg" | Out-Null }
if (-not (Find-FFplay)) {
    $zip = Join-Path $Downloads "ffmpeg-release-essentials.zip"
    if (Download "https://www.gyan.dev/ffmpeg/builds/ffmpeg-release-essentials.zip" $zip) {
        Expand-Archive -Path $zip -DestinationPath (Join-Path $Tools "ffmpeg") -Force
    }
}
if (Find-FFplay) { Ok "ffplay available" } else { Warn "FFmpeg not installed; music will open in the browser instead of playing directly." }

# ---------------------------------------------------------------------------- 5
Section 5 "Ollama (the offline language brain)"
function Find-Ollama {
    if (Have "ollama") { return (Get-Command ollama).Source }
    $c = "$env:LOCALAPPDATA\Programs\Ollama\ollama.exe"
    if (Test-Path $c) { return $c }
    return $null
}
function Ollama-Up {
    try { return (Invoke-WebRequest -Uri "http://127.0.0.1:11434/api/tags" -UseBasicParsing -TimeoutSec 3).StatusCode -eq 200 }
    catch { return $false }
}
if (-not (Find-Ollama)) { Winget-Install "Ollama.Ollama" | Out-Null }
if (-not (Find-Ollama)) {
    $exe = Join-Path $Downloads "OllamaSetup.exe"
    if (Download "https://ollama.com/download/OllamaSetup.exe" $exe) {
        Start-Process -FilePath $exe -ArgumentList "/VERYSILENT /NORESTART /SUPPRESSMSGBOXES" -Wait
        Refresh-Path
    }
}
$Ollama = Find-Ollama
if ($Ollama) {
    if (-not (Ollama-Up)) {
        Start-Process -FilePath $Ollama -ArgumentList "serve" -WindowStyle Hidden
        for ($i = 0; $i -lt 30 -and -not (Ollama-Up); $i++) { Start-Sleep -Seconds 1 }
    }
    $vram = 0
    if (Have "nvidia-smi") {
        try { $vram = [int]((& nvidia-smi --query-gpu=memory.total --format=csv,noheader,nounits | Select-Object -First 1).Trim()) } catch {}
    }
    $ram = [math]::Round((Get-CimInstance Win32_ComputerSystem).TotalPhysicalMemory / 1GB)
    if ($vram -ge 12000)                  { $model = "qwen2.5:14b" }
    elseif ($vram -ge 6000 -or $ram -ge 32) { $model = "qwen2.5:7b" }
    else                                  { $model = "qwen2.5:3b" }
    Info "  GPU memory: $vram MB, RAM: $ram GB -> model $model (large download)"
    if (Retry { & $Ollama pull $model } 3 "model download") {
        Ok "language model $model ready"
        if (Test-Path $VPy) { & $VPy (Join-Path $Root "scripts\set_config.py") llm.model $model | Out-Host }
    } else { Warn "The language model could not be downloaded; JARVIS will reply with a short fallback until it is." }
} else { Warn "Ollama could not be installed; conversation will use a short fallback until it is." }

# ---------------------------------------------------------------------------- 6
Section 6 "Voices and speech models"
$hf = "https://huggingface.co/rhasspy/piper-voices/resolve/main"
$okV = (Download "$hf/en/en_GB/alan/medium/en_GB-alan-medium.onnx" (Join-Path $Voices "en_GB-alan-medium.onnx")) -and
       (Download "$hf/en/en_GB/alan/medium/en_GB-alan-medium.onnx.json" (Join-Path $Voices "en_GB-alan-medium.onnx.json"))
if ($okV) { Ok "British voice (Piper en_GB-alan-medium)" } else { Warn "British Piper voice not downloaded; Windows voices will be used instead." }

# A Korean Piper voice, if one has been published (none official at the time of writing).
$vj = Join-Path $Downloads "voices.json"
if ((Test-Path $VPy) -and (Download "$hf/voices.json" $vj)) {
    $line = & $VPy -c "import json,sys; v=json.load(open(sys.argv[1],encoding='utf-8')); k=[x for x in v if x.startswith('ko_')]; f=list(v[k[0]]['files']) if k else []; print('|'.join([k[0]]+[p for p in f if p.endswith(('.onnx','.onnx.json'))]) if k else '')" $vj
    if ($line) {
        $parts = $line.Trim().Split('|'); $key = $parts[0]
        if ($parts.Length -gt 1) {
            foreach ($p in ($parts | Select-Object -Skip 1)) { Download "$hf/$p" (Join-Path $Voices (Split-Path $p -Leaf)) | Out-Null }
            & $VPy (Join-Path $Root "scripts\set_config.py") voice.tts.voice_ko $key | Out-Host
            Ok "Korean Piper voice: $key"
        }
    } else { Info "  no Korean Piper voice published; Korean replies use the Windows Korean voice" }
}

$voskDir = Join-Path $Models "vosk-small-en"
if (-not (Test-Path (Join-Path $voskDir "conf"))) {
    $zip = Join-Path $Downloads "vosk-model-small-en-us-0.15.zip"
    if (Download "https://alphacephei.com/vosk/models/vosk-model-small-en-us-0.15.zip" $zip) {
        Expand-Archive -Path $zip -DestinationPath $Models -Force
        $extracted = Join-Path $Models "vosk-model-small-en-us-0.15"
        if (Test-Path $extracted) {
            if (Test-Path $voskDir) { Remove-Item -Recurse -Force $voskDir }
            Rename-Item -Path $extracted -NewName "vosk-small-en"
        }
    }
}
if (Test-Path (Join-Path $voskDir "conf")) { Ok "wake-phrase model (Vosk) for 'wake up Jarvis'" } else { Warn "Vosk model missing; the voice wake word falls back to openWakeWord or the WAKE button." }

if (Download "https://storage.googleapis.com/mediapipe-models/hand_landmarker/hand_landmarker/float16/1/hand_landmarker.task" (Join-Path $Models "hand_landmarker.task")) {
    Ok "hand-tracking model"
} else { Warn "Hand-tracking model missing; gestures will try to download it when first used." }

if (Test-Path $VPy) {
    $env:PYTHONPATH = $Root
    & $VPy -c "from openwakeword import utils; utils.download_models(model_names=['hey_jarvis']); print('openWakeWord models ready')" | Out-Host
    Info "  preparing the speech recogniser (Whisper; first time only) ..."
    & $VPy -c "from voice.stt import Transcriber; t = Transcriber(); print('Whisper', t.model_name, 'ready' if t.available else 'NOT available')" | Out-Host
}

foreach ($cap in @("Language.TextToSpeech~~~en-GB~0.0.1.0", "Language.TextToSpeech~~~ko-KR~0.0.1.0")) {
    try {
        $state = (Get-WindowsCapability -Online -Name $cap -ErrorAction Stop).State
        if ($state -ne "Installed") { Add-WindowsCapability -Online -Name $cap -ErrorAction Stop | Out-Null }
        Ok "Windows voice pack $($cap.Split('~')[3])"
    } catch { Warn "Could not add Windows voice pack $cap ($($_.Exception.Message))." }
}

# ---------------------------------------------------------------------------- 7
Section 7 "Permissions and auto-start"
# JARVIS runs as the logged-on user, so make sure that user can write its records.
& icacls "$Data" /grant "*S-1-5-32-545:(OI)(CI)M" /T /C /Q | Out-Null
Ok "data folder writable by users"
if (Test-Path $VPy) {
    & powershell -NoProfile -ExecutionPolicy Bypass -File (Join-Path $Root "boot\register_autostart.ps1")
    if ($LASTEXITCODE -eq 0) { Ok "auto-start at logon registered" } else { Warn "Auto-start could not be registered." }
}

# ---------------------------------------------------------------------------- 8
Section 8 "Self-test"
if (Test-Path $VPy) { & $VPy (Join-Path $Root "scripts\healthcheck.py") | Out-Host }

# ---------------------------------------------------------------------------- 9
Section 9 "Launch"
$launchRc = 0
if ($NoLaunch -or $script:Fatal) {
    Info "  not launching (see the warnings below)."
} else {
    Stop-Jarvis
    $started = $false
    if (Get-ScheduledTask -TaskName "JARVIS" -ErrorAction SilentlyContinue) {
        try { Start-ScheduledTask -TaskName "JARVIS" -ErrorAction Stop; $started = $true } catch {}
    }
    if (-not $started) {
        $pw = Join-Path $Root ".venv\Scripts\pythonw.exe"
        Start-Process -FilePath $pw -ArgumentList "`"$(Join-Path $Root 'boot\jarvis_service.py')`"" -WorkingDirectory $Root
    }
    # Do not just hope: wait until the HUD is on screen and connected, or say why it is not.
    & $VPy (Join-Path $Root "boot\launcher.py") --wait --no-open | Out-Host
    $launchRc = $LASTEXITCODE
    if ($launchRc -eq 0) { Ok "JARVIS is on screen. Say 'wake up Jarvis' or press the WAKE button." }
    else { Warn "JARVIS is installed but did not come up on screen; the reason is printed above." }
}

Write-Host ""
if ($script:Warnings.Count -gt 0) {
    Write-Host "Finished with $($script:Warnings.Count) warning(s):" -ForegroundColor Yellow
    $script:Warnings | ForEach-Object { Write-Host "  - $_" -ForegroundColor Yellow }
} else {
    Write-Host "Finished without warnings." -ForegroundColor Green
}
Write-Host "Full log: $(Join-Path $Data 'install.log')"
try { Stop-Transcript | Out-Null } catch {}
if ($script:Fatal) { exit 1 } elseif ($launchRc -ne 0) { exit 2 } else { exit 0 }
