#Requires -Version 5.1
<#
    install.ps1 - guest-house guided bootstrapper for the WSL2 route (Windows).

    *** EXPERIMENTAL ***
    This script is syntax-reviewed only. It has NOT been drill-tested on real Windows
    hardware yet (docs/plans/2026-08-06-production-hardening-roadmap.md, Part 2, binding
    principle 4: "Honest platform labels. The Windows path ships EXPERIMENTAL until the
    drills run on real Windows hardware; the label is removed by evidence, not by time.")
    Treat every step as something to verify by hand until that drill has run and this
    banner is removed from the file. The closing summary lists ten specific things that
    cannot be verified without real Windows hardware - read them before trusting this.

    One-liner (no parameters - -Yes/-DistroName need the parameterized form instead, see
    the closing summary for the exact syntax; a bare `irm | iex` cannot forward parameters):
        irm https://raw.githubusercontent.com/ikangai/factory/main/install.ps1 | iex

    What it does: checks Windows/WSL2 are REALLY enabled (not just that wsl.exe exists on
    disk), creates a DEDICATED WSL distro (never your daily-driver distro - refuses to reuse
    an existing one that lacks this wizard's own ownership marker), installs base
    dependencies inside it, hardens it (/etc/wsl.conf: no Windows drives, no Windows exec, no
    host PATH - then READS BACK the result to confirm it actually took effect), then runs
    install.sh --guest-house --wsl inside it, which creates its own dedicated, non-admin
    "factory" Linux user and installs under that account with brakes on (mode stays "shift").
    See docs/runbooks/guest-house.md for the full rules table (what this boundary gives, and
    what it does not give yet) and docs/runbooks/factory-user-deployment.md section 4 for the
    supervised-smoke-shift that must pass, watched, before anything runs unattended.

    macOS users: use install.sh --guest-house instead - it is the more mature, tested path.

    Style notes (kept deliberately simple - see the roadmap's "compensate with discipline"
    instruction, since this file cannot be executed on the build host that authored it):
    PowerShell 5.1-compatible syntax only (no pwsh-7-only operators), Set-StrictMode, every
    dynamic message built via string CONCATENATION rather than double-quoted interpolation
    (so a literal "$" in bash text printed to the user is never mistaken for a PowerShell
    variable), no aliases (ForEach-Object/Where-Object spelled out, etc.), LF line endings
    (also pinned via .gitattributes so a Windows checkout can't silently convert them to
    CRLF and corrupt the here-strings below).

    RETURN-STREAM DISCIPLINE (the single most important correctness rule in this file): a
    PowerShell function's return value is EVERYTHING that lands on its success/output
    stream, not just what follows an explicit `return`. A bare native-command invocation
    like `& wsl.exe --install ...` writes its own stdout onto that SAME stream, which then
    gets appended to whatever the function later `return`s - so `if (-not (Some-Function))`
    can see a multi-element array (always truthy) even when the function meant to return
    $false. Every external `wsl.exe` call below therefore goes through one of two small
    helpers: Invoke-WslVisible (streams output live via Write-Host - which uses the
    Information stream, not Success, so it can never pollute a return value - and returns
    only a plain int exit code) or Invoke-WslSilent (captures output into a variable and
    returns a hashtable). No function in this file ever leaves a bare `& wsl.exe ...` call
    unassigned/unpiped.
#>
[CmdletBinding()]
param(
    [switch]$Yes,
    [string]$DistroName = 'factory-guesthouse'
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'

try {
    # wsl.exe emits UTF-16LE; PowerShell 5.1's default console encoding can mis-decode that,
    # producing stray embedded NULs in captured output (Get-RegisteredDistros strips them as
    # a belt-and-braces fallback either way).
    [Console]::OutputEncoding = [System.Text.Encoding]::Unicode
} catch {
    # best-effort only - a non-console host (ISE, some CI/task runners) may refuse this.
}

$FactoryRepoRaw = 'https://raw.githubusercontent.com/ikangai/factory/main'
$RunbookBaseUrl = 'https://github.com/ikangai/factory/blob/main'

function Write-Section {
    param([string]$Title)
    Write-Host ''
    Write-Host ('=' * 70)
    Write-Host (' ' + $Title)
    Write-Host ('=' * 70)
}

# Runs wsl.exe, streaming its output live via Write-Host (Information stream - can never
# pollute a caller's return value), and returns ONLY a plain int exit code.
function Invoke-WslVisible {
    param([Parameter(Mandatory = $true)][string[]]$WslArgs)
    & wsl.exe @WslArgs 2>&1 | ForEach-Object { Write-Host ('  ' + $_) }
    return $LASTEXITCODE
}

# Runs wsl.exe, capturing (not printing) its output, and returns a hashtable
# @{ ExitCode = <int>; Output = <string> } so a caller can inspect output only on failure.
function Invoke-WslSilent {
    param([Parameter(Mandatory = $true)][string[]]$WslArgs)
    $out = & wsl.exe @WslArgs 2>&1
    $joined = ''
    if ($out) {
        $joined = ($out -join "`n")
    }
    return @{ ExitCode = $LASTEXITCODE; Output = $joined }
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
    try {
        $raw = & wsl.exe --list --quiet 2>$null
    } catch {
        return @()
    }
    if (-not $raw) {
        return @()
    }
    # wsl.exe emits UTF-16LE text that older PowerShell hosts can mis-decode with stray NUL
    # bytes between characters - strip them before comparing distro names.
    $clean = $raw | ForEach-Object { ($_ -replace "`0", '').Trim() } | Where-Object { $_ -ne '' }
    return $clean
}

# The distro this wizard itself created writes this marker (C3) - the ONLY signal that lets
# the reuse branch below tell "our distro from a prior run" apart from "your daily driver
# that just happens to share this name".
function Test-DistroMarker {
    $result = Invoke-WslSilent -WslArgs @('-d', $DistroName, '-u', 'root', '--', 'bash', '-c',
        'test -f /etc/factory-guesthouse.marker && echo yes || echo no')
    return ($result.Output -match 'yes')
}

function Test-Preflight {
    Write-Section 'Guest-house install (Windows/WSL2) preflight'
    Write-Host 'EXPERIMENTAL: this path has not yet been drill-tested on real Windows'
    Write-Host 'hardware - see the banner at the top of this file. Proceed with that in mind.'

    $wslCmd = Get-Command wsl.exe -ErrorAction SilentlyContinue
    if (-not $wslCmd) {
        Write-Host ''
        Write-Host 'ERROR: wsl.exe was not found on this system at all.'
        Write-Host '  Run this in an elevated (Administrator) PowerShell, then REBOOT:'
        Write-Host '    wsl --install'
        return $false
    }

    # wsl.exe existing on disk does NOT mean WSL is enabled - a real "wsl --status" probe is
    # required (this used to be inert: Get-Command alone passes on every modern Windows even
    # with the WSL optional feature fully disabled).
    $statusResult = Invoke-WslSilent -WslArgs @('--status')
    if ($statusResult.ExitCode -ne 0) {
        Write-Host ''
        Write-Host ('ERROR: "wsl --status" failed (exit ' + $statusResult.ExitCode + ') - WSL is not enabled/installed.')
        Write-Host '  Run this in an elevated (Administrator) PowerShell, then REBOOT:'
        Write-Host '    wsl --install'
        return $false
    }

    # "wsl --version" only succeeds on the modern, distro-management-capable WSL. An older
    # "inbox" WSL (no --version support) also lacks --name and other flags this script
    # depends on - attempting --install --name against it would misattribute every failure
    # to --name specifically, so refuse cleanly here instead.
    $versionResult = Invoke-WslSilent -WslArgs @('--version')
    if ($versionResult.ExitCode -ne 0) {
        Write-Host ''
        Write-Host 'ERROR: "wsl --version" failed - this looks like an older "inbox" WSL release'
        Write-Host '  without modern distro-management support (no --name flag, etc). This script'
        Write-Host '  cannot drive that safely. Fall back to a manual import of a fresh Ubuntu'
        Write-Host '  rootfs into a NEW, dedicated distro:'
        Write-Host ('    wsl --import ' + $DistroName + ' <install-dir> <ubuntu-rootfs.tar.gz>')
        Write-Host '  See: https://learn.microsoft.com/windows/wsl/use-custom-distro'
        return $false
    }

    $regPath = 'HKLM:\SOFTWARE\Microsoft\Windows NT\CurrentVersion'
    $currentBuild = $null
    try {
        $currentBuildRaw = (Get-ItemProperty -Path $regPath -Name CurrentBuild -ErrorAction Stop).CurrentBuild
        $currentBuild = [int]$currentBuildRaw
    } catch {
        Write-Host 'WARNING: could not read the Windows build number from the registry - skipping that specific check.'
    }
    if ($currentBuild -and ($currentBuild -lt 19041)) {
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
        if (Test-DistroMarker) {
            Write-Host ('[1/6] distro "' + $DistroName + '" already registered and marked as ours - skipping creation')
            return $true
        }
        Write-Host ''
        Write-Host ('ERROR: a distro named "' + $DistroName + '" already exists but was NOT created by')
        Write-Host '  this wizard (no /etc/factory-guesthouse.marker inside it). Refusing to reuse it -'
        Write-Host '  this could be your daily-driver distro, and hardening it would silently cut off'
        Write-Host '  its Windows-drive access and .exe interop without warning.'
        Write-Host '  Pick another -DistroName, or if you are SURE this distro is disposable:'
        Write-Host ('    wsl --unregister ' + $DistroName)
        Write-Host '  then re-run this script.'
        return $false
    }

    Write-Host ''
    Write-Host '[1/6] Create a DEDICATED WSL distro for the guest house'
    Write-Host '  This is a separate Linux install inside WSL, used ONLY by the factory -'
    Write-Host '  never your regular/daily-driver WSL distro. Refusing to reuse an existing,'
    Write-Host '  unmarked distro (above) keeps the guest house from ever touching anything'
    Write-Host '  you already use.'
    if (-not (Confirm-Step ('Install a fresh Ubuntu as "' + $DistroName + '" now?'))) {
        Write-Host 'ERROR: a dedicated distro is required for a guest-house install - aborting.'
        return $false
    }

    $installExit = Invoke-WslVisible -WslArgs @('--install', '-d', 'Ubuntu', '--name', $DistroName, '--no-launch')
    if ($installExit -ne 0) {
        Write-Host ''
        Write-Host 'WARNING: "wsl --install -d Ubuntu --name <name>" failed on this wsl.exe (exit'
        Write-Host ('  ' + $installExit + '). Fall back to a manual import of a fresh Ubuntu rootfs tarball')
        Write-Host '  into a NEW, dedicated distro - do not import over an existing daily-driver distro:'
        Write-Host ('    wsl --import ' + $DistroName + ' <install-dir> <ubuntu-rootfs.tar.gz>')
        Write-Host '  See: https://learn.microsoft.com/windows/wsl/use-custom-distro'
        return $false
    }

    # Confirm the NAMED distro is actually what got registered - an older wsl.exe can ignore
    # --name while still exiting 0, which must never lead to hardening some other, unintended
    # (possibly pre-existing) distro.
    $registered = Get-RegisteredDistros
    if ($registered -notcontains $DistroName) {
        Write-Host ''
        Write-Host ('ERROR: "wsl --install" reported success but "' + $DistroName + '" is not registered.')
        Write-Host '  This WSL version may be ignoring --name. Do not proceed - hardening the wrong'
        Write-Host '  distro would cut off Windows-drive access on something you did not intend.'
        Write-Host '  Registered distros:'
        $registered | ForEach-Object { Write-Host ('    ' + $_) }
        return $false
    }

    $markerResult = Invoke-WslSilent -WslArgs @('-d', $DistroName, '-u', 'root', '--', 'bash', '-c',
        'touch /etc/factory-guesthouse.marker')
    if ($markerResult.ExitCode -ne 0) {
        Write-Host 'WARNING: could not write the ownership marker /etc/factory-guesthouse.marker -'
        Write-Host '  a future run of this script will not recognize this distro as its own.'
    }

    Write-Host ('  installed distro "' + $DistroName + '" (not yet launched)')
    return $true
}

function Install-DistroDependencies {
    Write-Host ''
    Write-Host '[2/6] Install base dependencies inside the distro'
    Write-Host '  A fresh Ubuntu image has no curl/git/python3-pip - installs them as root now,'
    Write-Host '  before handing off to install.sh (which needs all of them).'
    if (-not (Confirm-Step 'Install curl, git, python3-pip, python3-venv now (apt-get)?')) {
        Write-Host '  skipped - install.sh will likely fail without these. Install by hand:'
        Write-Host ('    wsl -d ' + $DistroName + ' -u root -- apt-get update')
        Write-Host ('    wsl -d ' + $DistroName + ' -u root -- apt-get install -y curl git python3-pip python3-venv')
        return $false
    }

    $aptScript = 'export DEBIAN_FRONTEND=noninteractive && apt-get update && ' +
        'apt-get install -y curl git python3-pip python3-venv'
    $bytes = [System.Text.Encoding]::UTF8.GetBytes($aptScript)
    $b64 = [System.Convert]::ToBase64String($bytes)
    $wrapped = 'echo ' + $b64 + ' | base64 -d | bash'
    $exitCode = Invoke-WslVisible -WslArgs @('-d', $DistroName, '-u', 'root', '--', 'bash', '-c', $wrapped)
    if ($exitCode -ne 0) {
        Write-Host 'ERROR: dependency installation failed inside the distro (see output above).'
        return $false
    }
    return $true
}

function Set-WslHardening {
    Write-Host ''
    Write-Host '[3/6] Harden the distro: no Windows drives, no Windows exec, no host PATH'
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
    $writeResult = Invoke-WslSilent -WslArgs @('-d', $DistroName, '-u', 'root', '--', 'bash', '-c', $writeCmd)
    if ($writeResult.ExitCode -ne 0) {
        Write-Host 'ERROR: failed to write /etc/wsl.conf inside the distro.'
        Write-Host $writeResult.Output
        return $false
    }

    $termResult = Invoke-WslSilent -WslArgs @('--terminate', $DistroName)
    if ($termResult.ExitCode -ne 0) {
        Write-Host ('WARNING: "wsl --terminate ' + $DistroName + '" failed (exit ' + $termResult.ExitCode + ') - the hardening may not take effect until the distro is stopped by hand.')
    } else {
        Write-Host '  distro terminated - the hardening takes effect on its next (cold) launch.'
    }

    # Read back /etc/wsl.conf and confirm automount is REALLY off, not just that the write
    # succeeded - a syntax mistake or a wsl.conf the distro image itself overwrites on boot
    # would otherwise go unnoticed.
    $readBack = Invoke-WslSilent -WslArgs @('-d', $DistroName, '-u', 'root', '--', 'bash', '-c', 'cat /etc/wsl.conf')
    if ($readBack.ExitCode -ne 0 -or ($readBack.Output -notmatch 'enabled=false')) {
        Write-Host 'ERROR: could not read back /etc/wsl.conf inside the distro, or it does not contain'
        Write-Host '  the expected hardening. Output:'
        Write-Host $readBack.Output
        return $false
    }
    $mountCheck = Invoke-WslSilent -WslArgs @('-d', $DistroName, '-u', 'root', '--', 'bash', '-c',
        'ls /mnt/c >/dev/null 2>&1 && echo MOUNTED || echo BLOCKED')
    if ($mountCheck.Output -notmatch 'BLOCKED') {
        Write-Host 'ERROR: /mnt/c is still reachable inside the distro after hardening - automount is'
        Write-Host '  NOT actually off. Verify /etc/wsl.conf by hand and re-terminate the distro, then'
        Write-Host '  re-run this script.'
        return $false
    }

    Write-Host '  verified: /etc/wsl.conf matches, and /mnt/c (the Windows C: drive) is unreachable inside the distro.'
    return $true
}

# Returns one of 'declined' | 'failed' | 'succeeded' - a plain boolean would collapse
# "the operator said no" and "it ran and errored" into the same "falsy" outcome, and Main
# needs to tell those apart to pick the right exit code (C5).
function Invoke-GuestHouseBashInstaller {
    Write-Host ''
    Write-Host '[4/6] Run the guest-house installer inside the distro'
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
        return 'declined'
    }

    $installArgs = '--guest-house --wsl'
    if ($Yes) {
        $installArgs = $installArgs + ' --yes'
    }
    $bashCmd = 'curl -fsSL ' + $FactoryRepoRaw + '/install.sh -o /tmp/install.sh && bash /tmp/install.sh ' + $installArgs
    $exitCode = Invoke-WslVisible -WslArgs @('-d', $DistroName, '-u', 'root', '--', 'bash', '-c', $bashCmd)
    if ($exitCode -ne 0) {
        Write-Host 'ERROR: the bash installer reported a failure inside the distro (see output above).'
        return 'failed'
    }
    return 'succeeded'
}

function Write-ClosingSummary {
    param([string]$InstallerStatus)
    $singleQuote = "'"
    $doctorCmd = 'wsl -d ' + $DistroName + ' -u factory -- bash -lc ' + $singleQuote +
        'cd $HOME/factories/guest-house/factory && python3 scripts/guesthouse_check.py' + $singleQuote

    Write-Section 'Guest-house install (Windows/WSL2) - summary'
    Write-Host ('distro:     ' + $DistroName + ' (isolated: no Windows drives, no Windows exec, no host PATH)')
    Write-Host ('installer:  ' + $InstallerStatus)
    Write-Host '[5/6] brakes: mode stays "shift" inside the distro until you flip it.'
    Write-Host '[6/6] next steps:'
    Write-Host ('  1. ' + $RunbookBaseUrl + '/docs/runbooks/factory-user-deployment.md section 4 -')
    Write-Host '     run ONE supervised smoke shift, watched, before anything runs unattended.'
    Write-Host ('  2. re-run the doctor any time: ' + $doctorCmd)
    Write-Host '  3. teardown: factory-user-deployment.md section 8, plus:'
    Write-Host ('     wsl --unregister ' + $DistroName + '   (removes the distro entirely)')
    Write-Host ('rules table: ' + $RunbookBaseUrl + '/docs/runbooks/guest-house.md')
    Write-Host ''
    Write-Host 'A bare "irm ... | iex" one-liner cannot forward -Yes/-DistroName. To pass them,'
    Write-Host 'use the parameterized invocation form instead:'
    Write-Host ('  & ([scriptblock]::Create((irm ' + $FactoryRepoRaw + '/install.ps1))) -Yes -DistroName my-distro')
    Write-Host ''
    Write-Host '*** EXPERIMENTAL ***  this Windows/WSL2 path is syntax-reviewed only - it has'
    Write-Host 'not yet been drill-tested on real Windows hardware. Treat every step above as'
    Write-Host 'something to verify by hand until that drill has run (roadmap Part 2, principle 4).'
    Write-Host 'Specifically UNVERIFIABLE without real Windows hardware - be extra skeptical of:'
    Write-Host '   1. wsl --install --name acceptance and its exact exit codes across WSL versions'
    Write-Host '   2. wsl --list stderr/encoding behavior on this exact PowerShell/Windows build'
    Write-Host '   3. $MyInvocation/$PSCommandPath behavior under a bare iex vs the parameterized'
    Write-Host '      scriptblock form above'
    Write-Host '   4. native-argument quoting/escaping of characters like | and > passed through wsl.exe'
    Write-Host '   5. /dev/tty availability from inside a command wsl.exe launches non-interactively'
    Write-Host '   6. whether -u root actually bypasses a fresh Ubuntu images one-time OOBE prompt'
    Write-Host '   7. systemd/interop settings being honored identically across WSL kernel versions'
    Write-Host '   8. the Ubuntu WSL app package image manifest (whether -d Ubuntu resolves consistently)'
    Write-Host '   9. [Environment]::UserInteractive in unusual hosts (Task Scheduler, remote sessions)'
    Write-Host '  10. registry CurrentBuild reads on Insider/ARM64/LTSC Windows builds'
}

function Main {
    Write-Section 'Guest-house install (Windows/WSL2) - EXPERIMENTAL'
    Write-Host 'This sets up an isolated WSL2 distro dedicated to the factory, hardened so it'
    Write-Host 'cannot see your Windows files or run Windows programs, then hands off to'
    Write-Host 'install.sh --guest-house --wsl inside it. Full rules:'
    Write-Host ($RunbookBaseUrl + '/docs/runbooks/guest-house.md')

    if (-not (Test-Preflight)) {
        return 1
    }
    if (-not (Install-GuestHouseDistro)) {
        return 1
    }
    if (-not (Install-DistroDependencies)) {
        return 1
    }
    if (-not (Set-WslHardening)) {
        return 1
    }
    $installerStatus = Invoke-GuestHouseBashInstaller

    Write-ClosingSummary -InstallerStatus $installerStatus
    if ($installerStatus -eq 'failed') {
        return 1
    }
    return 0
}

$script:GuestHouseExitCode = Main
# `exit` would close the caller's whole PowerShell console when this file is dot-sourced via
# `irm ... | iex` (no backing file, no real script process). $PSCommandPath - unlike
# $MyInvocation.MyCommand.Path, which can be $null in exactly that scenario and, under
# Set-StrictMode -Version Latest, THROWS when a property is accessed on a $null value - is
# simply an empty string when there is no backing file, so this comparison is always safe.
if ($PSCommandPath) {
    exit $script:GuestHouseExitCode
}
