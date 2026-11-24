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
4. **Compléter Branding** (sinon « Publish app » reste grisé avec le message « complete your
   configuration on the Branding page ») : Google Auth Platform → Branding.
   - Application home page : `https://alexisbaud.github.io/mailsort/`
   - Application privacy policy link : `https://alexisbaud.github.io/mailsort/privacy.html`
   - Authorized domains : `alexisbaud.github.io`

   Ces deux pages sont dans [`docs/`](docs/), publiées par GitHub Pages depuis la branche
   `main`. Aucune vérification Google n'est faite sur ces liens : il faut seulement qu'ils
   existent.
5. **Passer l'app en production** : Google Auth Platform → Audience → **Publish app**, puis
   confirmer. En statut « Testing », le jeton expirerait au bout de 7 jours. Aucune
   vérification Google n'est nécessaire pour un usage personnel.
6. **Créer le client OAuth** : Google Auth Platform → Clients → Create client → type
   **Desktop app** → Create → **Download JSON**. Enregistrer le fichier à la racine du projet
   sous le nom `credentials.json` (modèle : `credentials.json.example`).

Puis, sur le Mac, lancer un passage à blanc. Il ne modifie rien dans Gmail : il affiche
combien de mails iraient dans chaque label, combien n'en auraient aucun, et le total trié
(détail par mail dans un CSV). Par défaut sur les 200 derniers mails ; `--all` pour toute la
boîte, `--limit N` ou `--query "…"` pour un autre périmètre.

```bash
uv run mailsort run --dry-run
```

La première fois, faute de jeton, le navigateur s'ouvre (on peut aussi le faire à part avec
`uv run mailsort auth`). Choisir le compte Gmail, puis sur l'écran « Google hasn't verified this
app » : Advanced → Go to mailsort (unsafe). C'est attendu : l'app est la tienne. Accepter le
droit de gérer les mails (`gmail.modify` : labels et lecture, sans suppression définitive).

Vérifier ensuite l'accès, en lecture seule :

```bash
uv run mailsort messages --limit 5
uv run mailsort explain <identifiant> --text-only
```
