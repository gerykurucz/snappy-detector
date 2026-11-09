
$sig = @'
using System;
using System.Runtime.InteropServices;
public static class MemClean {
  [DllImport("ntdll.dll")]
  public static extern int NtSetSystemInformation(int infoClass, ref int info, int length);
}
'@
Add-Type -TypeDefinition $sig -ErrorAction Stop

function Read-MemStats {
  $c = Get-Counter '\Memory\Available MBytes' -ErrorAction SilentlyContinue
  $m = @{}
  foreach ($s in $c.CounterSamples) {
    $m['Available MBytes'] = [math]::Round($s.CookedValue, 1)
  }
  if (-not $m.ContainsKey('Available MBytes')) {
    try {
      $m['Available MBytes'] = [math]::Round(
        (Get-CimInstance Win32_OperatingSystem).FreePhysicalMemory / 1KB, 1)
    } catch {}
  }
  return $m
}

$before = Read-MemStats
$areas = New-Object System.Collections.ArrayList

function Invoke-Area {
  param([string]$Label, [int]$Command)
  $v = $Command
  $st = [MemClean]::NtSetSystemInformation(80, [ref]$v, 4)
  [void]$areas.Add([pscustomobject]@{
    area   = $Label
    status = $(if ($st -eq 0) { 'ok' } else { 'NTSTATUS 0x{0:X}' -f $st })
  })
}

Invoke-Area 'standby list' 4
Invoke-Area 'low-priority standby list' 5
Invoke-Area 'modified page list' 3
Invoke-Area 'working sets (all processes)' 2

$v = 0
$st = [MemClean]::NtSetSystemInformation(155, [ref]$v, 0)
[void]$areas.Add([pscustomobject]@{
  area   = 'registry cache'
  status = $(if ($st -eq 0) { 'ok' } else { 'NTSTATUS 0x{0:X}' -f $st })
})

Start-Sleep -Milliseconds 600
$after = Read-MemStats
[pscustomobject]@{
  before = $before
  after  = $after
  areas  = $areas
  caveat = 'Working sets were emptied machine-wide. Expect a short burst of page faults while applications fault their pages back in.'
} | ConvertTo-Json -Depth 4 -Compress
