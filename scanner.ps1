# Setze Arbeitsverzeichnis
Set-Location -Path "F:\pixai-sensible"

# Aktiviere virtuelle Umgebung (angepasst!)
$venvActivate = ".\venv310\Scripts\Activate.ps1"
if (Test-Path $venvActivate) {
    Write-Host "Aktiviere virtuelle Umgebung..."
    . $venvActivate
} else {
    Write-Host "Virtuelle Umgebung nicht gefunden: $venvActivate"
}

# Starte Scanner API
Write-Host "`nStarte scanner_api.py ..."
python scanner_api.py

# Halte Fenster offen nach Beenden
Write-Host "`nBeendet. Drücke eine Taste zum Schließen..."
$null = $Host.UI.RawUI.ReadKey("NoEcho,IncludeKeyDown")
