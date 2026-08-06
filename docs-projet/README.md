# Corpus de documentation du projet

Ce dossier est le corpus source du RAG interne. Les fichiers qui y sont déposés
sont découpés en chunks, vectorisés et interrogés quand un run active le RAG
(`use_rag: true`).

## Formats acceptés

- `.md`
- `.txt`
- `.py`
- `.rst`

## Utilisation

1. Déposer ici vos documents : architecture, référence API, connaissances
   métier, code existant.
2. Indexer le corpus :

   ```
   make index
   ```

   (équivalent à `python -m src.rag --index /app/docs-projet` dans le
   conteneur app).

3. Lancer un run avec le RAG activé (toggle "RAG" du web UI, `USE_RAG=1` avec
   `make run`, ou `use_rag: true` via l'API).

## Notes

- Réindexer après chaque modification (`make index`). L'insertion ne déduplique
  pas : pour un ré-index propre, vider la table au préalable (voir
  `docs/rag.md`).
- Le panneau et le lead ne voient jamais les chunks bruts, seul le coder les
  reçoit en contexte.
- RAG désactivé par défaut : sans corpus indexé, `search()` retourne `[]` et
  aucun contexte n'est injecté.
