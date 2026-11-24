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

## Secrets

Le dépôt ne contient que des modèles `*.example`. Pour chacun, dupliquer le fichier sans le
suffixe `.example` et le remplir. Les fichiers réels sont ignorés par git.

| Modèle | Fichier réel | Contenu |
| --- | --- | --- |
| `.env.example` | `.env` | Clé Jev, version de Jev, chemins, adresses de l'utilisateur |
| `credentials.json.example` | `credentials.json` | Client OAuth Google (téléchargé depuis la console) |
| — | `token.json` | Jeton Gmail, écrit par `mailsort auth` (droits 600) |

## Configurer l'accès Gmail (une seule fois)

Dans la [console Google Cloud](https://console.cloud.google.com/) :

1. **Créer un projet** (par exemple « mailsort ») et le sélectionner.
2. **Activer l'API Gmail** : APIs & Services → Library → « Gmail API » → Enable.
3. **Écran de consentement** : Google Auth Platform → Get started. Nom de l'app « mailsort »,
   ton adresse comme e-mail d'assistance et de contact, audience **External**.
4. **Passer l'app en production** : Google Auth Platform → Audience → **Publish app**, puis
   confirmer. En statut « Testing », le jeton expirerait au bout de 7 jours. Aucune
   vérification Google n'est nécessaire pour un usage personnel.
5. **Créer le client OAuth** : Google Auth Platform → Clients → Create client → type
   **Desktop app** → Create → **Download JSON**. Enregistrer le fichier à la racine du projet
   sous le nom `credentials.json` (modèle : `credentials.json.example`).

Puis, sur le Mac :

```bash
uv run mailsort auth
```

Le navigateur s'ouvre. Choisir le compte Gmail, puis sur l'écran « Google hasn't verified this
app » : Advanced → Go to mailsort (unsafe). C'est attendu : l'app est la tienne. Accepter le
droit de gérer les mails (`gmail.modify` : labels et lecture, sans suppression définitive).

Vérifier ensuite l'accès, en lecture seule :

```bash
uv run mailsort messages --limit 5
uv run mailsort explain <identifiant> --text-only
```
