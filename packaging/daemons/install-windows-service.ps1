# Potluck mesh sidecar — Windows Service install script template.
#
# Run during .msi installation (CustomAction) AFTER the binary is
# laid down on disk. Registers potluck-mesh as a Windows Service that
# starts automatically at boot under a dedicated low-privilege service
# account.
#
# Substitution tokens (rendered by the WiX installer):
#   @BINARY_PATH@         — absolute path to the installed sidecar binary
#                              e.g. C:\Program Files\Potluck\bin\potluck-mesh.exe
#   @COORDINATOR_URL@     — defaults to the cooperative's hosted URL
#   @KEY_DIR@             — %ProgramData%\Potluck\keys (ACL'd to LocalService)
#   @LOG_DIR@             — %ProgramData%\Potluck\logs
#
# Why a dedicated account (rather than SYSTEM): SYSTEM has too much
# privilege. wintun + the named-pipe IPC don't need it. We create a
# `NT SERVICE\PotluckMesh` virtual account at service-create time.

[CmdletBinding()]
param(
    [Parameter(Mandatory=$true)]
    [string]$BinaryPath = "@BINARY_PATH@",

    [Parameter(Mandatory=$false)]
    [string]$CoordinatorUrl = "@COORDINATOR_URL@",

    [Parameter(Mandatory=$false)]
    [string]$KeyDir = "@KEY_DIR@",

    [Parameter(Mandatory=$false)]
    [string]$LogDir = "@LOG_DIR@"
)

$ErrorActionPreference = "Stop"
$ServiceName = "PotluckMesh"
$DisplayName = "Potluck Mesh"
$Description = "Cooperative AI mesh sidecar — connects this machine to its household mesh."

# 1. Ensure the working directories exist with the right ACLs.
foreach ($dir in @($KeyDir, $LogDir)) {
    if (-not (Test-Path $dir)) {
        New-Item -ItemType Directory -Path $dir -Force | Out-Null
        Write-Host "Created $dir"
    }
}

# 2. Stop + remove any existing instance of the service so reinstalls work.
$existing = Get-Service -Name $ServiceName -ErrorAction SilentlyContinue
if ($existing) {
    Write-Host "Stopping existing service $ServiceName..."
    Stop-Service -Name $ServiceName -Force -ErrorAction SilentlyContinue
    sc.exe delete $ServiceName | Out-Null
    Start-Sleep -Seconds 2
}

# 3. Register the service. binPath= must use the full path with arguments
#    inline. The service runs under NT SERVICE\PotluckMesh which is a
#    virtual account auto-created by SCM when we use that name.
$binPath = "`"$BinaryPath`" --coordinator-url $CoordinatorUrl --log-format json"

sc.exe create $ServiceName `
    binPath= $binPath `
    DisplayName= "$DisplayName" `
    start= auto `
    obj= "NT SERVICE\$ServiceName"

if ($LASTEXITCODE -ne 0) {
    Write-Error "sc.exe create failed with exit code $LASTEXITCODE"
    exit 1
}

# 4. Set the description.
sc.exe description $ServiceName "$Description" | Out-Null

# 5. ACL the key + log directories to allow the service account.
#    NT SERVICE\PotluckMesh needs (Read+Write) on KeyDir, (Write) on LogDir.
$acl = Get-Acl $KeyDir
$rule = New-Object System.Security.AccessControl.FileSystemAccessRule(
    "NT SERVICE\$ServiceName", "FullControl", "ContainerInherit,ObjectInherit", "None", "Allow")
$acl.SetAccessRule($rule)
Set-Acl -Path $KeyDir -AclObject $acl

$acl = Get-Acl $LogDir
$acl.SetAccessRule($rule)
Set-Acl -Path $LogDir -AclObject $acl

# 6. Configure recovery actions — restart on failure with 10-second delay,
#    up to 3 times, then leave it stopped so the user can investigate.
sc.exe failure $ServiceName reset= 86400 actions= restart/10000/restart/10000/restart/10000 | Out-Null

# 7. Start the service.
Write-Host "Starting service $ServiceName..."
Start-Service -Name $ServiceName

Write-Host "Service $ServiceName installed + started successfully."
