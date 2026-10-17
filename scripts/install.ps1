<#
.SYNOPSIS
  callmaibe installer for Windows (PowerShell 5.1 or newer). No admin rights, no sudo.

.DESCRIPTION
  1. Ensures uv on Windows.
  2. Installs callmaibe[speech,local] on Windows (MCP server, CLI, hooks, local mic/speaker mode).
  3. Checks WSL2 and a distro. Real SIP calls run in WSL because the Windows pjsua2 wheels have no TLS.
  4. Inside WSL: fetches uv (no sudo), creates ~/.venvs/callmaibe, installs callmaibe[sip,speech].
  5. Optionally sets networkingMode=mirrored in ~/.wslconfig (asks first, makes a backup).
  6. Runs `callmaibe setup`, the interactive wizard (skipped with -Yes or -DryRun).

  Safe to re-run.

.PARAMETER Source
  Package source. Default: the GitHub archive (no git needed inside WSL).
  Env var CALLMAIBE_SOURCE works too. A local folder (a checkout) is fine for development.
.PARAMETER Distro
  WSL distro to use. Default: the WSL default distro.
.PARAMETER DryRun
  Print what would run, change nothing. Read-only checks still run. Prompts are assumed "yes".
.PARAMETER Yes
  Do not ask questions; assume yes.

.EXAMPLE
  powershell -NoProfile -ExecutionPolicy Bypass -File scripts\install.ps1 -DryRun
#>
[CmdletBinding()]
param(
    [string]$Source = $(if ($env:CALLMAIBE_SOURCE) { $env:CALLMAIBE_SOURCE } else { 'https://github.com/mnaomii/callmaibe/archive/refs/heads/main.zip' }),
    [string]$Distro = '',
    [switch]$DryRun,
    [switch]$Yes
)

$ErrorActionPreference = 'Stop'
$UvLinuxUrl = 'https://github.com/astral-sh/uv/releases/latest/download/uv-x86_64-unknown-linux-gnu.tar.gz'

function Step($msg) { Write-Host ""; Write-Host "==> $msg" -ForegroundColor Cyan }
function Info($msg) { Write-Host "    $msg" }
function Warn($msg) { Write-Host "    ! $msg" -ForegroundColor Yellow }

function Ask($question) {
    if ($Yes -or $DryRun) { Info "$question -> yes (assumed)"; return $true }
    $r = Read-Host "    $question [y/N]"
    return ($r -match '^(y|yes)$')
}

# Run an external command, or just print it in dry-run mode.
function Invoke-Cmd {
    param([string]$Exe, [string[]]$CmdArgs)
    Info ("> " + $Exe + " " + ($CmdArgs -join ' '))
    if ($DryRun) { return }
    & $Exe @CmdArgs
    if ($LASTEXITCODE -ne 0) { throw "$Exe failed with exit code $LASTEXITCODE" }
}

# wsl.exe prints UTF-16; PowerShell shows NULs between characters. Strip them.
function Get-WslText {
    param([string[]]$WslArgs)
    $raw = & wsl.exe @WslArgs 2>&1
    $code = $LASTEXITCODE
    $lines = @($raw | ForEach-Object { ([string]$_).Replace([string][char]0, '').Trim() } | Where-Object { $_ -ne '' })
    return @{ Code = $code; Lines = $lines }
}

# "C:\a\b" -> "/mnt/c/a/b"
function ConvertTo-WslPath([string]$p) {
    $p = (Resolve-Path -LiteralPath $p).Path
    $drive = $p.Substring(0, 1).ToLower()
    return "/mnt/$drive/" + $p.Substring(3).Replace('\', '/')
}

# A local path source needs a file:// URL for "name @ url". Remote URLs stay as they are.
$IsLocal = Test-Path -LiteralPath $Source
if ($IsLocal) {
    $WinSource = ([Uri](Resolve-Path -LiteralPath $Source).Path).AbsoluteUri
    $WslSource = 'file://' + (ConvertTo-WslPath $Source)
} else {
    $WinSource = $Source
    $WslSource = $Source
}

if ($DryRun) { Write-Host "DRY RUN: nothing will be changed." -ForegroundColor Yellow }
Info "package source: $Source"

# ---------------------------------------------------------------- 1. uv on Windows
Step "1/6 Checking for uv on Windows"
$uvBin = Join-Path $env:USERPROFILE '.local\bin'
if (-not (Get-Command uv -ErrorAction SilentlyContinue)) { $env:Path = "$uvBin;$env:Path" }
if (Get-Command uv -ErrorAction SilentlyContinue) {
    Info ("found " + (Get-Command uv).Source)
} else {
    Info "uv is not installed. It manages Python 3.12 and the callmaibe install."
    $hasWinget = [bool](Get-Command winget -ErrorAction SilentlyContinue)
    $q = if ($hasWinget) { 'Install uv with winget (winget install --id astral-sh.uv -e)?' }
         else { 'Install uv with the official script (irm https://astral.sh/uv/install.ps1 | iex)?' }
    if (-not (Ask $q)) { throw "uv is required. Install it from https://docs.astral.sh/uv/ and re-run." }
    if ($hasWinget) {
        Invoke-Cmd 'winget' @('install', '--id', 'astral-sh.uv', '-e')
    } else {
        Info '> irm https://astral.sh/uv/install.ps1 | iex'
        if (-not $DryRun) { Invoke-RestMethod https://astral.sh/uv/install.ps1 | Invoke-Expression }
    }
    if (-not $DryRun) {
        $env:Path = "$uvBin;$env:Path"
        if (-not (Get-Command uv -ErrorAction SilentlyContinue)) {
            throw "uv was installed but is not on PATH yet. Open a new terminal and re-run."
        }
    }
}

# ---------------------------------------------------------------- 2. Windows side
Step "2/6 Installing callmaibe[speech,local] on Windows"
Invoke-Cmd 'uv' @('tool', 'install', '--python', '3.12', '--force', "callmaibe[speech,local] @ $WinSource")
Invoke-Cmd 'uv' @('tool', 'update-shell')

# ---------------------------------------------------------------- 3. WSL check
Step "3/6 Checking WSL2 and a Linux distro"
$wslOk = $false
$distroArgs = @()
if (-not (Get-Command wsl.exe -ErrorAction SilentlyContinue)) {
    Warn "wsl.exe not found."
} else {
    $st = Get-WslText @('--status')
    $list = Get-WslText @('-l', '-q')
    $distros = @()
    if ($list.Code -eq 0) { $distros = @($list.Lines | Where-Object { $_ -notmatch '^docker-desktop' }) }
    if ($st.Code -ne 0) {
        Warn "WSL is not set up (wsl --status failed)."
    } elseif ($distros.Count -eq 0) {
        Warn "WSL has no Linux distro installed."
    } elseif ($Distro -and ($distros -notcontains $Distro)) {
        Warn "Distro '$Distro' not found. Installed: $($distros -join ', ')"
    } else {
        if ($Distro) { $distroArgs = @('-d', $Distro) }
        $probe = Get-WslText ($distroArgs + @('--exec', 'uname', '-m'))
        if ($probe.Code -eq 0) {
            $wslOk = $true
            $arch = ($probe.Lines | Select-Object -Last 1)
            Info ("using WSL distro: " + $(if ($Distro) { $Distro } else { "default ($($distros[0]))" }) + " ($arch)")
            if ($arch -ne 'x86_64') { Warn "uv download below is for x86_64; this distro reports $arch." }
        } else {
            Warn "Could not run a command in the distro: $($probe.Lines -join ' ')"
        }
    }
}
if (-not $wslOk) {
    Write-Host ""
    Write-Host "Real phone calls need WSL2 with a Linux distro. To install one:" -ForegroundColor Yellow
    Write-Host "    wsl --install -d Debian      (run in an ADMIN PowerShell, then reboot)"
    Write-Host "Then re-run this script. The Windows side is installed, so local"
    Write-Host "mic/speaker mode already works:  callmaibe setup --backend local"
    if (-not $DryRun) { exit 0 }
    Warn "(dry run: continuing to show the WSL steps anyway)"
}

# ---------------------------------------------------------------- 4. Inside WSL
Step "4/6 Installing callmaibe[sip,speech] inside WSL (no sudo)"
$bash = @'
set -e
mkdir -p "$HOME/.local/bin"
if [ ! -x "$HOME/.local/bin/uv" ]; then
  echo "downloading uv"
  URL='__UVURL__'
  if command -v curl >/dev/null 2>&1; then curl -LsSf "$URL" -o /tmp/uv.tar.gz
  elif command -v wget >/dev/null 2>&1; then wget -qO /tmp/uv.tar.gz "$URL"
  elif command -v python3 >/dev/null 2>&1; then
    python3 -c 'import sys,urllib.request; urllib.request.urlretrieve(sys.argv[1], "/tmp/uv.tar.gz")' "$URL"
  else echo "no curl, wget or python3 in this distro; cannot fetch uv" >&2; exit 1; fi
  tar -xzf /tmp/uv.tar.gz -C "$HOME/.local/bin" --strip-components=1
  rm -f /tmp/uv.tar.gz
fi
export PATH="$HOME/.local/bin:$PATH"
if [ ! -x "$HOME/.venvs/callmaibe/bin/python" ]; then
  uv venv --python 3.12 "$HOME/.venvs/callmaibe"
fi
uv pip install --python "$HOME/.venvs/callmaibe/bin/python" 'callmaibe[sip,speech] @ __SRC__'
"$HOME/.venvs/callmaibe/bin/python" -c 'import callmaibe; print("callmaibe installed in WSL")'
'@
$bash = $bash.Replace('__UVURL__', $UvLinuxUrl).Replace('__SRC__', $WslSource).Replace("`r`n", "`n")
# Pass the script base64-encoded so neither wsl.exe nor PowerShell can touch the quotes or $ signs.
$b64 = [Convert]::ToBase64String([Text.Encoding]::UTF8.GetBytes($bash))
$wslCmd = $distroArgs + @('--exec', 'bash', '-c', "echo $b64 | base64 -d | bash")
if ($DryRun) {
    Info "> wsl.exe $($distroArgs -join ' ') --exec bash -c '<script below, base64-piped>'"
    $bash -split "`n" | ForEach-Object { Info "  | $_" }
} else {
    & wsl.exe @wslCmd
    if ($LASTEXITCODE -ne 0) { throw "The WSL install failed (exit code $LASTEXITCODE)." }
}

# ---------------------------------------------------------------- 5. mirrored networking
Step "5/6 WSL mirrored networking (recommended)"
$cfgPath = Join-Path $env:USERPROFILE '.wslconfig'
$existing = if (Test-Path $cfgPath) { Get-Content -LiteralPath $cfgPath } else { @() }
if ($existing -match '^\s*networkingMode\s*=\s*mirrored\s*$') {
    Info "already set: networkingMode=mirrored"
} else {
    Info "Mirrored mode lets WSL share the Windows network, which helps SIP/RTP get through NAT."
    Info "It edits $cfgPath (a backup is made) and needs 'wsl --shutdown' to take effect."
    if (Ask "Set networkingMode=mirrored under [wsl2]?") {
        $out = New-Object System.Collections.Generic.List[string]
        $inWsl2 = $false; $done = $false
        foreach ($line in $existing) {
            if ($line -match '^\s*\[(.+)\]\s*$') {
                if ($inWsl2 -and -not $done) { $out.Add('networkingMode=mirrored'); $done = $true }
                $inWsl2 = ($Matches[1].Trim().ToLower() -eq 'wsl2')
                $out.Add($line)
                if ($inWsl2 -and -not $done) { $out.Add('networkingMode=mirrored'); $done = $true }
            } elseif ($inWsl2 -and $line -match '^\s*networkingMode\s*=') {
                if (-not $done) { $out.Add('networkingMode=mirrored'); $done = $true }
            } else { $out.Add($line) }
        }
        if (-not $done) { $out.Add('[wsl2]'); $out.Add('networkingMode=mirrored') }
        if ($DryRun) {
            Info "would write $cfgPath :"
            $out | ForEach-Object { Info "  | $_" }
        } else {
            if (Test-Path $cfgPath) {
                $bak = "$cfgPath.callmaibe-backup-" + (Get-Date -Format 'yyyyMMdd-HHmmss')
                Copy-Item -LiteralPath $cfgPath -Destination $bak
                Info "backup: $bak"
            }
            [IO.File]::WriteAllText($cfgPath, (($out -join "`r`n") + "`r`n"), (New-Object Text.UTF8Encoding($false)))
            Info "written. Run:  wsl --shutdown   (this stops all WSL distros), then start your distro again."
        }
    } else {
        Info "skipped."
    }
}

# ---------------------------------------------------------------- 6. setup wizard
Step "6/6 Setting up callmaibe (addresses, password, backend, clients)"
$cm = Join-Path (& uv tool dir --bin 2>$null) 'callmaibe.exe'
if (-not (Test-Path $cm)) { $cm = 'callmaibe' }
if ($Yes -or $DryRun) {
    # No questions: record the WSL runner only. Run `callmaibe setup` later for the rest.
    $setupArgs = @('setup', '--yes', '--backend', 'pjsua2', '--runner', 'wsl')
    if ($Distro) { $setupArgs += @('--wsl-distro', $Distro) }
    Invoke-Cmd $cm $setupArgs
    Write-Host ""
    Write-Host "Next: open a new terminal and run  callmaibe setup  (SIP addresses, password, clients)."
} else {
    $setupArgs = @('setup')
    if ($Distro) { $setupArgs += @('--wsl-distro', $Distro) }
    Info ("> " + $cm + " " + ($setupArgs -join ' '))
    & $cm @setupArgs
    if ($LASTEXITCODE -ne 0) { Warn "callmaibe setup exited with code $LASTEXITCODE; re-run it any time: callmaibe setup" }
}
