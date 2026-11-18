# Undoes allow-surround-without-prompt.ps1: removes the extra permission from the PRO X 2 surround key.
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

if (-not $UserSid) {
    $signedIn = (Get-CimInstance Win32_ComputerSystem).UserName
    $UserSid = (New-Object Security.Principal.NTAccount($signedIn)).Translate([Security.Principal.SecurityIdentifier]).Value
}

$root = 'HKLM:\SOFTWARE\Logitech\GHUB\Audio Devices'
$removed = 0
foreach ($k in Get-ChildItem $root -ErrorAction SilentlyContinue) {
    $acl = Get-Acl $k.PSPath
    $rules = $acl.Access | Where-Object { -not $_.IsInherited -and $_.IdentityReference.Translate(
        [Security.Principal.SecurityIdentifier]).Value -eq $UserSid }
    foreach ($r in $rules) { [void]$acl.RemoveAccessRule($r); $removed++ }
    if ($rules) { Set-Acl -Path $k.PSPath -AclObject $acl }
}
Write-Host "Removed $removed permission entr$(if ($removed -eq 1) {'y'} else {'ies'})." -ForegroundColor Green
