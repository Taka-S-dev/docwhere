<#
.SYNOPSIS
Installs (or updates) docwhere and copies its skills into an agent's skills folder.

.DESCRIPTION
1. Installs the docwhere command from this folder with uv (an editable install, so a later
   git pull takes effect at once; run this script again when dependencies change).
2. Copies each skills\<name>\SKILL.md into <SkillsDir>\<name>\. A copy that differs from the
   new one is kept next to it as SKILL.md.bak.
3. Checks the result with "docwhere doctor".

Run it from any folder:
    powershell -ExecutionPolicy Bypass -File <this folder>\install.ps1
    powershell -ExecutionPolicy Bypass -File <this folder>\install.ps1 -SkillsDir D:\agent\skills

.PARAMETER SkillsDir
The folder the agent loads skills from. Default: %USERPROFILE%\.copilot\skills.

.PARAMETER SkipTool
Only copy the skills; leave the docwhere command as it is.

.PARAMETER WhatIf
Show what would be done without changing anything.
#>
[CmdletBinding(SupportsShouldProcess = $true)]
param(
    [string]$SkillsDir = (Join-Path $env:USERPROFILE ".copilot\skills"),
    [switch]$SkipTool
)

$ErrorActionPreference = "Stop"
$repo = $PSScriptRoot

function Step([string]$text) { Write-Host "`n== $text" -ForegroundColor Cyan }

# ---- 1. the command ------------------------------------------------------------------------
if (-not $SkipTool) {
    Step "Installing the docwhere command"
    if (-not (Get-Command uv -ErrorAction SilentlyContinue)) {
        throw "uv is not installed. Install it first (https://docs.astral.sh/uv/), open a new terminal, then run this script again."
    }
    if ($PSCmdlet.ShouldProcess($repo, "uv tool install -e --force")) {
        & uv tool install -e $repo --force
        if ($LASTEXITCODE -ne 0) { throw "uv tool install failed (exit code $LASTEXITCODE)." }
    }
    if (-not (Get-Command docwhere -ErrorAction SilentlyContinue)) {
        Write-Warning "docwhere is installed but not on PATH yet. Run 'uv tool update-shell', open a new terminal, and run this script again."
    }
}

# ---- 2. the skills -------------------------------------------------------------------------
Step "Copying skills to $SkillsDir"
$copied = @()
foreach ($skill in Get-ChildItem (Join-Path $repo "skills") -Directory) {
    $source = Join-Path $skill.FullName "SKILL.md"
    if (-not (Test-Path $source)) { continue }
    $targetDir = Join-Path $SkillsDir $skill.Name
    $target = Join-Path $targetDir "SKILL.md"
    if ((Test-Path $target) -and ((Get-FileHash $target).Hash -eq (Get-FileHash $source).Hash)) {
        Write-Host "  $($skill.Name): already current"
        $copied += $target
        continue
    }
    if ($PSCmdlet.ShouldProcess($target, "copy $source")) {
        New-Item -ItemType Directory -Force -Path $targetDir | Out-Null
        if (Test-Path $target) {
            Copy-Item $target "$target.bak" -Force
            Write-Host "  $($skill.Name): updated (the previous copy is SKILL.md.bak)"
        } else {
            Write-Host "  $($skill.Name): added"
        }
        Copy-Item $source $target -Force
    }
    $copied += $target
}
if (-not $copied) { Write-Warning "No skills\<name>\SKILL.md found under $repo." }

# ---- 3. the check --------------------------------------------------------------------------
if ($WhatIfPreference) { return }
if (Get-Command docwhere -ErrorAction SilentlyContinue) {
    Step "Checking (docwhere doctor; Office checks are skipped here, run 'docwhere doctor' for them)"
    $arguments = @("doctor", "--no-office")
    foreach ($path in $copied) { $arguments += @("--skill", $path) }
    & docwhere @arguments
    if ($LASTEXITCODE -ne 0) {
        Write-Warning "Some checks failed (FAIL lines above)."
    }
}
Write-Host "`nDone. Restart the agent (or open a new chat) so it loads the updated skills."
