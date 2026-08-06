#Requires -Version 5.1
<#
    install.ps1 - guest-house guided bootstrapper for the WSL2 route (Windows).

    *** EXPERIMENTAL ***
    This script is syntax-reviewed only. It has NOT been drill-tested on real Windows
    hardware yet (docs/plans/2026-08-06-production-hardening-roadmap.md, Part 2, binding
    principle 4: "Honest platform labels. The Windows path ships EXPERIMENTAL until the
    drills run on real Windows hardware; the label is removed by evidence, not by time.")
    Treat every step as something to verify by hand until that drill has run and this
    banner is removed from the file.

    One-liner:
        irm https://raw.githubusercontent.com/ikangai/factory/main/install.ps1 | iex

    What it does: checks Windows/WSL2 are present, creates a DEDICATED WSL distro (never
    your daily-driver distro), hardens it (/etc/wsl.conf: no Windows drives, no Windows
    exec, no host PATH), then runs install.sh --guest-house --wsl inside it, which creates
    its own dedicated, non-admin "factory" Linux user and installs under that account with
    brakes on (mode stays "shift"). See docs/runbooks/guest-house.md for the full rules
    table (what this boundary gives, and what it does not give yet) and
    docs/runbooks/factory-user-deployment.md section 4 for the supervised-smoke-shift that
    must pass, watched, before anything runs unattended.

    macOS users: use install.sh --guest-house instead - it is the more mature, tested path.

    Style notes (kept deliberately simple - see the roadmap's "compensate with discipline"
    instruction, since this file cannot be executed on the build host that authored it):
    PowerShell 5.1-compatible syntax only (no pwsh-7-only operators), Set-StrictMode,
    every dynamic message built via string CONCATENATION rather than double-quoted
    interpolation (so a literal "$" in bash text printed to the user is never mistaken for
    a PowerShell variable), no aliases (ForEach-Object/Where-Object spelled out, etc.),
    LF line endings.
#>
[CmdletBinding()]
param(
    [switch]$Yes,
    [string]$DistroName = 'factory-guesthouse'
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'

$FactoryRepoRaw = 'https://raw.githubusercontent.com/ikangai/factory/main'

function Write-Section {
    param([string]$Title)
    Write-Host ''
    Write-Host ('=' * 70)
    Write-Host (' ' + $Title)
    Write-Host ('=' * 70)
}

# Mirrors install.sh's gh_confirm: prints the already-explained prompt, honors -Yes, and
# refuses to silently hang when there is no interactive console to read from (a downloaded
# script run via `irm | iex` still has a real console attached - unlike bash's `curl | bash`,
# where stdin IS the pipe - but a truly non-interactive host, e.g. a scheduled task, must
# still fail loudly rather than block forever on Read-Host).
function Confirm-Step {
    param([Parameter(Mandatory = $true)][string]$Message)
    if ($Yes) {
        Write-Host ('  (-Yes) ' + $Message + ' -> yes')
        return $true
    }
    if (-not [Environment]::UserInteractive) {
        Write-Host ('ERROR: no interactive console to ask: ' + $Message)
        Write-Host '  re-run with -Yes for a non-interactive install.'
        return $false
    }
    $reply = Read-Host ($Message + ' [y/N]')
    if ($reply -match '^(y|yes)$') {
        return $true
    }
    return $false
}

function Get-RegisteredDistros {
    $raw = & wsl.exe --list --quiet 2>$null
    if (-not $raw) {
        return @()
    }
    # wsl.exe emits UTF-16LE text that older PowerShell hosts can mis-decode with stray NUL
    # bytes between characters - strip them before comparing distro names.
    $clean = $raw | ForEach-Object { ($_ -replace "`0", '').Trim() } | Where-Object { $_ -ne '' }
    return $clean
}

function Test-Preflight {
    Write-Section 'Guest-house install (Windows/WSL2) preflight'
    Write-Host 'EXPERIMENTAL: this path has not yet been drill-tested on real Windows'
    Write-Host 'hardware - see the banner at the top of this file. Proceed with that in mind.'

    $wslCmd = Get-Command wsl.exe -ErrorAction SilentlyContinue
    if (-not $wslCmd) {
        Write-Host ''
        Write-Host 'ERROR: wsl.exe was not found. Install WSL2 first, then re-run this script.'
        Write-Host '  Run this in an elevated (Administrator) PowerShell, then REBOOT:'
        Write-Host '    wsl --install'
        return $false
    }

    $regPath = 'HKLM:\SOFTWARE\Microsoft\Windows NT\CurrentVersion'
    $currentBuildRaw = (Get-ItemProperty -Path $regPath -Name CurrentBuild).CurrentBuild
    $currentBuild = [int]$currentBuildRaw
    if ($currentBuild -lt 19041) {
        Write-Host ('ERROR: Windows build ' + $currentBuild + ' is older than the WSL2 minimum (build 19041, "Windows 10 2004").')
        Write-Host '  Update Windows, then re-run.'
        return $false
    }

    Write-Host '  preflight OK'
    return $true
}

function Install-GuestHouseDistro {
    $existing = Get-RegisteredDistros
    if ($existing -contains $DistroName) {
        Write-Host ('[1/5] distro "' + $DistroName + '" already registered - skipping creation')
        return $true
    }

    Write-Host ''
    Write-Host '[1/5] Create a DEDICATED WSL distro for the guest house'
    Write-Host '  This is a separate Linux install inside WSL, used ONLY by the factory -'
    Write-Host '  never your regular/daily-driver WSL distro. Refusing to reuse an existing'
    Write-Host '  distro keeps the guest house from ever touching anything you already use.'
    if (-not (Confirm-Step ('Install a fresh Ubuntu as "' + $DistroName + '" now?'))) {
        Write-Host 'ERROR: a dedicated distro is required for a guest-house install - aborting.'
        return $false
    }

    & wsl.exe --install -d Ubuntu --name $DistroName --no-launch
    if ($LASTEXITCODE -ne 0) {
        Write-Host ''
        Write-Host 'WARNING: "wsl --install -d Ubuntu --name <name>" was not accepted by this'
        Write-Host '  wsl.exe (older WSL releases do not support --name). Fall back to a manual'
        Write-Host '  import of a fresh Ubuntu rootfs tarball into a NEW, dedicated distro - do'
        Write-Host '  not import over an existing daily-driver distro:'
        Write-Host ('    wsl --import ' + $DistroName + ' <install-dir> <ubuntu-rootfs.tar.gz>')
        Write-Host '  See: https://learn.microsoft.com/windows/wsl/use-custom-distro'
        return $false
    }
    Write-Host ('  installed distro "' + $DistroName + '" (not yet launched)')
    return $true
}

function Set-WslHardening {
    Write-Host ''
    Write-Host '[2/5] Harden the distro: no Windows drives, no Windows exec, no host PATH'
    Write-Host '  automount off         - the distro cannot see your C:\ drive or any other Windows drive.'
    Write-Host '  interop off           - the distro cannot launch Windows .exe programs.'
    Write-Host '  appendWindowsPath off - the Windows PATH is never merged into the distro PATH.'
    Write-Host '  systemd on            - the distro can run background services (some factory tooling needs it).'
    if (-not (Confirm-Step 'Write /etc/wsl.conf with this hardening now?')) {
        Write-Host '  skipped - the guest house is NOT isolated from Windows until this is applied.'
        return $false
    }

    $wslConfContent = @'
[automount]
enabled=false

[interop]
enabled=false
appendWindowsPath=false

[boot]
systemd=true
'@
    # Piped-to-external-command stdin has known UTF-16/encoding surprises on PowerShell 5.1;
    # base64 sidesteps all of it (and every PowerShell/bash quoting trap) in one move.
    $bytes = [System.Text.Encoding]::UTF8.GetBytes($wslConfContent)
    $b64 = [System.Convert]::ToBase64String($bytes)
    $writeCmd = 'echo ' + $b64 + ' | base64 -d > /etc/wsl.conf'
    & wsl.exe -d $DistroName -u root -- bash -c $writeCmd
    if ($LASTEXITCODE -ne 0) {
        Write-Host 'ERROR: failed to write /etc/wsl.conf inside the distro.'
        return $false
    }

    Write-Host '  terminating the distro so the hardening takes effect on next launch ...'
    & wsl.exe --terminate $DistroName
    Write-Host '  /etc/wsl.conf written and the distro restarted'
    return $true
}

function Invoke-GuestHouseBashInstaller {
    Write-Host ''
    Write-Host '[3/5] Run the guest-house installer inside the distro'
    Write-Host '  Downloads install.sh from the factory repo and runs it AS ROOT inside the'
    Write-Host '  distro only (root always exists in a fresh distro) - install.sh --guest-house'
    Write-Host '  --wsl then creates its OWN dedicated, non-admin "factory" Linux user and'
    Write-Host '  installs under that account, brakes on (mode stays "shift").'
    Write-Host '  NOTE (EXPERIMENTAL caveat): a brand-new Ubuntu WSL distro may show a'
    Write-Host '  one-time interactive setup prompt (UNIX username/password) on its very'
    Write-Host '  first launch. If this step appears to hang, open a normal WSL window for'
    Write-Host ('  the distro once (wsl -d ' + $DistroName + '), complete that prompt, then re-run this step.')
    if (-not (Confirm-Step 'Run install.sh --guest-house --wsl inside the distro now?')) {
        Write-Host '  skipped - run it later by hand (see the closing summary for the exact command).'
        return $false
    }

    $installArgs = '--guest-house --wsl'
    if ($Yes) {
        $installArgs = $installArgs + ' --yes'
    }
    $bashCmd = 'curl -fsSL ' + $FactoryRepoRaw + '/install.sh -o /tmp/install.sh && bash /tmp/install.sh ' + $installArgs
    & wsl.exe -d $DistroName -u root -- bash -c $bashCmd
    if ($LASTEXITCODE -ne 0) {
        Write-Host 'ERROR: the bash installer reported a failure inside the distro (see output above).'
        return $false
    }
    return $true
}

function Write-ClosingSummary {
    $singleQuote = "'"
    $doctorCmd = 'wsl -d ' + $DistroName + ' -u factory -- bash -lc ' + $singleQuote +
        'cd $HOME/fab/factory && python3 scripts/guesthouse_check.py' + $singleQuote

    Write-Section 'Guest-house install (Windows/WSL2) - summary'
    Write-Host ('distro:   ' + $DistroName + ' (isolated: no Windows drives, no Windows exec, no host PATH)')
    Write-Host '[4/5] brakes: mode stays "shift" inside the distro until you flip it.'
    Write-Host '[5/5] next steps:'
    Write-Host '  1. docs/runbooks/factory-user-deployment.md section 4 - run ONE supervised'
    Write-Host '     smoke shift, watched, before anything runs unattended.'
    Write-Host ('  2. re-run the doctor any time: ' + $doctorCmd)
    Write-Host '  3. teardown: docs/runbooks/factory-user-deployment.md section 8, plus'
    Write-Host ('     "wsl --unregister ' + $DistroName + '" to remove the distro entirely.')
    Write-Host 'rules table: docs/runbooks/guest-house.md'
    Write-Host ''
    Write-Host '*** EXPERIMENTAL ***  this Windows/WSL2 path is syntax-reviewed only - it has'
    Write-Host 'not yet been drill-tested on real Windows hardware. Treat every step above as'
    Write-Host 'something to verify by hand until that drill has run (roadmap Part 2, principle 4).'
}

function Main {
    Write-Section 'Guest-house install (Windows/WSL2) - EXPERIMENTAL'
    Write-Host 'This sets up an isolated WSL2 distro dedicated to the factory, hardened so it'
    Write-Host 'cannot see your Windows files or run Windows programs, then hands off to'
    Write-Host 'install.sh --guest-house --wsl inside it. Full rules: docs/runbooks/guest-house.md'

    if (-not (Test-Preflight)) {
        return 1
    }
    if (-not (Install-GuestHouseDistro)) {
        return 1
    }
    if (-not (Set-WslHardening)) {
        return 1
    }
    Invoke-GuestHouseBashInstaller | Out-Null

    Write-ClosingSummary
    return 0
}

$script:GuestHouseExitCode = Main
# `exit` would close the caller's whole PowerShell console when this file is dot-sourced via
# `irm ... | iex` (no backing file, no real script process) - only call it when running as a
# real .ps1 file, where MyCommand.Path is set.
if ($MyInvocation.MyCommand.Path) {
    exit $script:GuestHouseExitCode
}
