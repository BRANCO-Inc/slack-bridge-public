$ErrorActionPreference = "Stop"

$script:WslCalls = @()
$script:ActionExitCode = 0

function global:wsl.exe {
    param([Parameter(ValueFromRemainingArguments = $true)][string[]]$Arguments)

    $script:WslCalls += , @($Arguments)
    if ($Arguments.Count -eq 2 -and $Arguments[0] -eq "--list" -and $Arguments[1] -eq "--quiet") {
        "U$([char]0)buntu$([char]0)"
        $global:LASTEXITCODE = 0
        return
    }
    if ($Arguments.Count -eq 2 -and $Arguments[0] -eq "--list" -and $Arguments[1] -eq "--verbose") {
        "  NAME      STATE           VERSION"
        "* U$([char]0)buntu$([char]0)    Running         2"
        $global:LASTEXITCODE = 0
        return
    }
    $global:LASTEXITCODE = $script:ActionExitCode
}

function Assert-True {
    param([bool]$Condition, [string]$Message)

    if (-not $Condition) {
        throw "Assertion failed: $Message"
    }
}

function Reset-WslMock {
    param([int]$ExitCode = 0)

    $script:WslCalls = @()
    $script:ActionExitCode = $ExitCode
    $global:LASTEXITCODE = 0
}

. (Join-Path $PSScriptRoot "windows.ps1")

$projectPath = "/home/alice/src/slack-bridge-public"

Reset-WslMock
Invoke-SlackBridgeWindows -SelectedAction setup -SelectedProjectPath $projectPath
Assert-True ($LASTEXITCODE -eq 0) "setup propagates a successful WSL exit code"
Assert-True ($script:WslCalls.Count -eq 3) "setup checks the named distribution and invokes it once"
$setupCall = $script:WslCalls[2]
Assert-True ($setupCall[0] -eq "--distribution" -and $setupCall[1] -eq "Ubuntu") "setup targets the requested distribution"
Assert-True ($setupCall[2] -eq "--cd" -and $setupCall[3] -eq $projectPath) "setup sets the WSL Linux project directory directly"
Assert-True ($setupCall[4] -eq "--exec" -and $setupCall[5] -eq "bash" -and $setupCall[6] -eq "-l") "setup uses the WSL bash login shell"
Assert-True ($setupCall[7] -eq "scripts/wsl.sh" -and $setupCall[8] -eq "setup") "setup invokes the fixed WSL script with an action argument"

Reset-WslMock
Invoke-SlackBridgeWindows -SelectedAction configure -SelectedDistro Ubuntu -SelectedProjectPath $projectPath
$configureCall = $script:WslCalls[2]
Assert-True ($configureCall[7] -eq "scripts/wsl.sh" -and $configureCall[8] -eq "configure") "configure passes the selected action to the WSL script"

Reset-WslMock -ExitCode 17
Invoke-SlackBridgeWindows -SelectedAction doctor -SelectedDistro Ubuntu -SelectedProjectPath $projectPath
Assert-True ($LASTEXITCODE -eq 17) "doctor preserves the WSL process exit code"

Reset-WslMock
$quotedPath = "/home/alice/bridge; touch should-not-run"
Invoke-SlackBridgeWindows -SelectedAction run -SelectedDistro Ubuntu -SelectedProjectPath $quotedPath
$runCall = $script:WslCalls[2]
Assert-True ($runCall[3] -eq $quotedPath) "project path is never interpolated into a shell command"
Assert-True ($runCall[7] -eq "scripts/wsl.sh" -and $runCall[8] -eq "run") "run passes only the fixed script and action to bash"

Reset-WslMock
$invalidPathRaised = $false
try {
    Invoke-SlackBridgeWindows -SelectedAction doctor -SelectedDistro Ubuntu -SelectedProjectPath "/mnt/c/slack-bridge-public"
}
catch {
    $invalidPathRaised = $_.Exception.Message -match "Linux path below /home"
}
Assert-True $invalidPathRaised "Windows-mounted project paths are rejected before WSL runs"
Assert-True ($script:WslCalls.Count -eq 0) "invalid project paths do not invoke WSL"

Write-Output "windows launcher tests passed"
