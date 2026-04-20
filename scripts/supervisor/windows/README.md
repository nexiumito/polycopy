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

Depuis PowerShell (host Windows). Le XML vit dans WSL — on le lit via le
share `\\wsl.localhost\` et on rend une copie locale avant import.

Important :
- **Encodage** : le XML déclare `encoding="UTF-16"` ; sauve avec
  `-Encoding Unicode` (= UTF-16 LE BOM) pour que `schtasks` parse OK.
- **Distro WSL** : si la tienne ne s'appelle pas `Ubuntu`, ajuste le
  `<Arguments>` du XML (`-d <ta-distro>`) avant rendu.
- **Adapte le chemin WSL** si ton user Linux n'est pas `nexium` ou si
  le repo n'est pas dans `~/code/polycopy`.

```powershell
# Rendu (UNC path WSL → fichier local Windows)
$src = "\\wsl.localhost\Ubuntu\home\nexium\code\polycopy\scripts\supervisor\windows\polycopy-wsl-autostart.xml"
$dst = "$env:USERPROFILE\polycopy-wsl-autostart.rendered.xml"
$winUser = "$env:USERDOMAIN\$env:USERNAME"

(Get-Content $src -Raw) -replace '\{\{WIN_USER\}\}', $winUser `
    | Set-Content $dst -Encoding Unicode

# Import (/f écrase une task partielle laissée par un essai précédent)
schtasks /create /xml $dst /tn "polycopy-wsl-autostart" /f

# Vérifier l'enregistrement
schtasks /query /tn "polycopy-wsl-autostart"

# Smoke test sans relogin : déclenche la task à la main
schtasks /run /tn "polycopy-wsl-autostart"
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
