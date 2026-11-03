<#
  setup_windows.ps1 — one-shot installer run by INSTALL_JARVIS.bat (double-click).

  Installs everything JARVIS needs and then starts it, so from one double-click it runs now
  and permanently thereafter:
    1. Python 3.11+  (+ virtualenv, pip deps)         -> backend, neural core, voice
    2. Node.js LTS   (+ npm deps: electron, three)    -> holographic overlay + 3D space
    3. Ollama        (+ a model sized to this PC)      -> the offline language brain
    4. voices/ + models/ assets (British TTS, hands)  -> voice + gestures
    5. auto-start registration                         -> runs at every logon
    6. launch now

  Network installs use winget; each step is best-effort and prints what it did. Missing
  pieces degrade gracefully (text still works without a mic, etc.).
#>
$ErrorActionPreference = "Continue"
$Root = Split-Path -Parent (Split-Path -Parent $MyInvocation.MyCommand.Path)
Set-Location $Root

function Section($n, $t) { Write-Host "`n==== [$n] $t ====" -ForegroundColor Cyan }
function Have($cmd) { return [bool](Get-Command $cmd -ErrorAction SilentlyContinue) }
function Winget($id) {
  if (Have "winget") {
    Write-Host "  installing $id via winget (est. 1-4 min)..."
    winget install --id $id -e --accept-source-agreements --accept-package-agreements --silent
  } else { Write-Host "  winget not available; please install $id manually." -ForegroundColor Yellow }
}

Section 1 "Python"
if (-not (Have "python") -and -not (Have "py")) { Winget "Python.Python.3.11" }
$py = if (Have "py") { "py -3" } elseif (Have "python") { "python" } else { $null }
if ($py) {
  Write-Host "  creating virtual environment (.venv)..."
  iex "$py -m venv `"$Root\.venv`""
  $vpy = Join-Path $Root ".venv\Scripts\python.exe"
  & $vpy -m pip install --upgrade pip
  Write-Host "  installing Python dependencies (est. 2-5 min)..."
  & $vpy -m pip install -r (Join-Path $Root "requirements.txt")
  Write-Host "  installing Playwright browser (optional, for web reading)..."
  & $vpy -m playwright install chromium 2>$null
} else { Write-Host "  Python not found; backend cannot run until Python 3.11+ is installed." -ForegroundColor Yellow }

Section 2 "Node.js + overlay"
if (-not (Have "node")) { Winget "OpenJS.NodeJS.LTS" }
if (Have "npm") {
  Write-Host "  installing overlay dependencies (electron, three) (est. 2-6 min)..."
  Push-Location $Root; npm install --no-audit --no-fund; Pop-Location
} else { Write-Host "  Node.js not found; the holographic overlay won't launch until it's installed." -ForegroundColor Yellow }

Section 3 "Ollama (local language brain)"
if (-not (Have "ollama")) { Winget "Ollama.Ollama" }
if (Have "ollama") {
  Write-Host "  starting Ollama service..."
  Start-Process -FilePath "ollama" -ArgumentList "serve" -WindowStyle Hidden -ErrorAction SilentlyContinue
  Start-Sleep -Seconds 3
  $ram = [math]::Round((Get-CimInstance Win32_ComputerSystem).TotalPhysicalMemory / 1GB)
  $model = if ($ram -ge 32) { "qwen2.5:14b-instruct" } elseif ($ram -ge 16) { "llama3.1:8b-instruct-q4_K_M" } else { "qwen2.5:3b-instruct" }
  Write-Host "  detected ${ram} GB RAM -> pulling model '$model' (large download, est. 3-15 min)..."
  ollama pull $model
} else { Write-Host "  Ollama not found; JARVIS will use offline fallback replies until it's installed." -ForegroundColor Yellow }

Section 4 "Voice + gesture assets"
New-Item -ItemType Directory -Force -Path (Join-Path $Root "voices"), (Join-Path $Root "models") | Out-Null
function Fetch($url, $out) {
  if (Test-Path $out) { Write-Host "  have $(Split-Path $out -Leaf)"; return }
  try { Write-Host "  downloading $(Split-Path $out -Leaf)..."; Invoke-WebRequest -Uri $url -OutFile $out -UseBasicParsing }
  catch { Write-Host "  (skipped $(Split-Path $out -Leaf): $($_.Exception.Message))" -ForegroundColor Yellow }
}
$piper = "https://huggingface.co/rhasspy/piper-voices/resolve/main/en/en_GB/alan/medium"
Fetch "$piper/en_GB-alan-medium.onnx"      (Join-Path $Root "voices\en_GB-alan-medium.onnx")
Fetch "$piper/en_GB-alan-medium.onnx.json" (Join-Path $Root "voices\en_GB-alan-medium.onnx.json")
Fetch "https://storage.googleapis.com/mediapipe-models/hand_landmarker/hand_landmarker/float16/1/hand_landmarker.task" (Join-Path $Root "models\hand_landmarker.task")
Write-Host "  (A custom 'wake up jarvis' model can be trained later; openWakeWord ships usable defaults.)"

Section 5 "Auto-start at logon"
& powershell -NoProfile -ExecutionPolicy Bypass -File (Join-Path $Root "boot\register_autostart.ps1")

Section 6 "Launch now"
$pw = Join-Path $Root ".venv\Scripts\pythonw.exe"
if (-not (Test-Path $pw)) { $pw = "pythonw" }
Start-Process -FilePath $pw -ArgumentList "`"$(Join-Path $Root 'boot\jarvis_service.py')`"" -WorkingDirectory $Root
Write-Host "`nJARVIS is installed and starting. Say 'wake up Jarvis' or press the WAKE button." -ForegroundColor Green
