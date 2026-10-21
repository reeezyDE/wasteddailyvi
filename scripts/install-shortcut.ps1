# Legt eine Desktop-Verknuepfung "WastedDaily" an (Doppelklick = Pipeline + Dashboard starten).
# Optional mit -Autostart: startet zusaetzlich automatisch bei der Windows-Anmeldung.
#   powershell -ExecutionPolicy Bypass -File scripts\install-shortcut.ps1 [-Autostart]
param([switch]$Autostart)
$ErrorActionPreference = "Stop"
$repo = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
$target = Join-Path $repo "scripts\start-windows.bat"
$shell = New-Object -ComObject WScript.Shell

function New-Link($path) {
    $lnk = $shell.CreateShortcut($path)
    $lnk.TargetPath = $target
    $lnk.WorkingDirectory = $repo
    $lnk.WindowStyle = 7
    $lnk.Description = "WastedDaily: Pipeline starten und Dashboard oeffnen"
    $lnk.Save()
    Write-Host "Verknuepfung angelegt: $path"
}
New-Link (Join-Path ([Environment]::GetFolderPath("Desktop")) "WastedDaily.lnk")
if ($Autostart) { New-Link (Join-Path ([Environment]::GetFolderPath("Startup")) "WastedDaily.lnk") }
