<#
.SYNOPSIS
Installs (or updates) docwhere and copies its skills into an agent's skills folder.

.DESCRIPTION
1. Installs the docwhere command from this folder with uv (an editable install, so a later
   git pull takes effect at once; run this script again when dependencies change).
2. Copies each skills\<name>\SKILL.md, with the references\ files it tells the agent to read,
   into <SkillsDir>\<name>\. A SKILL.md that differs from the new one is kept as SKILL.md.bak,
   and a references\ file this folder no longer has is removed.
3. Checks the result with "docwhere doctor".

Run it from any folder:
    powershell -ExecutionPolicy Bypass -File <this folder>\install.ps1
    powershell -ExecutionPolicy Bypass -File <this folder>\install.ps1 -SkillsDir D:\agent\skills

.PARAMETER SkillsDir
The folder (or folders) an agent loads skills from. Default: %USERPROFILE%\.copilot\skills, and
every other %USERPROFILE%\.<agent>\skills folder that already holds a copy of one of these skills.

.PARAMETER SkipTool
Only copy the skills; leave the docwhere command as it is.

.PARAMETER WhatIf
Show what would be done without changing anything.
#>
[CmdletBinding(SupportsShouldProcess = $true)]
param(
    [string[]]$SkillsDir,
    [switch]$SkipTool
)

$ErrorActionPreference = "Stop"
$repo = $PSScriptRoot

function Step([string]$text) { Write-Host "`n== $text" -ForegroundColor Cyan }

if (-not $SkillsDir) {
    # Another agent's folder that already holds one of these skills is updated too: a copy left
    # there went on being read after it was outdated.
    $first = Join-Path $env:USERPROFILE ".copilot\skills"
    $names = @(Get-ChildItem (Join-Path $repo "skills") -Directory | ForEach-Object { $_.Name })
    $held = @(Get-ChildItem $env:USERPROFILE -Directory -Force -Filter ".*" -ErrorAction SilentlyContinue |
        ForEach-Object { Join-Path $_.FullName "skills" } |
        Where-Object { $dir = $_; $dir -ne $first -and @($names | Where-Object { Test-Path (Join-Path $dir $_) }).Count })
    $SkillsDir = @($first) + $held
}

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
$copied = @()
foreach ($dir in $SkillsDir) {
Step "Copying skills to $dir"
foreach ($skill in Get-ChildItem (Join-Path $repo "skills") -Directory) {
    $source = Join-Path $skill.FullName "SKILL.md"
    if (-not (Test-Path $source)) { continue }
    $targetDir = Join-Path $dir $skill.Name
    $target = Join-Path $targetDir "SKILL.md"
    # SKILL.md and the files it tells the agent to read (references\); evals\ is for development
    $files = @(Get-Item $source)
    $refs = Join-Path $skill.FullName "references"
    if (Test-Path $refs) { $files += Get-ChildItem $refs -File -Recurse }
    $changed = @($files | Where-Object {
        $to = Join-Path $targetDir $_.FullName.Substring($skill.FullName.Length + 1)
        -not (Test-Path $to) -or (Get-FileHash $to).Hash -ne (Get-FileHash $_.FullName).Hash
    })
    # a references\ file that was renamed or dropped here would stay there, to be read as current
    $targetRefs = Join-Path $targetDir "references"
    if (Test-Path $targetRefs) {
        $kept = @($files | ForEach-Object { $_.FullName.Substring($skill.FullName.Length + 1) })
        foreach ($old in Get-ChildItem $targetRefs -File -Recurse) {
            $relative = $old.FullName.Substring($targetDir.Length + 1)
            if ($kept -notcontains $relative -and $PSCmdlet.ShouldProcess($old.FullName, "remove (no longer part of the skill)")) {
                Remove-Item $old.FullName -Force
                Write-Host "  $($skill.Name): removed $relative"
            }
        }
    }
    if (-not $changed) {
        Write-Host "  $($skill.Name): already current"
        $copied += $target
        continue
    }
    if ($PSCmdlet.ShouldProcess($targetDir, "copy $($changed.Count) file(s) from $($skill.FullName)")) {
        $existed = Test-Path $target
        if ($existed -and ($changed.FullName -contains $source)) {
            Copy-Item $target "$target.bak" -Force
        }
        foreach ($file in $changed) {
            $to = Join-Path $targetDir $file.FullName.Substring($skill.FullName.Length + 1)
            New-Item -ItemType Directory -Force -Path (Split-Path $to) | Out-Null
            Copy-Item $file.FullName $to -Force
        }
        $what = if (-not $existed) { "added" } elseif ($changed.FullName -contains $source) { "updated (the previous SKILL.md is SKILL.md.bak)" } else { "updated" }
        Write-Host "  $($skill.Name): $what, $($changed.Count) file(s)"
    }
    $copied += $target
}
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
