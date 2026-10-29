param(
    [Parameter(Mandatory = $true)]
    [int]$ParentProcessId,
    [Parameter(Mandatory = $true)]
    [string]$InstallerPath,
    [Parameter(Mandatory = $true)]
    [string]$ApplicationPath
)

$ErrorActionPreference = "Stop"

function Show-UpdateError([string]$Message) {
    try {
        Add-Type -AssemblyName System.Windows.Forms
        [System.Windows.Forms.MessageBox]::Show(
            $Message,
            "MotionSync update",
            [System.Windows.Forms.MessageBoxButtons]::OK,
            [System.Windows.Forms.MessageBoxIcon]::Error
        ) | Out-Null
    } catch {
        Write-Error $Message
    }
}

try {
    Wait-Process -Id $ParentProcessId -ErrorAction SilentlyContinue

    if (-not (Test-Path -LiteralPath $InstallerPath -PathType Leaf)) {
        throw "The downloaded installer could not be found."
    }
    if (-not (Test-Path -LiteralPath $ApplicationPath -PathType Leaf)) {
        throw "The MotionSync application could not be found."
    }

    $installer = Start-Process `
        -FilePath $InstallerPath `
        -ArgumentList @(
            "/VERYSILENT",
            "/SUPPRESSMSGBOXES",
            "/NORESTART",
            "/CLOSEAPPLICATIONS"
        ) `
        -Wait `
        -PassThru

    if ($installer.ExitCode -ne 0) {
        throw "The installer exited with code $($installer.ExitCode)."
    }

    Start-Process -FilePath $ApplicationPath
} catch {
    Show-UpdateError "MotionSync could not finish installing the update.`n`n$($_.Exception.Message)"
    if (Test-Path -LiteralPath $ApplicationPath -PathType Leaf) {
        Start-Process -FilePath $ApplicationPath
    }
    exit 1
} finally {
    Remove-Item -LiteralPath $InstallerPath -Force -ErrorAction SilentlyContinue
}
