<#
install.ps1 — symlink this repo into $HERMES_HOME/plugins/contextpilot-lcm-bridge

Usage:
  powershell -ExecutionPolicy Bypass -File scripts/install.ps1

HERMES_HOME resolution order:
  1. $env:HERMES_HOME          (if set and non-empty)
  2. $env:LOCALAPPDATA\hermes  (Windows)
  3. $HOME\.hermes             (POSIX-style fallback)

Manual copy fallback (no symlink privileges — e.g. missing admin rights or
Windows Developer Mode):
  Copy-Item -Recurse -Force . "$env:HERMES_HOME\plugins\contextpilot-lcm-bridge"
This script also falls back to a copy automatically if the symlink cannot be
created.

No hardcoded user or machine paths in this script.
#>

$ErrorActionPreference = "Stop"

$PluginName = "contextpilot-lcm-bridge"
$RepoRoot   = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path

# --- Resolve HERMES_HOME ---
$hermesHome = $env:HERMES_HOME
if ([string]::IsNullOrWhiteSpace($hermesHome)) {
    if (-not [string]::IsNullOrWhiteSpace($env:LOCALAPPDATA)) {
        $hermesHome = Join-Path $env:LOCALAPPDATA "hermes"
    } elseif (-not [string]::IsNullOrWhiteSpace($env:HOME)) {
        $hermesHome = Join-Path $env:HOME ".hermes"
    } else {
        Write-Host "error: HERMES_HOME is not set and no fallback location could be determined." -ForegroundColor Red
        Write-Host "Set HERMES_HOME to your Hermes home directory and re-run this script."
        exit 1
    }
    Write-Host "note: HERMES_HOME not set; using $hermesHome"
}

$pluginsDir = Join-Path $hermesHome "plugins"
$target     = Join-Path $pluginsDir $PluginName
New-Item -ItemType Directory -Force -Path $pluginsDir | Out-Null

# --- Handle an existing target ---
$item = Get-Item $target -ErrorAction SilentlyContinue
if ($null -ne $item) {
    if ($item.LinkType -eq "SymbolicLink") {
        $existing = $item.Target
        if ($existing -eq $RepoRoot) {
            Write-Host "already installed: $target -> $existing"
            exit 0
        }
        Write-Host "error: $target is already a symlink to $existing (not this repo)." -ForegroundColor Red
        Write-Host "Remove it manually, then re-run this script."
        exit 1
    }
    $backup = "$target.bak.$(Get-Date -Format 'yyyyMMddHHmmss')"
    Write-Host "backing up existing plugin directory to $backup"
    Move-Item $target $backup
}

# --- Create the symlink; fall back to a copy ---
try {
    New-Item -ItemType SymbolicLink -Path $target -Target $RepoRoot | Out-Null
    Write-Host "installed: $target -> $RepoRoot"
} catch {
    Write-Warning "Could not create symlink ($($_.Exception.Message)). Falling back to a copy."
    Copy-Item -Recurse -Force $RepoRoot $target
    Write-Host "installed (copy): $target"
}

# --- Next steps ---
Write-Host ""
Write-Host "Next steps:"
Write-Host "  1. Add `"$PluginName`" to plugins.enabled in your Hermes config."
Write-Host "  2. Set context.engine to `"$PluginName`"."
Write-Host "  3. Restart Hermes (or reload plugins) and verify with: hermes plugins"
