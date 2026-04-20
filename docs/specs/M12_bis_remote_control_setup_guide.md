# M12_bis — Setup guide utilisateur (Tailscale + Remote Control)

> **Audience** : Elie. À suivre pas-à-pas pour activer le remote control
> sur les 3 machines cibles (Debian 12 université / macOS MacBook /
> Windows 10 + WSL2 maison).
>
> **Estimation** : 30-45 min par machine + 10 min tailnet setup initial.
>
> **⚠️ Commandes documentées d'après l'implémentation M12_bis** — non
> testées end-to-end à l'écriture. Reporter tout écart dans §12
> Troubleshooting pour raffiner le guide.

---

## 1. Prérequis

- [ ] Compte **Tailscale** (free tier suffit — 100 devices max, 3 users).
      Sign-up : <https://login.tailscale.com/start> (Google/GitHub/email).
- [ ] Téléphone **iOS** ou **Android** avec app Tailscale installée
      ([App Store](https://apps.apple.com/app/tailscale/id1470499037) /
      [Play Store](https://play.google.com/store/apps/details?id=com.tailscale.ipn)).
- [ ] Une app **TOTP** sur le téléphone : Google Authenticator /
      1Password / Bitwarden / Authy. Elle doit être protégée par
      biométrie **indépendamment** du déverrouillage phone
      (sinon 2FA bypass trivial si phone volé déverrouillé).
- [ ] **Accès admin/root** sur chacune des 3 machines cibles.
- [ ] **Polycopy M12_bis** installé sur chaque machine
      (`git pull && scripts/setup.sh`).

## 2. Installation Tailscale par OS

### 2.1 Debian 12 (machine université)

```bash
curl -fsSL https://tailscale.com/install.sh | sh
sudo tailscale up --hostname=uni-debian

# Suivre le lien affiché dans le navigateur pour enrôler la machine
# dans le tailnet. Le flag --hostname fixe le nom qui apparaîtra dans
# MagicDNS (ex. uni-debian.tailXXXX.ts.net).

# Vérifier
tailscale status
tailscale ip -4
# Output attendu : 100.64.x.x (plage CGNAT Tailscale)
```

### 2.2 macOS (MacBook)

Deux options, au choix :

**Option A — App Store (recommandé)** :
1. Installer [Tailscale](https://apps.apple.com/app/tailscale/id1475387142)
   depuis le Mac App Store.
2. Ouvrir l'app → "Log in" → auth via le navigateur.
3. Dans le menu barre (icône Tailscale) → Preferences → mettre le
   hostname à `macbook`.

**Option B — CLI Homebrew** :

```bash
brew install tailscale
sudo tailscale up --hostname=macbook
# Le binaire `tailscale` est dans /opt/homebrew/bin/ (Apple Silicon)
# ou /usr/local/bin/ (Intel). Les scripts launchd couvrent les 2 dans
# EnvironmentVariables.PATH.

tailscale ip -4
```

### 2.3 Windows 10 + WSL2

**Coucher extérieure (Windows)** — Tailscale app Windows :

1. Télécharger [Tailscale pour Windows](https://tailscale.com/download/windows)
   et installer.
2. Démarrer → Log in via le navigateur.
3. Via le tray icon → Preferences → hostname `pc-fixe`.

**Couche intérieure (WSL2)** — le daemon Tailscale Windows expose
l'interface sur la machine hôte, et `wsl.exe` hérite de la configuration
réseau. **Aucune install Tailscale inside WSL2 nécessaire** — mais
`tailscale ip -4` doit marcher dans le shell WSL.

Test :

```bash
# Depuis WSL2 Ubuntu
tailscale ip -4  # doit retourner 100.64.x.x
```

Si `tailscale ip -4` échoue dans WSL2 ("command not found"), deux options :
- (a) Ajouter `/mnt/c/Program\ Files/Tailscale/` au `$PATH` dans
  `~/.bashrc`.
- (b) Installer Tailscale natif dans WSL2 :
  `curl -fsSL https://tailscale.com/install.sh | sh`. Dans ce cas,
  **ne pas** faire `tailscale up` dans WSL (conflit avec le host).

### 2.4 Téléphone

1. App Tailscale → Log in.
2. Vérifier que les 3 machines apparaissent dans la liste.
3. Vérifier la résolution MagicDNS : tenter un ping dans l'app :
   `pc-fixe`, `macbook`, `uni-debian`.

## 3. Alignement hostnames / MACHINE_ID / emoji

Règle : la valeur `MACHINE_ID` de chaque `.env` doit matcher le hostname
Tailscale (case-insensitive).

| Machine | `MACHINE_ID` (.env) | Tailscale hostname | `MACHINE_EMOJI` suggéré |
|---|---|---|---|
| PC fixe maison (WSL2) | `PC-FIXE` | `pc-fixe` | 🖥️ |
| MacBook portable | `MACBOOK` | `macbook` | 💻 |
| PC université (Debian) | `UNI-DEBIAN` | `uni-debian` | 🏫 |

Vérifier :

```bash
tailscale status | head -5
# La 1ère ligne doit contenir le hostname attendu.
```

## 4. Activation WSL2 systemd (Windows 10 uniquement)

```powershell
# PowerShell admin
wsl --version
# Vérifier WSL version ≥ 0.67.6 (support systemd).
# Si < 0.67.6 :
wsl --update
```

```bash
# WSL2 Ubuntu
sudo tee /etc/wsl.conf > /dev/null <<'EOF'
[boot]
systemd=true
EOF
```

```powershell
# Retour PowerShell
wsl --shutdown
# Attendre 10s, puis relancer WSL2
wsl -d Ubuntu systemctl is-system-running
# Output attendu : "running" ou "degraded" (OK tant que systemd lui-même up)
```

Fallback si impossible d'activer systemd : voir
[`scripts/supervisor/windows/README.md`](../../scripts/supervisor/windows/README.md) §Fallback.

## 5. Génération du secret TOTP (par machine)

**Règle** : un secret différent par machine, un QR code par machine,
scanné dans l'authenticator avec un label explicite (`polycopy-PC-FIXE`,
etc.). Permet de révoquer une machine sans casser les autres.

Sur chaque machine :

```bash
# Générer un secret base32 32 chars
python -c "import pyotp; print(pyotp.random_base32())"
# Copier la valeur retournée (ex. JBSWY3DPEHPK3PXPJBSWY3DPEHPK3PXP)

# Générer l'URL provisioning + QR (Python a qrcode installé ? Sinon pip install qrcode[pil])
pip install 'qrcode[pil]'
python -c "
import pyotp, qrcode
SECRET = 'JBSWY3DPEHPK3PXPJBSWY3DPEHPK3PXP'  # <-- remplacer
MACHINE = 'PC-FIXE'  # <-- remplacer
uri = pyotp.TOTP(SECRET).provisioning_uri(f'polycopy-{MACHINE}', issuer_name='polycopy')
qrcode.make(uri).save(f'totp-{MACHINE}.png')
print(uri)
"
# Fichier totp-<MACHINE>.png généré → scanner dans l'authenticator
# depuis le téléphone.

# VÉRIFICATION OBLIGATOIRE avant de continuer :
python -c "
import pyotp
SECRET = 'JBSWY3DPEHPK3PXPJBSWY3DPEHPK3PXP'  # <-- même valeur
print('Code TOTP actuel :', pyotp.TOTP(SECRET).now())
"
# Comparer avec le code affiché dans l'authenticator pour polycopy-PC-FIXE.
# Ils doivent matcher. Si pas, clock skew → sync NTP.

# IMPORTANT : supprimer le QR PNG après scan (il contient le secret)
rm totp-PC-FIXE.png
```

### ⚠️ Sécurité

- **Ne jamais committer `.env`** — déjà dans `.gitignore`.
- **Rotation trimestrielle** : régénérer + replacer dans `.env` + scanner
  un nouveau QR. L'ancienne entrée authenticator à supprimer.
- Le QR PNG contient le secret → supprimer après scan (`shred -u` sur
  Linux pour effacement sécurisé).

## 6. Installation unit systemd (Debian 12 + WSL2)

Depuis `scripts/supervisor/systemd/` (dans le repo) :

```bash
# Depuis la racine du repo
cd scripts/supervisor/systemd/

# Substituer les placeholders
POLYCOPY_PATH="$HOME/code/polycopy"  # <-- adapter si différent
VENV_PATH="$POLYCOPY_PATH/.venv"
sed -e "s|{{POLYCOPY_PATH}}|$POLYCOPY_PATH|g" \
    -e "s|{{VENV_PATH}}|$VENV_PATH|g" \
    polycopy.service > ~/.config/systemd/user/polycopy.service

# Enable user-linger (survit à la déconnexion SSH)
sudo loginctl enable-linger "$USER"

# Enable + start
systemctl --user daemon-reload
systemctl --user enable --now polycopy

# Vérifier
systemctl --user status polycopy
journalctl --user -u polycopy -n 50
```

Voir aussi [scripts/supervisor/systemd/README.md](../../scripts/supervisor/systemd/README.md).

## 7. Installation LaunchAgent (macOS)

```bash
cd scripts/supervisor/launchd/

# Substituer
POLYCOPY_PATH="$HOME/code/polycopy"
VENV_PATH="$POLYCOPY_PATH/.venv"
sed -e "s|{{POLYCOPY_PATH}}|$POLYCOPY_PATH|g" \
    -e "s|{{VENV_PATH}}|$VENV_PATH|g" \
    fr.polycopy.bot.plist > ~/Library/LaunchAgents/fr.polycopy.bot.plist
chmod 0644 ~/Library/LaunchAgents/fr.polycopy.bot.plist

# Créer le dossier de logs launchd
mkdir -p "$POLYCOPY_PATH/logs"

# Load
launchctl load ~/Library/LaunchAgents/fr.polycopy.bot.plist

# Vérifier
launchctl list | grep polycopy
tail -f ~/.polycopy/logs/polycopy.log
```

Voir aussi [scripts/supervisor/launchd/README.md](../../scripts/supervisor/launchd/README.md).

## 8. Auto-start Windows + fallback

### 8.1 Task Scheduler — import XML

```powershell
# PowerShell — depuis le dossier du repo
cd scripts\supervisor\windows

# Substituer WIN_USER
$winUser = "$env:USERDOMAIN\$env:USERNAME"
(Get-Content polycopy-wsl-autostart.xml) -replace '\{\{WIN_USER\}\}', $winUser `
    | Set-Content polycopy-wsl-autostart.rendered.xml

# Import
schtasks /create /xml polycopy-wsl-autostart.rendered.xml `
    /tn "polycopy-wsl-autostart"

# Vérifier
schtasks /query /tn "polycopy-wsl-autostart"
```

### 8.2 Vérifier le boot WSL2 au logon

```powershell
# Log off puis log back in (ou reboot)
# Puis :
wsl -d Ubuntu systemctl --user status polycopy
# Doit montrer "active (running)"
```

### 8.3 Fallback PowerShell (WSL2 sans systemd)

Si WSL < 0.67.6 ou `/etc/wsl.conf` impossible à éditer, voir
[scripts/supervisor/windows/README.md](../../scripts/supervisor/windows/README.md) §Fallback.

## 9. Configuration `.env` par machine

Ajouter à chaque `.env` (permissions strictes 0o600) :

```bash
# === M12_bis : multi-machine & remote control ===
MACHINE_ID=PC-FIXE          # <-- unique par machine
MACHINE_EMOJI=🖥️           # <-- ou 💻 / 🏫

REMOTE_CONTROL_ENABLED=true
REMOTE_CONTROL_PORT=8765
REMOTE_CONTROL_TOTP_SECRET=JBSWY3DPEHPK3PXPJBSWY3DPEHPK3PXP  # <-- généré §5
# REMOTE_CONTROL_SENTINEL_PATH par défaut : ~/.polycopy/halt.flag
# REMOTE_CONTROL_ALLOWED_TAILSCALE_PEERS=  # vide = tous peers OK (perso tailnet)

# === Dashboard bind Tailscale (optionnel, recommandé en multi-machine) ===
DASHBOARD_ENABLED=true
DASHBOARD_BIND_TAILSCALE=true
# Si les deux ci-dessous : DASHBOARD_HOST est ignoré (warning au boot).
# DASHBOARD_HOST=127.0.0.1
# DASHBOARD_PORT=8787
```

Vérifier les permissions :

```bash
ls -l ~/code/polycopy/.env
# Attendu : -rw------- (0o600), owner = user
# Si pas : chmod 0600 ~/code/polycopy/.env
```

## 10. Bookmarks + Shortcuts téléphone

### 10.1 Bookmarks dashboard (navigateur phone)

Ajouter un bookmark par machine :

- `http://pc-fixe.<ton-tailnet>.ts.net:8787/`
- `http://macbook.<ton-tailnet>.ts.net:8787/`
- `http://uni-debian.<ton-tailnet>.ts.net:8787/`

Remplacer `<ton-tailnet>` par le nom du tailnet (visible dans
admin Tailscale, ex. `tailXXXX`).

### 10.2 iOS Shortcuts — recette par machine

Créer dans l'app Shortcuts (une shortcut par action par machine) :

**Exemple : "polycopy restart PC-FIXE"** :

1. `Ask for Input` → Type : Number → Prompt : "TOTP code".
2. `URL` → `http://pc-fixe.<tailnet>.ts.net:8765/v1/restart/PC-FIXE`.
3. `Get Contents of URL` :
   - Method : **POST**.
   - Headers : `Content-Type: application/json`.
   - Request Body : **JSON**.
   - Body : `{ "totp": "<Provided Input>" }` (Provided Input = variable
     de l'étape 1).
4. `Show Result` (ou `Show Notification`).

Dupliquer pour `/stop`, `/resume`, `/status` × 3 machines = 12 shortcuts.

Raccourci recommandé : créer un **Shortcut menu** au lieu de 12 séparés
(cf. Apple docs `Choose from Menu` action).

### 10.3 Android — HTTP Shortcuts

App recommandée : [HTTP Shortcuts](https://play.google.com/store/apps/details?id=ch.rmy.android.http_shortcuts) (FOSS).

Pour chaque action :
- URL, Method POST, Content-Type JSON.
- Body : `{"totp": "$TOTP"}` avec variable `$TOTP` de type **prompt
  à l'exécution**.

Alternative : Tasker (payant, plus flexible).

## 11. Smoke test end-to-end

Ordre recommandé, depuis le téléphone sur 4G (hors wifi maison) pour
valider la traversée NAT :

1. **`GET /v1/status/PC-FIXE`** (bookmark ou curl Shortcut).
   Attendu : `200` + JSON `{"mode": "running", ...}`.
2. **`POST /v1/restart/PC-FIXE`** avec un TOTP valide.
   Attendu : `202` + JSON `{"ok": true, "action": "restart", ...}`.
   Vérifier que `/v1/status/PC-FIXE` revient `running` après ~10s.
3. **`POST /v1/stop/PC-FIXE`** avec TOTP valide.
   Attendu : `202` + `respawn_mode: "paused"`.
   Vérifier : `/v1/status/PC-FIXE` → `"mode": "paused"`.
   Vérifier : **Telegram** reçoit l'alerte `paused_mode` avec badge
   `🖥️ *PC-FIXE*` ⏸️.
4. **`POST /v1/resume/PC-FIXE`** avec TOTP valide.
   Attendu : `202` + `respawn_mode: "running"`.
   Vérifier : `/v1/status/PC-FIXE` → `"mode": "running"`.
5. **Test TOTP invalide 3× consécutifs** :
   Envoyer 3× `POST /v1/stop/PC-FIXE` avec `"totp": "000000"`.
   Attendu : 401, 401, 423 (lockdown).
   Vérifier : **Telegram** reçoit `remote_control_brute_force_detected`
   CRITICAL. Recovery :
   ```bash
   # SSH sur la machine (Tailscale suffit)
   ssh user@pc-fixe
   rm ~/.polycopy/halt.flag
   systemctl --user restart polycopy
   ```
6. **Test dashboard distant** : ouvrir le bookmark dashboard sur phone
   → la page s'affiche, traders/PnL visibles.
7. **Répéter 1-6 pour MACBOOK et UNI-DEBIAN**.

## 12. Troubleshooting — 10 symptômes courants

| Symptôme | Cause probable | Solution |
|---|---|---|
| Boot crash `tailscale_not_installed` | Binaire absent du PATH | Install via §2.1-2.3. Vérifier `which tailscale`. |
| Boot crash `tailscale_timeout` | Daemon down | Linux : `sudo systemctl start tailscaled`. macOS : ouvrir app Tailscale. Win : lancer Tailscale app. |
| Boot crash `tailscale_no_ipv4` | Machine non-enrôlée au tailnet | `sudo tailscale up --hostname=<name>` + suivre lien auth. |
| Boot crash `tailscale_not_in_cgnat_range` | `REMOTE_CONTROL_TAILSCALE_IP_OVERRIDE` pointe hors `100.64.0.0/10` | Retirer l'override ou corriger la valeur. |
| Boot crash `REMOTE_CONTROL_TOTP_SECRET` missing | Flag on sans secret | Ajouter dans `.env` + §5. |
| TOTP refuse tous les codes | Clock skew phone/bot > 30s | Sync NTP : `sudo timedatectl set-ntp true` (Linux) ou `sudo sntp -sS time.apple.com` (macOS). Vérifier `date` des 2 côtés. |
| `/stop` fonctionne mais respawn en running | `halt.flag` créé mais chemin mauvais / permissions | `ls -la ~/.polycopy/halt.flag` doit être 0o600. Sinon `chmod 0600`. Vérifier `REMOTE_CONTROL_SENTINEL_PATH` non-overridé incohéremment. |
| Port 8765 déjà utilisé | Autre process squatte | `ss -tlnp \| grep 8765` → kill, ou `REMOTE_CONTROL_PORT=8766` dans `.env`. |
| Dashboard 404 depuis phone | `DASHBOARD_BIND_TAILSCALE=false` ou `DASHBOARD_ENABLED=false` | Passer les 2 à `true` + restart service. |
| `/resume` bloque avec 423 | Lockdown brute-force actif OU sentinel absent | Si 423 : SSH + `rm halt.flag` + `systemctl restart polycopy` (ou `--force-resume`). Si 409 `not_paused` : le bot tourne déjà. |

### Diagnostics utiles

```bash
# Linux / macOS / WSL2
~/.polycopy/logs/polycopy.log    # logs applicatifs M9 (rotation 10 MB × 10)
tail -f ~/.polycopy/logs/polycopy.log

# Status services
systemctl --user status polycopy              # Debian / WSL2
launchctl list | grep polycopy                # macOS
schtasks /query /tn "polycopy-wsl-autostart"  # Windows

# Filter events M12_bis spécifiques
grep -E 'remote_control|sentinel|halt.flag|machine_id_resolved' ~/.polycopy/logs/polycopy.log
```

---

_Guide rédigé dans le cadre de M12_bis Phase F — à raffiner au fil des
déploiements réels. Tout écart constaté → PR dans `docs/specs/`._
