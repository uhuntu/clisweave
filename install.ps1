<#
.SYNOPSIS
Installs Clisweave's commands (ai, ai-sessions, plus compatibility aliases) as native
.cmd launchers on PATH -- no Git Bash/WSL/Cygwin required.

Run after cloning:
  .\install.ps1

Or as a one-liner, which clones the repo first:
  irm https://raw.githubusercontent.com/uhuntu/clisweave/master/install.ps1 | iex
#>

$ErrorActionPreference = "Stop"
# PS 7.3+ otherwise turns any stderr output from the python/git probes below
# into a terminating error even on exit code 0; harmless to set on older
# versions, where this preference variable doesn't exist yet.
$PSNativeCommandUseErrorActionPreference = $false

$RepoUrl = "https://github.com/uhuntu/clisweave.git"
$BinDir = if ($env:CLISWEAVE_BIN_DIR) { $env:CLISWEAVE_BIN_DIR } elseif ($env:AIMUX_BIN_DIR) { $env:AIMUX_BIN_DIR } else { Join-Path $HOME ".local\bin" }

# When run via `irm | iex` there's no script file on disk, so $PSCommandPath
# is empty -- guard the lookup instead of assuming a local clone, same idea
# as install.sh's BASH_SOURCE[0] check.
if ($PSCommandPath -and (Test-Path (Join-Path (Split-Path $PSCommandPath -Parent) "bin\ai"))) {
    $ScriptDir = Split-Path $PSCommandPath -Parent
} else {
    if (-not (Get-Command git -ErrorAction SilentlyContinue)) {
        Write-Error "clisweave: git is required to fetch the repo. Install Git for Windows and re-run."
        exit 1
    }
    $RepoDir = if ($env:CLISWEAVE_REPO_DIR) { $env:CLISWEAVE_REPO_DIR } elseif ($env:AIMUX_REPO_DIR) { $env:AIMUX_REPO_DIR } else { Join-Path $HOME ".local\share\clisweave" }
    if (Test-Path (Join-Path $RepoDir ".git")) {
        git -C $RepoDir pull --ff-only
    } else {
        git clone --depth 1 $RepoUrl $RepoDir
    }
    & (Join-Path $RepoDir "install.ps1")
    exit $LASTEXITCODE
}

New-Item -ItemType Directory -Force -Path $BinDir | Out-Null

# Windows ships `python.exe`/`py.exe`, not `python3.exe` -- and the Microsoft
# Store's `python3` PATH alias is a no-op stub that exits without doing
# anything even when a real interpreter is installed under a different name.
# Probe by actually running each candidate rather than trusting PATH presence.
function Get-WorkingPython {
    foreach ($cmd in @("python", "python3")) {
        $exe = Get-Command $cmd -ErrorAction SilentlyContinue
        if ($exe) {
            & $exe.Source -c "pass" 2>$null | Out-Null
            if ($LASTEXITCODE -eq 0) { return @{ Path = $exe.Source; Args = @() } }
        }
    }
    $py = Get-Command py -ErrorAction SilentlyContinue
    if ($py) {
        & $py.Source -3 -c "pass" 2>$null | Out-Null
        if ($LASTEXITCODE -eq 0) { return @{ Path = $py.Source; Args = @("-3") } }
    }
    return $null
}

$Python = Get-WorkingPython
if (-not $Python) {
    Write-Error "clisweave: no working Python 3 interpreter found (tried python, python3, py -3). Install Python from https://python.org and re-run."
    exit 1
}
$PyArgsStr = ($Python.Args -join " ")

# ai/ai-sessions are plain Python scripts with a `#!/usr/bin/env python3`
# shebang, which only Git Bash/WSL/msys know how to run. A .cmd launcher
# runs natively from both cmd.exe and PowerShell (PATHEXT covers .cmd), and
# always invokes the *current* file at $Target -- nothing is copied, so a
# later `git pull` in the clone is picked up automatically, same promise
# install.sh makes with its symlinks.
function Write-Launcher($Name, $Target) {
    $cmdPath = Join-Path $BinDir "$Name.cmd"
    $pyPart = if ($PyArgsStr) { "`"$($Python.Path)`" $PyArgsStr" } else { "`"$($Python.Path)`"" }
    $lines = @(
        "@echo off"
        "$pyPart `"$Target`" %*"
    )
    Set-Content -Path $cmdPath -Value $lines -Encoding oem
    Write-Host "linked $cmdPath -> $Target"
}

Write-Launcher "ai" (Join-Path $ScriptDir "bin\ai")
Write-Launcher "ai-sessions" (Join-Path $ScriptDir "bin\ai-sessions")
Write-Launcher "clisweave" (Join-Path $ScriptDir "bin\ai")
Write-Launcher "aim" (Join-Path $ScriptDir "bin\ai")
Write-Launcher "aimux" (Join-Path $ScriptDir "bin\ai")

$pathDirs = $env:PATH -split ";"
if ($pathDirs -notcontains $BinDir) {
    Write-Host "Note: $BinDir is not on your PATH. Add it, e.g.:"
    Write-Host "  [Environment]::SetEnvironmentVariable('PATH', `"$BinDir;`$env:PATH`", 'User')"
    Write-Host "  (then restart your terminal)"
}

Write-Host "Done. Try: ai --help"
