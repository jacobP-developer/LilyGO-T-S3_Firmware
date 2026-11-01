<#
.SYNOPSIS
    Push m4nt1s-nav.py and c2_android.py to the phone's /sdcard/python/.

.DESCRIPTION
    Place this script next to m4nt1s-nav.py. Running it pushes the local
    copies to the connected phone, overwriting whatever's there:

      <scriptDir>\m4nt1s-nav.py         -> /sdcard/python/m4nt1s-nav.py
      <scriptDir>\..\C2\c2_android.py   -> /sdcard/python/c2_android.py

    Where <scriptDir> is the folder containing deploy.ps1, i.e.:
      C:\M4NT1S\deploy\m4nt1s-nav.py
      C:\M4NT1S\C2\c2_android.py

    Each file is verified with md5sum after pushing. A summary is printed
    at the end; the script exits non-zero if any push or verification fails.

.PARAMETER Source
    Optional. Overrides auto-detection for m4nt1s-nav.py.

.PARAMETER PhoneDest
    Optional. Defaults to /sdcard/python/m4nt1s-nav.py.

.PARAMETER C2Source
    Optional. Overrides auto-detection for c2_android.py.

.PARAMETER C2Dest
    Optional. Defaults to /sdcard/python/c2_android.py.

.EXAMPLE
    .\deploy.ps1
#>
[CmdletBinding()]
param(
    [string]$Source,
    [string]$PhoneDest = '/sdcard/python/m4nt1s-nav.py',
    [string]$C2Source,
    [string]$C2Dest = '/sdcard/python/c2_android.py'
)

$ErrorActionPreference = 'Stop'

# PS 7.3+ - stop NativeCommandError for non-zero exit codes from
# external tools. Harmless on PS 5.1 (variable just sits unused).
$PSNativeCommandUseErrorActionPreference = $false

function Fail($msg) {
    Write-Host "[!] $msg" -ForegroundColor Red
    exit 1
}

# Runs adb with stderr folded into stdout, WITHOUT letting
# $ErrorActionPreference='Stop' terminate on adb's progress output.
# adb writes to stderr on success, which PowerShell 5.1 treats as an
# ErrorRecord and would otherwise abort the script.
function Invoke-Adb {
    param([Parameter(ValueFromRemainingArguments = $true)][string[]]$Args)
    $prev = $ErrorActionPreference
    $ErrorActionPreference = 'Continue'
    try {
        $raw = & adb @Args 2>&1
        $code = $LASTEXITCODE
    } finally {
        $ErrorActionPreference = $prev
    }
    $lines = @($raw | ForEach-Object {
        if ($_ -is [System.Management.Automation.ErrorRecord]) {
            $_.Exception.Message
        } else {
            "$_"
        }
    })
    return [pscustomobject]@{ Output = $lines; ExitCode = $code }
}

# 1. adb on PATH
if (-not (Get-Command adb -ErrorAction SilentlyContinue)) {
    Fail "adb not found on PATH. Install platform-tools or add its folder to PATH."
}

# 2. exactly one authorized device
$devResult   = Invoke-Adb devices
$deviceLines = @($devResult.Output | Select-Object -Skip 1 | Where-Object { $_ -match '\S' })
if ($deviceLines.Count -eq 0) {
    Fail "No ADB devices connected."
}
if ($deviceLines -match 'unauthorized') {
    Fail "Device unauthorized. Unlock the phone and accept the USB debugging prompt."
}
if ($deviceLines -match 'offline') {
    Fail "Device offline. Reconnect the USB cable and re-run."
}
if ($deviceLines.Count -gt 1) {
    Fail "Multiple ADB devices connected ($($deviceLines.Count)). Disconnect all but one, or specify a single device."
}
$deviceId = ($deviceLines[0] -split '\s+')[0]

# 3. resolve sources
$scriptDir = if ($PSScriptRoot) { $PSScriptRoot } else { Split-Path -Parent $MyInvocation.MyCommand.Path }

# nav (m4nt1s-nav.py) - lives next to deploy.ps1
if (-not $Source) {
    foreach ($name in @('m4nt1s-nav.py', 'm4nt1s_nav.py')) {
        $candidate = Join-Path $scriptDir $name
        if (Test-Path $candidate) {
            $Source = $candidate
            break
        }
    }
}
if (-not $Source) {
    Fail "No m4nt1s-nav.py found in $scriptDir."
}
if (-not (Test-Path $Source)) {
    Fail "Nav source file not found: $Source"
}
$Source = (Resolve-Path $Source).Path

# C2 (c2_android.py) - one level up from $scriptDir, then into \C2\
# i.e. C:\M4NT1S\C2\c2_android.py
if (-not $C2Source) {
    foreach ($candidate in @(
        (Join-Path $scriptDir '..\C2\c2_android.py'),
        (Join-Path $scriptDir '..\C2\c2.py')          # fallback
    )) {
        if (Test-Path $candidate) {
            $C2Source = $candidate
            break
        }
    }
}
if (-not $C2Source) {
    Fail "No c2_android.py or c2.py found at $scriptDir\..\C2\"
}
if (-not (Test-Path $C2Source)) {
    Fail "C2 source file not found: $C2Source"
}
$C2Source = (Resolve-Path $C2Source).Path

# 4. build deploy job list (both files resolved before touching the device)
$jobs = @(
    [pscustomobject]@{
        Label  = 'm4nt1s-nav'
        Source = $Source
        Dest   = $PhoneDest
        Hash   = (Get-FileHash -Path $Source -Algorithm MD5).Hash
    },
    [pscustomobject]@{
        Label  = 'c2_android'
        Source = $C2Source
        Dest   = $C2Dest
        Hash   = (Get-FileHash -Path $C2Source -Algorithm MD5).Hash
    }
)

Write-Host "[*] Device: $deviceId"
foreach ($j in $jobs) {
    Write-Host ("[*] {0,-11} {1}" -f ($j.Label + ':'), $j.Source)
    Write-Host ("    -> {0}" -f $j.Dest)
    Write-Host ("    MD5: {0}" -f $j.Hash)
}

$failures = New-Object System.Collections.ArrayList

# 5. push + verify each
foreach ($j in $jobs) {
    Write-Host ""
    Write-Host "[*] Pushing $($j.Label)..." -ForegroundColor Cyan
    $pushResult = Invoke-Adb push $j.Source $j.Dest
    $pushResult.Output | ForEach-Object { Write-Host "    $_" }
    if ($pushResult.ExitCode -ne 0) {
        Write-Host "[!] $($j.Label): adb push failed (exit $($pushResult.ExitCode))." -ForegroundColor Red
        [void]$failures.Add($j.Label)
        continue
    }

    Write-Host "[*] Verifying $($j.Label)..." -ForegroundColor Cyan
    $md5Result  = Invoke-Adb shell "md5sum $($j.Dest)"
    $remoteOut  = $md5Result.Output -join "`n"
    $remoteHash = $null
    if ($remoteOut -match '([0-9a-fA-F]{32})') {
        $remoteHash = $Matches[1].ToUpper()
    }

    if (-not $remoteHash) {
        Write-Host "[?] $($j.Label): could not verify (md5sum not available on device)" -ForegroundColor DarkYellow
        $md5Result.Output | ForEach-Object { Write-Host "    $_" -ForegroundColor DarkGray }
    } elseif ($remoteHash -eq $j.Hash) {
        Write-Host "[+] $($j.Label): verified" -ForegroundColor Green
    } else {
        Write-Host "[!] $($j.Label): hash mismatch" -ForegroundColor Yellow
        Write-Host "    local : $($j.Hash)"
        Write-Host "    remote: $remoteHash"
        [void]$failures.Add($j.Label)
    }
}

# 6. summary
Write-Host ""
if ($failures.Count -gt 0) {
    Write-Host ("[!] Deploy finished with errors: {0}" -f ($failures -join ', ')) -ForegroundColor Red
    exit 1
}

Write-Host "[+] Deploy complete." -ForegroundColor Green
Write-Host ""
Write-Host "    On the phone:" -ForegroundColor DarkGray
Write-Host "      nav                                    (start the UI)" -ForegroundColor DarkGray
Write-Host "      python3 /sdcard/python/c2_android.py   (start the C2)" -ForegroundColor DarkGray
Write-Host ""
Write-Host "    If the C2 has never run on this device, install flask first:" -ForegroundColor DarkGray
Write-Host "      pkg install python" -ForegroundColor DarkGray
Write-Host "      pip install flask" -ForegroundColor DarkGray
Write-Host ""
Write-Host "    A running nav or C2 session keeps old code until restarted." -ForegroundColor DarkGray