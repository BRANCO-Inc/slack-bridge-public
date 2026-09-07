[CmdletBinding()]
param(
    [ValidateSet("setup", "configure", "doctor", "run")]
    [string]$Action,
    [string]$Distro = "Ubuntu",
    [string]$ProjectPath
)

Set-StrictMode -Version Latest

function Assert-WindowsBridgeArguments {
    param(
        [string]$SelectedDistro,
        [string]$SelectedProjectPath
    )

    if ($SelectedDistro -notmatch "^[A-Za-z0-9._-]+$") {
        throw "Distro must be an installed WSL distribution name, for example Ubuntu."
    }
    if (
        $SelectedProjectPath -notmatch "^/home/.+" -or
        $SelectedProjectPath -match "[\x00\r\n\\]"
    ) {
        throw "ProjectPath must be a Linux path below /home, not a Windows or /mnt path."
    }
}

function Get-WslDistributionNames {
    try {
        $names = @(& wsl.exe --list --quiet)
    }
    catch {
        throw "WSL was not found. Install WSL2 and Ubuntu first; see docs/windows.md."
    }
    if ($LASTEXITCODE -ne 0) {
        throw "Could not list WSL distributions. Install WSL2 and Ubuntu first; see docs/windows.md."
    }
    return @(
        $names |
            ForEach-Object { $_.Replace([string][char]0, "").Trim([char]0xFEFF, [char]32) } |
            Where-Object { $_ }
    )
}

function Assert-Wsl2Distribution {
    param([string]$SelectedDistro)

    $installed = Get-WslDistributionNames
    if ($installed -notcontains $SelectedDistro) {
        throw "WSL distribution '$SelectedDistro' is not installed. Run wsl --list --online, then install it before retrying."
    }

    $details = @(& wsl.exe --list --verbose)
    if ($LASTEXITCODE -ne 0) {
        throw "Could not read the WSL version for '$SelectedDistro'. Run wsl --list --verbose and repair WSL before retrying."
    }
    $escapedDistro = [regex]::Escape($SelectedDistro)
    $wsl2Pattern = "^\s*\*?\s*$escapedDistro\s+\S+\s+2\s*$"
    $isWsl2 = $details | Where-Object {
        $_.Replace([string][char]0, "").Trim([char]0xFEFF, [char]32) -match $wsl2Pattern
    }
    if (-not $isWsl2) {
        throw "WSL distribution '$SelectedDistro' is not running as WSL2. Run wsl --set-version $SelectedDistro 2, then retry."
    }
}

function Invoke-SlackBridgeWindows {
    param(
        [ValidateSet("setup", "configure", "doctor", "run")]
        [string]$SelectedAction,
        [string]$SelectedDistro = "Ubuntu",
        [string]$SelectedProjectPath
    )

    Assert-WindowsBridgeArguments -SelectedDistro $SelectedDistro -SelectedProjectPath $SelectedProjectPath
    Assert-Wsl2Distribution -SelectedDistro $SelectedDistro
    & wsl.exe --distribution $SelectedDistro --cd $SelectedProjectPath --exec bash -l scripts/wsl.sh $SelectedAction
}

if ($MyInvocation.InvocationName -ne ".") {
    if (-not $Action -or -not $ProjectPath) {
        Write-Error "Usage: .\\scripts\\windows.ps1 -Action setup|configure|doctor|run -Distro Ubuntu -ProjectPath /home/<user>/src/slack-bridge-public"
        exit 2
    }
    try {
        Invoke-SlackBridgeWindows -SelectedAction $Action -SelectedDistro $Distro -SelectedProjectPath $ProjectPath
        exit $LASTEXITCODE
    }
    catch {
        Write-Error $_.Exception.Message
        exit 2
    }
}
