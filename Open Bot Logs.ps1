param(
    [ValidateRange(1, 65535)]
    [int]$LocalPort = 3000,
    [switch]$NoBrowser
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'

$sshTarget = 'root@34.76.190.186'
$forward = "127.0.0.1:${LocalPort}:127.0.0.1:3000"
$grafanaBase = "http://127.0.0.1:$LocalPort"
$dashboardPaths = @(
    '/d/gemaibot-logs/gemai-bot-logs',
    '/d/ytdlbot-logs/yt-dlp-bot-logs'
)
$startedSsh = $null

function Get-TunnelListener {
    @(Get-NetTCPConnection -LocalAddress '127.0.0.1' -LocalPort $LocalPort -State Listen -ErrorAction SilentlyContinue)
}

function Test-ExpectedTunnel {
    param([int]$ProcessId)

    $owner = Get-CimInstance Win32_Process -Filter "ProcessId=$ProcessId" -ErrorAction SilentlyContinue
    if ($null -eq $owner -or $owner.Name -ine 'ssh.exe') {
        return $false
    }

    return $owner.CommandLine.Contains($sshTarget) -and $owner.CommandLine.Contains($forward)
}

function Test-GrafanaReady {
    try {
        $response = Invoke-RestMethod -Uri "$grafanaBase/api/health" -TimeoutSec 3
        return $response.database -eq 'ok'
    }
    catch {
        return $false
    }
}

function Show-LauncherError {
    param([string]$Message)

    if ($NoBrowser) {
        [Console]::Error.WriteLine($Message)
        return
    }

    Add-Type -AssemblyName System.Windows.Forms
    [void][System.Windows.Forms.MessageBox]::Show(
        $Message,
        'Bot Logs',
        [System.Windows.Forms.MessageBoxButtons]::OK,
        [System.Windows.Forms.MessageBoxIcon]::Error
    )
}

try {
    $listeners = @(Get-TunnelListener)
    if ($listeners.Count -gt 0) {
        if (-not (Test-ExpectedTunnel -ProcessId $listeners[0].OwningProcess)) {
            throw "Local port $LocalPort is already used by a different process. Close it and try again."
        }
        if (-not (Test-GrafanaReady)) {
            throw 'The existing SSH tunnel is not reaching Grafana. Check the SSH connection and try again.'
        }
    }
    else {
        $ssh = (Get-Command ssh.exe -ErrorAction Stop).Source
        $sshArguments = @(
            '-N', '-T',
            '-o', 'BatchMode=yes',
            '-o', 'StrictHostKeyChecking=yes',
            '-o', 'ExitOnForwardFailure=yes',
            '-o', 'ConnectTimeout=10',
            '-o', 'ServerAliveInterval=30',
            '-o', 'ServerAliveCountMax=3',
            '-L', $forward,
            $sshTarget
        )
        $startedSsh = Start-Process -FilePath $ssh -ArgumentList $sshArguments -WindowStyle Hidden -PassThru

        $deadline = [DateTime]::UtcNow.AddSeconds(20)
        $ready = $false
        do {
            Start-Sleep -Milliseconds 250
            $startedSsh.Refresh()
            if ($startedSsh.HasExited) {
                throw "SSH exited before the tunnel was ready (exit code $($startedSsh.ExitCode)). Check your SSH key and host access."
            }

            $listeners = @(Get-TunnelListener)
            if ($listeners.Count -gt 0 -and
                (Test-ExpectedTunnel -ProcessId $listeners[0].OwningProcess) -and
                (Test-GrafanaReady)) {
                $ready = $true
                break
            }
        } while ([DateTime]::UtcNow -lt $deadline)

        if (-not $ready) {
            throw 'The SSH tunnel started, but Grafana did not respond within 20 seconds.'
        }
    }

    if (-not $NoBrowser) {
        foreach ($path in $dashboardPaths) {
            Start-Process "$grafanaBase$path"
        }
    }
}
catch {
    if ($null -ne $startedSsh) {
        $startedSsh.Refresh()
        if (-not $startedSsh.HasExited) {
            Stop-Process -Id $startedSsh.Id -Force -ErrorAction SilentlyContinue
        }
    }
    Show-LauncherError $_.Exception.Message
    exit 1
}
