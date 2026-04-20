# systemd user unit — polycopy (M12_bis Phase F §4.8.1)

Unit : [`polycopy.service`](./polycopy.service).

## Pour qui

Linux (Debian 12 recommandé) + WSL2 Ubuntu si `systemd=true` activé dans
`/etc/wsl.conf` (cf. [WSL2 guide](../windows/README.md)).

## Install (≈ 2 min)

```bash
# 1. Substituer les placeholders {{POLYCOPY_PATH}} et {{VENV_PATH}}
sed -e "s|{{POLYCOPY_PATH}}|$HOME/code/polycopy|g" \
    -e "s|{{VENV_PATH}}|$HOME/code/polycopy/.venv|g" \
    polycopy.service > ~/.config/systemd/user/polycopy.service

# 2. Permettre au service de survivre à la déconnexion SSH
sudo loginctl enable-linger "$USER"

# 3. Enable + start
systemctl --user daemon-reload
systemctl --user enable --now polycopy

# 4. Vérifier
systemctl --user status polycopy
journalctl --user -u polycopy -n 50 -f
```

## Lifecycle attendu

| Commande HTTP (Tailscale) | Effet `systemd` |
|---|---|
| `POST /v1/restart/<machine>` | bot exit 0 → `Restart=always` respawn ≤ 5s, sentinel absent → **running** |
| `POST /v1/stop/<machine>`    | touch `halt.flag` → exit 0 → respawn → sentinel présent → **paused** |
| `POST /v1/resume/<machine>`  | clear `halt.flag` → exit 0 → respawn → **running** |
| Kill switch drawdown         | touch `halt.flag` → exit 0 → respawn → **paused** |

## Recovery manuelle

```bash
# Force resume (vide halt.flag avant respawn suivant)
rm ~/.polycopy/halt.flag
systemctl --user restart polycopy

# Ou, sans arrêt :
{{VENV_PATH}}/bin/python -m polycopy --force-resume --no-cli &
systemctl --user restart polycopy
```

## Désinstall

```bash
systemctl --user disable --now polycopy
rm ~/.config/systemd/user/polycopy.service
sudo loginctl disable-linger "$USER"
```
