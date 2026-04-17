---
description: Implémenter un module en suivant l'architecture définie
---

Tu vas implémenter le module : $ARGUMENTS

Avant d'écrire du code :

1. Lis `CLAUDE.md` à la racine pour les conventions
2. Lis `docs/architecture.md` pour la spec du module
3. Vérifie les `__init__.py` existants pour comprendre ce qui est déjà en place
4. Si le module a des dépendances externes (Polymarket API), vérifie la doc officielle avant de coder, ne devine pas la structure des réponses

Implémente en suivant ces principes :
- Async par défaut, type hints stricts
- Pydantic v2 pour les DTOs
- Une classe = une responsabilité
- Pas d'abstraction prématurée
- Tests unitaires avec `respx` pour mocker les APIs HTTP

À la fin, fais tourner `ruff check`, `mypy src` et `pytest` et corrige tout ce qui pète.
