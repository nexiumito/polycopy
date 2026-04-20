# launchd LaunchAgent — polycopy (M12_bis Phase F §4.8.2)

LaunchAgent : [`fr.polycopy.bot.plist`](./fr.polycopy.bot.plist).

## Pour qui

macOS récent (Monterey+ testé). User-scope uniquement — pas de
LaunchDaemon (pas besoin de root ni de tourner avant le login).

## Install (≈ 2 min)

```bash
# 1. Substituer les placeholders
sed -e "s|{{POLYCOPY_PATH}}|$HOME/code/polycopy|g" \
    -e "s|{{VENV_PATH}}|$HOME/code/polycopy/.venv|g" \
    fr.polycopy.bot.plist > ~/Library/LaunchAgents/fr.polycopy.bot.plist

# 2. Permissions (launchd refuse 0o666)
chmod 0644 ~/Library/LaunchAgents/fr.polycopy.bot.plist

# 3. Créer le dossier de logs launchd
mkdir -p ~/code/polycopy/logs

# 4. Load + start
launchctl load ~/Library/LaunchAgents/fr.polycopy.bot.plist

# 5. Vérifier
launchctl list | grep polycopy
# Output attendu : <PID>   0   fr.polycopy.bot
tail -f ~/code/polycopy/logs/launchd_stdout.log
tail -f ~/.polycopy/logs/polycopy.log
```

## Lifecycle attendu

| Commande HTTP (Tailscale) | Effet launchd |
|---|---|
| `POST /v1/restart/<machine>` | exit 0 → `KeepAlive=true` respawn après `ThrottleInterval=5`s, sentinel absent → **running** |
| `POST /v1/stop/<machine>`    | touch `halt.flag` → exit 0 → respawn → **paused** |
| `POST /v1/resume/<machine>`  | clear `halt.flag` → exit 0 → respawn → **running** |
| Kill switch drawdown         | touch `halt.flag` → exit 0 → respawn → **paused** |

## Recovery manuelle

```bash
# Force resume
rm ~/.polycopy/halt.flag
launchctl kickstart -k gui/$(id -u)/fr.polycopy.bot

# Ou via le flag CLI (après unload pour tester manuellement)
launchctl unload ~/Library/LaunchAgents/fr.polycopy.bot.plist
$HOME/code/polycopy/.venv/bin/python -m polycopy --force-resume --no-cli
# Puis reload pour supervision normale
launchctl load ~/Library/LaunchAgents/fr.polycopy.bot.plist
```

## Désinstall

```bash
launchctl unload ~/Library/LaunchAgents/fr.polycopy.bot.plist
rm ~/Library/LaunchAgents/fr.polycopy.bot.plist
```

## Notes macOS spécifiques

- **Tailscale dans `EnvironmentVariables.PATH`** : le binaire `tailscale`
  installé via `brew install tailscale` vit dans `/opt/homebrew/bin/`
  (Apple Silicon) ou `/usr/local/bin/` (Intel). Le PATH dans le plist
  couvre les 2.
- **Pas besoin de `Nice` ou `ProcessType`** : polycopy est léger,
  les defaults macOS suffisent.
- **Notification Center** : macOS peut afficher "polycopy quit
  unexpectedly" si le process crash au boot. Vérifier les logs
  `launchd_stderr.log` en premier.
