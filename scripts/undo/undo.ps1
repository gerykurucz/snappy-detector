<#
.SYNOPSIS
    SnappyAI undo. Restores state from the snapshot written by the apply script.

.DESCRIPTION
    Restores the values recorded in ..\exec\snapshot.json, which are the values observed on THIS
    machine before the change, not generic defaults. If the snapshot is missing, this
    script changes nothing rather than guessing at a default.

    Preview what it would do:  .\..\undo\undo.ps1 -WhatIf2
#>
#Requires -Version 5.1
[CmdletBinding()]
param([switch]$WhatIf2, [switch]$NoPause, [switch]$Force, [switch]$SkipManual)

$ErrorActionPreference = 'Stop'
$script:Here = Split-Path -Parent $MyInvocation.MyCommand.Path
$script:Snapshot = Join-Path $Here '..\exec\snapshot.json'
$script:Interactive = [Environment]::UserInteractive

function Exit-Exit-SnappyAI {
    param([int]$Code = 0)
    if ($script:Interactive -and -not $NoPause -and -not $WhatIf2) {
        Write-Host ''
        Read-Host 'Press Enter to close this window'
    }
    exit $Code
}

if (-not (Test-Path -LiteralPath $script:Snapshot)) {
    Write-Host "Snapshot not found at $script:Snapshot" -ForegroundColor Red
    Write-Host "Nothing to undo, and SnappyAI will not guess at your prior values." -ForegroundColor Red
    Exit-Exit-SnappyAI 1
}

$raw = Get-Content -LiteralPath $script:Snapshot -Raw
$changes = @($raw | ConvertFrom-Json)
if ($changes.Count -eq 0 -or ($changes.Count -eq 1 -and $null -eq $changes[0].tweak)) {
    Write-Host "Snapshot is empty; nothing was applied." -ForegroundColor Yellow
    Exit-Exit-SnappyAI 0
}

Write-Host ""
Write-Host "SnappyAI undo" -ForegroundColor Magenta
Write-Host ("Restoring {0} recorded change(s), newest first." -f $changes.Count)
Write-Host ""

$ordered = @($changes)
[array]::Reverse($ordered)

$failed = 0
foreach ($c in $ordered) {
    Write-Host ("[{0}] {1}" -f $c.tweak, $c.description) -ForegroundColor Cyan
    if ($WhatIf2) {
        Write-Host "    [preview] would restore prior value" -ForegroundColor DarkGray
        continue
    }
    try {
        if (-not $c.undoScript) {
            Write-Host "    no undo recorded for this change" -ForegroundColor Yellow
            continue
        }
        $undo = [scriptblock]::Create($c.undoScript)
        & $undo
        Write-Host "    restored" -ForegroundColor Green
    } catch {
        Write-Host ("    FAILED to restore: {0}" -f $_.Exception.Message) -ForegroundColor Red
        $failed++
    }
}

Write-Host ""
if ($failed -gt 0) {
    Write-Host ("{0} change(s) could not be restored. Review them manually." -f $failed) -ForegroundColor Red
    Exit-Exit-SnappyAI 1
}
Write-Host "All recorded changes restored." -ForegroundColor Green
if (@($changes | Where-Object { $_.requiresReboot }).Count -gt 0) {
    Write-Host "Some of these changes require a reboot to fully unwind." -ForegroundColor Yellow
}
Exit-Exit-SnappyAI 0
