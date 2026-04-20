# Fallback PowerShell — supervise polycopy dans WSL2 sans systemd inside.
# M12_bis Phase F §4.8.3. À utiliser uniquement si :
# - `wsl --version` < 0.67.6 (pas de support systemd).
# - Ou `/etc/wsl.conf` ne peut PAS être modifié (Ubuntu instance shared).
#
# Discipline identique systemd `Restart=always` : respawn quel que soit
# l'exit code, 5s entre tentatives.
#
# Placeholders :
# - {{POLYCOPY_PATH_WSL}} : chemin WSL du repo (ex. /home/elie/code/polycopy).
# - {{VENV_PATH_WSL}}     : chemin WSL du venv (ex. /home/elie/code/polycopy/.venv).
#
# Configuration Task Scheduler :
#   Action : powershell.exe -WindowStyle Hidden -File <path>\polycopy-wsl-respawn.ps1
#   Trigger : At log on of user
#
# Vérifier les logs : `Get-Content $env:USERPROFILE\polycopy-respawn.log -Tail 20 -Wait`

$logFile = Join-Path $env:USERPROFILE "polycopy-respawn.log"

function Write-Log {
    param([string]$msg)
    $timestamp = Get-Date -Format "yyyy-MM-dd HH:mm:ss"
    "$timestamp $msg" | Out-File -FilePath $logFile -Append -Encoding utf8
}

Write-Log "polycopy-wsl-respawn started (WSL2 systemd fallback)"

while ($true) {
    Write-Log "spawn: wsl -d Ubuntu python -m polycopy --no-cli"
    # Commande : cd au repo, active le venv, lance le bot.
    # Exit code capturé dans $LASTEXITCODE.
    wsl.exe -d Ubuntu --exec bash -lc "cd {{POLYCOPY_PATH_WSL}} && {{VENV_PATH_WSL}}/bin/python -m polycopy --no-cli"
    $exitCode = $LASTEXITCODE
    Write-Log "polycopy exited with code $exitCode — respawning in 5s"
    Start-Sleep -Seconds 5
}
