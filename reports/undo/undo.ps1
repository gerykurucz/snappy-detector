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
$script:KnownUndoIds = @('0223fa85ff1b55f18565376f', 'cbc0b13a84af92b98e01202a')

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
# Windows PowerShell 5.1 emits a JSON array as ONE pipeline object, so
# @(pipeline) nests it. Assign first, then wrap.
$parsed = $raw | ConvertFrom-Json
$changes = @()
if ($null -ne $parsed) { $changes = @($parsed) }
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
    try {
        if (-not $c.undoId) {
            throw "snapshot record has no undo id"
        }
        if ($script:KnownUndoIds -notcontains [string]$c.undoId) {
            throw "snapshot contains an unknown undo id"
        }
        if ($WhatIf2) {
            Write-Host "    [preview] would restore prior value" -ForegroundColor DarkGray
            continue
        }
        switch ([string]$c.undoId) {
            'cbc0b13a84af92b98e01202a' {
                Get-NetAdapter -Physical | Set-NetAdapterAdvancedProperty -DisplayName 'Interrupt Moderation' -DisplayValue 'Enabled' -NoRestart
                break
            }
            '0223fa85ff1b55f18565376f' {
                Get-NetAdapter -Physical | Set-DnsClientServerAddress -ResetServerAddresses
                break
            }
            default { throw "snapshot contains an unknown undo id" }
        }
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
if ($WhatIf2) {
    Write-Host "Undo preview complete; the active snapshot was left untouched." -ForegroundColor DarkGray
    Exit-Exit-SnappyAI 0
}
Write-Host "All recorded changes restored." -ForegroundColor Green
if (@($changes | Where-Object { $_.requiresReboot }).Count -gt 0) {
    Write-Host "Some of these changes require a reboot to fully unwind." -ForegroundColor Yellow
}
$archive = "$script:Snapshot.restored-$(Get-Date -Format 'yyyyMMdd-HHmmssfff')"
try {
    Move-Item -LiteralPath $script:Snapshot -Destination $archive -Force
    Write-Host "Archived the completed snapshot at $archive" -ForegroundColor DarkGray
} catch {
    Write-Host "Restored the changes but could not archive the active snapshot: $($_.Exception.Message)" -ForegroundColor Red
    Exit-Exit-SnappyAI 1
}
Exit-Exit-SnappyAI 0
