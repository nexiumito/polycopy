# M12_bis — Setup guide utilisateur (Tailscale + Remote Control)

> **[à compléter en fin d'implémentation phase F]**
>
> Ce guide est le livrable final de M12_bis. Les sections ci-dessous sont le squelette validé en phase de spec. Chaque section sera remplie par l'implémenteur avec les **commandes réellement testées** sur au moins une des 3 machines cibles (Debian 12 / macOS / Windows 10 + WSL2).
>
> Audience : Elie (user), qui suivra ce guide pas-à-pas pour activer le remote control sur ses 3 machines.

---

## 1. Prérequis

- [ ] Compte Tailscale (free tier, sign-up Google/GitHub/email sur https://tailscale.com).
- [ ] Téléphone iOS ou Android avec app Tailscale installée + app Authenticator (Google Authenticator, 1Password, Bitwarden).
- [ ] Accès admin/root sur les 3 machines cibles.
- [ ] Polycopy installé en M12_bis sur chaque machine (git pull + `scripts/setup.sh`).
- [ ] **Windows 10 uniquement** : WSL2 à jour (`wsl --update` ≥ 0.67.6 pour support systemd).

## 2. Installation Tailscale par OS

### 2.1 Debian 12 (machine université)

_[à compléter : commandes `curl -fsSL https://tailscale.com/install.sh | sh` + `sudo tailscale up --hostname=uni-debian` + vérif `tailscale ip -4`]_

### 2.2 macOS (MacBook)

_[à compléter : `brew install tailscale` OU App Store + `sudo tailscale up --hostname=macbook`]_

### 2.3 Windows 10 + WSL2

_[à compléter : Tailscale app Store Windows + subtilité tailscaled WSL vs host — recommandation finale après test]_

### 2.4 Téléphone

_[à compléter : Tailscale app + login + vérifier les 3 machines visibles]_

## 3. Enrôlement tailnet + alignement hostnames/MACHINE_ID

_[à compléter : table `MACHINE_ID` env var ↔ hostname Tailscale ↔ emoji suggéré, ex :]_

| Machine | `MACHINE_ID` (.env) | Tailscale hostname | `MACHINE_EMOJI` |
|---|---|---|---|
| PC fixe maison | `PC-FIXE` | `pc-fixe` | `🖥️` |
| MacBook portable | `MACBOOK` | `macbook` | `💻` |
| PC université | `UNI-DEBIAN` | `uni-debian` | `🏫` |

_[à compléter : commandes `tailscale up --hostname=<x>` + vérif MagicDNS (`ping pc-fixe.<tailnet>.ts.net`)]_

## 4. Activation WSL2 systemd (Windows 10 uniquement)

_[à compléter : `wsl --update` ; `sudo nano /etc/wsl.conf` + bloc `[boot]\nsystemd=true` ; `wsl --shutdown` ; vérif `systemctl status` après reboot]_

Fallback si systemd indisponible : voir §8.3 pour `polycopy-wsl-respawn.ps1`.

## 5. Génération du secret TOTP

_[à compléter : `python -c "import pyotp; print(pyotp.random_base32())"` → copier le secret dans `.env` comme `REMOTE_CONTROL_TOTP_SECRET=...` ; `python -c "import pyotp, qrcode; url = pyotp.TOTP('<SECRET>').provisioning_uri('polycopy-PC-FIXE', issuer_name='polycopy'); qrcode.make(url).save('totp.png'); print(url)"` → scan QR dans Google Authenticator → vérifier avec un `pyotp.TOTP(secret).now()` en Python REPL]_

**⚠️ Sécurité** :
- Ne jamais committer `.env`.
- Secret différent par machine (une entrée Authenticator par machine, nommée `polycopy-<MACHINE_ID>`).
- Rotation trimestrielle : regénérer + replacer dans `.env` + rescanner QR.

## 6. Installation unit systemd (Debian 12 + WSL2)

_[à compléter : `cp scripts/supervisor/systemd/polycopy.service ~/.config/systemd/user/` + sed `s/{{USER}}/elie/g` etc. + `loginctl enable-linger $USER` + `systemctl --user daemon-reload` + `systemctl --user enable --now polycopy` + vérif `systemctl --user status polycopy`]_

## 7. Installation LaunchAgent (macOS)

_[à compléter : `cp scripts/supervisor/launchd/fr.polycopy.bot.plist ~/Library/LaunchAgents/` + sed substitutions + `launchctl load ~/Library/LaunchAgents/fr.polycopy.bot.plist` + vérif `launchctl list | grep polycopy`]_

## 8. Auto-start Windows + fallback

### 8.1 Task Scheduler — import XML

_[à compléter : importer `scripts/supervisor/windows/polycopy-wsl-autostart.xml` via `schtasks /create /xml ... /tn "polycopy-wsl-autostart"` + screenshot de la task dans l'UI]_

### 8.2 Vérifier boot WSL2 au logon

_[à compléter : logout/login → vérifier que WSL2 est up automatiquement → `wsl.exe -d Ubuntu systemctl --user status polycopy`]_

### 8.3 Fallback PowerShell (si WSL2 sans systemd)

_[à compléter : configurer Task Scheduler action `powershell.exe -File <path>\polycopy-wsl-respawn.ps1 -WindowStyle Hidden` au lieu du XML ci-dessus]_

## 9. Configuration `.env` par machine

_[à compléter : bloc exemple final avec toutes les env vars M12_bis :]_

```
# === M12_bis : multi-machine & remote control ===
MACHINE_ID=PC-FIXE
MACHINE_EMOJI=🖥️

REMOTE_CONTROL_ENABLED=true
REMOTE_CONTROL_PORT=8765
REMOTE_CONTROL_TOTP_SECRET=<base32-32chars-généré-en-§5>
# REMOTE_CONTROL_ALLOWED_TAILSCALE_PEERS=  # vide = tous peers tailnet OK

DASHBOARD_ENABLED=true
DASHBOARD_BIND_TAILSCALE=true
# DASHBOARD_HOST=127.0.0.1  # ignoré si DASHBOARD_BIND_TAILSCALE=true
```

_[à compléter : check permissions `.env` 0o600]_

## 10. Bookmarks + Shortcuts téléphone

### 10.1 Bookmarks dashboard (navigateur phone)

_[à compléter : URL pattern `http://<tailscale-hostname>.<tailnet>.ts.net:8000/` par machine]_

### 10.2 iOS Shortcuts — 4 commandes par machine

_[à compléter : screenshots du Shortcut "Restart PC-FIXE" avec :]_
- Action 1 : **Ask for Input** → Number → prompt "TOTP code"
- Action 2 : **Get Contents of URL** → POST `http://pc-fixe.<tailnet>.ts.net:8765/v1/restart/PC-FIXE`
  - Headers : `Content-Type: application/json`
  - Request Body : JSON `{"totp": "<input>"}`
- Action 3 : **Show Result**

Répéter pour `/stop`, `/resume`, `/status` × 3 machines = 12 Shortcuts (ou 1 Shortcut menu unique).

### 10.3 Android — Tasker ou HTTP Shortcuts

_[à compléter : recette équivalente via app "HTTP Shortcuts" (FOSS Play Store) — plus simple que Tasker pour ce use-case]_

## 11. Smoke test end-to-end

_[à compléter : protocole test depuis phone, dans l'ordre :]_

1. **`/status/PC-FIXE`** (phone → Tailscale → bot) → attend JSON `{"mode": "running", ...}`.
2. **`/restart/PC-FIXE`** avec TOTP valide → 202 + attendre 10s → `/status` redevient 200 en mode running.
3. **`/stop/PC-FIXE`** avec TOTP → 202 → `/status` revient en mode paused + check Telegram reçoit alerte `paused_mode_entered` avec badge `🖥️ *PC-FIXE*`.
4. **`/resume/PC-FIXE`** avec TOTP → 202 → mode running à nouveau.
5. Tester **TOTP invalide** 3× consécutifs → 423 Locked + check Telegram reçoit `remote_control_brute_force_detected` CRITICAL + recover via `--force-resume` CLI.
6. Tester **dashboard distant** : browser phone sur `http://pc-fixe.<tailnet>.ts.net:8000/` → dashboard s'affiche, traders visibles.

## 12. Troubleshooting (10 symptômes courants)

_[à compléter avec les 10 issues rencontrées réellement pendant le setup phases B-F. Gabarit :]_

| Symptôme | Cause probable | Solution |
|---|---|---|
| Boot crash `tailscale_not_running` | Tailscale daemon pas up | `sudo systemctl start tailscaled` (Linux) / `sudo launchctl load /Library/LaunchDaemons/com.tailscale.tailscaled.plist` (macOS) |
| TOTP refuse tous les codes | Clock skew phone/bot > 30s | Sync NTP manuel : `sudo timedatectl set-ntp true` |
| `/stop` fonctionne mais respawn en running | Sentinel path mauvais / permissions | Check `ls -la ~/.polycopy/halt.flag` + perm 0o600 |
| Port 8765 déjà utilisé | Autre process squatte | `ss -tlnp \| grep 8765` → kill, ou changer `REMOTE_CONTROL_PORT` |
| Dashboard 404 depuis phone | `DASHBOARD_BIND_TAILSCALE=false` | Passer à `true` + restart service |
| _[à compléter au fur et à mesure]_ | _..._ | _..._ |

---

_Guide finalisé après implémentation complète phase F. Version draft — 2026-04-20._
