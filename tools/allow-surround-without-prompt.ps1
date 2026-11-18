# Lets Device Hub change PRO X 2 surround settings without a Windows permission prompt.
#
# What it does: gives ONE Windows account (by default, the user signed in to this PC) permission to
# read and write ONE registry key: the one G HUB uses for the PRO X 2 speakers' DTS settings:
#   HKLM\SOFTWARE\Logitech\GHUB\Audio Devices\{0.0.0.00000000}.{<PRO X 2 speaker endpoint id>}
# Nothing else is changed. Undo with: tools\undo-allow-surround-without-prompt.ps1
#
# Run as administrator:
#   powershell -ExecutionPolicy Bypass -File "<this file>"
param([string]$UserSid)

$ErrorActionPreference = 'Stop'

if (-not ([Security.Principal.WindowsPrincipal][Security.Principal.WindowsIdentity]::GetCurrent()).IsInRole(
        [Security.Principal.WindowsBuiltInRole]::Administrator)) {
    Write-Host 'Please run this from an administrator PowerShell.' -ForegroundColor Red
    exit 1
}

# Who gets access: the signed-in user (not necessarily the admin account running this script).
if (-not $UserSid) {
    $signedIn = (Get-CimInstance Win32_ComputerSystem).UserName
    $UserSid = (New-Object Security.Principal.NTAccount($signedIn)).Translate([Security.Principal.SecurityIdentifier]).Value
    Write-Host "Signed-in user: $signedIn ($UserSid)"
}

# Find the PRO X 2 speaker endpoint.
$render = 'HKLM:\SOFTWARE\Microsoft\Windows\CurrentVersion\MMDevices\Audio\Render'
$endpoint = Get-ChildItem $render | Where-Object {
    $p = Get-ItemProperty "$($_.PSPath)\Properties" -ErrorAction SilentlyContinue
    (Get-ItemProperty $_.PSPath).DeviceState -eq 1 -and $p.'{b3f8fa53-0004-438e-9003-51a46e139bfc},6' -match 'PRO X 2'
} | Select-Object -First 1
if (-not $endpoint) {
    Write-Host 'PRO X 2 speakers not found. Plug in the headset dongle and try again.' -ForegroundColor Red
    exit 1
}
$key = "HKLM:\SOFTWARE\Logitech\GHUB\Audio Devices\{0.0.0.00000000}.$($endpoint.PSChildName)"
if (-not (Test-Path $key)) { New-Item -Path $key -Force | Out-Null }

$acl = Get-Acl $key
$rule = New-Object Security.AccessControl.RegistryAccessRule(
    (New-Object Security.Principal.SecurityIdentifier($UserSid)), 'QueryValues, SetValue, ReadKey', 'None', 'None', 'Allow')
$acl.AddAccessRule($rule)
Set-Acl -Path $key -AclObject $acl

Write-Host "Done. This account can now change surround settings in:" -ForegroundColor Green
Write-Host "  $key"
