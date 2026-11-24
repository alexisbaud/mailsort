# mailsort

Tri automatique des e-mails Gmail par Jev. La spécification complète est dans
[`Tri automatique des e-mails par jev.md`](Tri%20automatique%20des%20e-mails%20par%20jev.md).

## Développement

```bash
uv sync                  # installe les dépendances dans .venv
uv run mailsort --help   # liste les commandes
uv run pytest            # tests
uv run ruff check .      # lint
```

Les secrets (`.env`, `token.json`, `credentials.json`) restent hors du dépôt : voir `.env.example`.
