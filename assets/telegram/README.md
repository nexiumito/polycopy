# `assets/telegram/` — surcharges utilisateur des templates Telegram

Ce répertoire est **optionnel**. Il n'est livré vide (avec ce seul README) que pour documenter le mécanisme de surcharge des templates Markdown v2 du bot Telegram polycopy (M7).

## Comment ça marche

Au démarrage, `AlertRenderer` construit un `FileSystemLoader` Jinja2 avec deux chemins en cascade :

1. **`assets/telegram/`** — surcharges utilisateur (ce dossier).
2. **`src/polycopy/monitoring/templates/`** — templates défaut livrés avec polycopy.

Si un template existe dans `assets/telegram/` avec le **même nom de fichier** qu'un default, il **remplace** le default. Sinon, le default est utilisé.

## Exemple : personnaliser le message "kill switch"

```bash
# 1. Copier le default
mkdir -p assets/telegram/
cp src/polycopy/monitoring/templates/kill_switch_triggered.md.j2 assets/telegram/

# 2. Éditer à ton goût
$EDITOR assets/telegram/kill_switch_triggered.md.j2

# 3. Relancer polycopy — le template surchargé est utilisé automatiquement.
python -m polycopy --dry-run

# 4. Revenir au default : supprimer le fichier user-land.
rm assets/telegram/kill_switch_triggered.md.j2
```

## Templates surchargeable

Tous les fichiers `*.md.j2` listés dans `src/polycopy/monitoring/templates/` :

- `startup.md.j2`, `shutdown.md.j2`, `heartbeat.md.j2`
- `daily_summary.md.j2`, `digest.md.j2`
- `fallback.md.j2`
- `kill_switch_triggered.md.j2`, `executor_auth_fatal.md.j2`, `executor_error.md.j2`
- `pnl_snapshot_drawdown.md.j2`, `order_filled_large.md.j2`
- `trader_promoted.md.j2`, `trader_demoted.md.j2`
- `discovery_cap_reached.md.j2`, `discovery_cycle_failed.md.j2`

Les macros partagées (`partials/common_partials.md.j2`) sont également surchargeables si tu veux changer le rendu des lignes de wallet ou de modules.

## Règles de rédaction

- **Markdown v2** strict. Tous les caractères `_*[]()~\`>#+-=|{}.!` doivent être échappés avec `\` dans les parties statiques du template.
- **Variables** : utilise le filter `| telegram_md_escape` sur toute valeur user-controlled (slugs, labels, URLs). Exemple : `{{ pinned.wallet_short | telegram_md_escape }}`.
- **Filters disponibles** : `telegram_md_escape`, `wallet_short`, `format_usd_tg`, `humanize_dt_tg`.
- **`StrictUndefined`** : une variable absente du context **crash** le render explicitement (plutôt que rendre une chaîne vide silencieuse). Référence la spec ou le code `dtos.py` pour connaître les variables disponibles par template.
- **Longueur** : Telegram tronque à 4096 caractères. `AlertRenderer` log un warning et tronque avec `…` si dépassement.

## Versions & upgrades

Si tu surchargers un template puis mets à jour polycopy vers une nouvelle version mineure, **le context du template peut changer** (nouvelles variables, renommage). `StrictUndefined` fait crasher bruyamment — tu verras immédiatement le problème. Solution : re-copier le template par défaut, ré-appliquer tes ajustements.

Voir `specs/M7-telegram-enhanced.md` pour la liste exhaustive des variables par template.
