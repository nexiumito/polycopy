# Session C — Dashboard UX & data consistency

**Priorité** : 🟡 #3 (confort user, pas de perte capital direct)
**Charge estimée** : M (1-2 jours, beaucoup de petits fixes)
**Branche suggérée** : `feat/dashboard-ux-cleanup`

---

## Objectif business

Le dashboard est déjà bon (M6 refonte + M9 /logs + M13 cartes PnL) mais souffre
de **frictions quotidiennes** qui rendent l'audit du bot plus lent qu'il ne
devrait :

1. **Adresses tronquées partout** : impossible de copier un wallet pour faire
   une query SQL sans passer par la DB. Signalé par le user 2026-04-24 : "Je
   n'arrive pas à blacklist car je n'ai pas de moyen de récupérer l'adresse
   complète".
2. **`Size 0.00` systématique sur /activité** : les copy-trades font 0.001 à
   0.05 shares (copy_ratio 0.01), arrondis à 0.00 par le format → colonne
   inutile.
3. **Métriques biaisées** (ex `APPROVE STRATÉGIE 5.0 %`) parce que les
   compteurs `trade_detected` ne sont pas reset quand on reset les positions.
4. **Confusion PnL latent vs réalisé vs gain max** : pas de tooltip explicatif,
   l'user doit deviner la relation `total = initial + realized + latent`.
5. **Aucun moyen de fermer une position virtuelle manuellement** depuis le
   dashboard (utile en debug ou pour dégager un trade moisi manuellement).

## Items

### C1 — Bouton "copier" sur chaque adresse wallet

Partout où un wallet est affiché tronqué (`0xabcd…1234`), ajouter un petit
bouton copier (icon Lucide `copy`) ou un tooltip au hover qui révèle l'adresse
complète et permet la sélection/copie.

**Vues concernées** : `/home` (top trader + discovery panel), `/détection`,
`/stratégie`, `/exécution` (source + cond_id), `/positions` (cond_id),
`/pnl` (milestones), `/activité`, `/traders`, `/performance`, `/scoring v1/v2`,
`/logs` (si wallets mentionnés).

**Implémentation suggérée** : macro Jinja `render_wallet_address(address)` qui
produit `<span title="{full}" data-clipboard="{full}">{truncated}</span>` +
petit JS (HTMX ou vanilla 10 lignes) pour le clic → `navigator.clipboard.writeText`.

### C2 — Fix `Size 0.00` format sur /activité

[src/polycopy/dashboard/templates/activity.html](../../src/polycopy/dashboard/templates/) :
la colonne Size formate à 2 décimales → 0.00 pour toute size < 0.005.

**Fix simple** : formater à 4-5 décimales, ou notation scientifique si < 0.001
(ex `3.2e-3`), ou remplacer par un span avec tooltip full précision.

Jinja filter existant `format_size_precise` à créer dans
[src/polycopy/dashboard/jinja_filters.py](../../src/polycopy/dashboard/jinja_filters.py).

### C3 — Métrique `APPROVE STRATÉGIE` : base glissante ou reset

Le ratio `strategy_approve_rate_pct` sur /home utilise le compteur total all-time
de `strategy_decisions` / total `detected_trades`. Problème : après un reset
positions/capital, `detected_trades` n'est pas reset → le ratio reste faussement
bas pendant des jours.

**Options** :
- **A** : calculer le ratio sur une **fenêtre glissante 24 h** (cohérent avec les
  autres stats /home).
- **B** : un "reset metrics" CLI command qui reset tous les compteurs en même
  temps que le reset positions.

Option A est plus simple et plus utile.

### C4 — Fix arrondi `TOTAL USDC` vs somme composantes

User remarqué 2026-04-24 : /home affiche `TOTAL USDC $1006` alors que le calcul
exact `$1000 − $0.54 + $7.04 = $1006.50`. L'un ou l'autre est arrondi
différemment.

Standardiser sur `format_usd` avec 2 décimales partout (inclus `TOTAL USDC` qui
semble arrondir à l'unité aujourd'hui).

### C5 — Tooltips explicatifs sur les cartes KPI /home

Ajouter tooltip (`<span title="">` suffit) sur :
- **PnL réalisé** : "Gains/pertes cristallisés sur positions fermées"
- **PnL latent** : "Mark-to-market des positions ouvertes (mid_price − avg_buy) × size"
- **Gain max latent** : "Payout théorique si toutes les YES gagnent : (1 − avg_buy) × size"
- **Exposition** : "Capital engagé dans les positions ouvertes : avg_buy × size"
- **Drawdown** : "Chute depuis le plus haut historique"

Décharge mentale user énorme pour 5 lignes de code.

### C6 — Action "pinner/unpinner un wallet" depuis /traders

Actuellement on modifie `TARGET_WALLETS` dans `.env` et on redémarre. Proposition
d'un bouton (ou lien SSH command copiable) sur /traders qui génère la commande
exacte :

```bash
# Ajouter à TARGET_WALLETS :
echo "TARGET_WALLETS=...,0xABCD..." >> .env
# puis restart
```

**Variante plus ambitieuse** : un endpoint `POST /api/traders/<addr>/pin` qui
modifie la DB directement (puis `reconcile_blacklist`-style refresh). Sort du
scope "read-only dashboard" M4.5 — à peser. Peut rester hors scope v1.

### C7 — Filter recherche sur `/traders`, `/performance`

Les deux pages listent 50+ wallets. Ajouter un input de recherche côté client
(HTMX ou JS vanilla) pour filtrer par préfixe d'adresse / label. Déjà présent
sur /détection (input existant), à généraliser.

### C8 — Affichage `sell_without_position` reason dans /stratégie

Nouveau reason code M13 Bug 5 — apparaît dans les rejections mais sans distinction
visuelle des `liquidity_too_low`. Pas un bug, juste un ajout UX pour tracer
le nombre de SELL orphelins copiés.

## Hypothèses à valider

- **H1** : un tooltip au hover suffit (pas besoin d'un modal complet pour les
  explanations PnL).
- **H2** : la copie presse-papier via `navigator.clipboard` fonctionne bien sur
  les navigateurs desktop (cible user) — à tester sur Safari/Firefox si besoin.

## Livrables

- Nouvelles macros Jinja (`render_wallet_address`, `format_size_precise`).
- JS vanilla 20-30 lignes pour le copy button (pas de dépendance nouvelle).
- ~8-10 tests unit (format filters, template render, security grep non-régression).
- Mise à jour CLAUDE.md §Dashboard sur la nouvelle macro d'adresse.
- Pas de migration DB.

## Out of scope

- Pas d'endpoint write (pinner via API) — décision read-only M4.5 préservée.
- Pas de refonte visuelle majeure (Tailwind/Radix M6 conservés tels quels).
- Pas de mobile-first refactor (la page est déjà responsive M6).

## Success criteria

1. Sur n'importe quelle page qui liste des wallets, un clic (ou hover + ctrl+C)
   suffit à copier l'adresse complète.
2. /activité ne montre plus de `Size 0.00` si la taille réelle est > 0.
3. `APPROVE STRATÉGIE` sur /home reflète une fenêtre 24 h cohérente avec les
   autres stats.
4. Tous les chiffres USDC affichent 2 décimales et la somme `initial + realized +
   latent` matche `TOTAL USDC` au cent près.
