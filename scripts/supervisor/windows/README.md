# Windows auto-start — polycopy WSL2 (M12_bis Phase F §4.8.3)

Deux couches distinctes :

1. **Couche extérieure** (Task Scheduler XML) — démarre WSL2 au logon.
2. **Couche intérieure** (systemd inside WSL2 ou fallback PowerShell) —
   supervise polycopy.

## Pour qui

Windows 10 (22H2+ recommandé) ou Windows 11, avec WSL2 installé.

## Prérequis WSL2

```powershell
# Vérifier version WSL (≥ 0.67.6 pour support systemd inside)
wsl --version

# Update si < 0.67.6
wsl --update
```

## Setup recommandé (WSL2 + systemd)

### 1. Activer systemd dans WSL2 (à faire une fois)

Depuis WSL2 (Ubuntu) :

```bash
sudo tee /etc/wsl.conf > /dev/null <<'EOF'
[boot]
systemd=true
EOF
```

Depuis PowerShell (host Windows) :

```powershell
wsl --shutdown
# Relancer WSL → systemd doit tourner
wsl -d Ubuntu systemctl is-system-running
# Output attendu : "running" (ou "degraded" si quelques services fail,
# OK tant que systemd lui-même est up)
```

### 2. Installer l'unit systemd inside WSL

Voir [`../systemd/README.md`](../systemd/README.md) — install `.service` inside
WSL2 exactement comme sur Debian native.

### 3. Couche extérieure — Task Scheduler

Importer la task :

```powershell
# Substituer {{WIN_USER}} dans le XML avant import
$winUser = "$env:USERDOMAIN\$env:USERNAME"
(Get-Content polycopy-wsl-autostart.xml) -replace '\{\{WIN_USER\}\}', $winUser `
    | Set-Content polycopy-wsl-autostart.rendered.xml

# Import dans Task Scheduler
schtasks /create /xml polycopy-wsl-autostart.rendered.xml /tn "polycopy-wsl-autostart"

# Vérifier
schtasks /query /tn "polycopy-wsl-autostart"
```

Au prochain logon, WSL2 boot automatiquement → systemd inside démarre polycopy.

## Fallback — WSL2 sans systemd (vieilles versions)

Si `wsl --version` < 0.67.6 ou impossible d'éditer `/etc/wsl.conf` :

### 1. Substituer les placeholders dans `polycopy-wsl-respawn.ps1`

```powershell
$scriptContent = Get-Content polycopy-wsl-respawn.ps1 -Raw
$scriptContent = $scriptContent -replace '\{\{POLYCOPY_PATH_WSL\}\}', '/home/elie/code/polycopy'
$scriptContent = $scriptContent -replace '\{\{VENV_PATH_WSL\}\}', '/home/elie/code/polycopy/.venv'
Set-Content -Path "$env:USERPROFILE\polycopy-wsl-respawn.ps1" -Value $scriptContent
```

### 2. Créer une task Task Scheduler manuelle

- Trigger : **At log on of user**.
- Action : **Start a program**.
  - Program : `powershell.exe`.
  - Arguments : `-WindowStyle Hidden -ExecutionPolicy Bypass -File "%USERPROFILE%\polycopy-wsl-respawn.ps1"`.
- Settings : **Run whether user is logged on or not** (sinon la task s'arrête au logoff).

### 3. Vérifier les logs

```powershell
Get-Content $env:USERPROFILE\polycopy-respawn.log -Tail 20 -Wait
```

## Lifecycle attendu

Identique aux superviseurs systemd/launchd :

| Commande | Effet |
|---|---|
| `POST /v1/restart/<machine>` | respawn ≤ 5s → **running** |
| `POST /v1/stop/<machine>`    | `halt.flag` posé → respawn → **paused** |
| `POST /v1/resume/<machine>`  | `halt.flag` retiré → respawn → **running** |

## Recovery manuelle

Depuis PowerShell (ou un terminal WSL) :

```bash
# Force resume
wsl -d Ubuntu bash -lc "rm ~/.polycopy/halt.flag"

# Avec systemd inside :
wsl -d Ubuntu systemctl --user restart polycopy

# Sans systemd (fallback) : la task scheduler relancera automatiquement
# après le prochain exit du bot. Ou tuer le process manuellement :
wsl -d Ubuntu pkill -f "polycopy --no-cli"
```

## Désinstall

```powershell
# Couche extérieure
schtasks /delete /tn "polycopy-wsl-autostart" /f

# Couche intérieure systemd (depuis WSL)
wsl -d Ubuntu systemctl --user disable --now polycopy
```
