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

Il liste aussi chaque mail qui irait dans 🤔 To Review, avec ses scores et la raison, puis compte
les mails par raison (`--no-review` masque la liste). Les réponses de Jev sont gardées dans
`mailsort.db` : après un changement de seuil dans `labels.yaml`, relancer la commande ne rappelle
pas Jev et ne coûte rien.

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

## Trier pour de vrai

```bash
uv run mailsort run                     # toute la boîte : nouveaux mails, labels modifiés, corrections
uv run mailsort run -q "newer_than:7d"  # un petit lot, pour tester
uv run mailsort run --csv               # écrit aussi run-….csv : mails modifiés, labels ajoutés et retirés
```

Le premier passage crée les labels Gmail (`Types/…`, `Domains/…`, `Actions/…`) avec leur couleur
et leur visibilité. Ensuite, chaque passage :

- renomme dans Gmail un label renommé dans `labels.yaml` (les mails le gardent) ;
- repère tes corrections : un label que tu retires d'un mail n'y est plus jamais reposé, un label
  que tu ajoutes y reste (`uv run mailsort corrections` les liste) ;
- retire ✉️ To Reply quand le dernier message du fil vient de toi ;
- ne touche jamais à tes propres labels ni à un label qu'il n'a pas posé.

Un seul passage tourne à la fois (verrou `mailsort.db.lock`). Le bilan et les erreurs vont dans
`logs/mailsort.log` (identifiants de mails seulement, jamais leur contenu).

**Limites de Gmail et pannes.** Le programme étale ses appels sous un budget d'unités de quota
par minute (`GMAIL_QUOTA_UNITS_PER_MINUTE`, 6 000 par défaut). Si Gmail demande quand même de
ralentir, renvoie une erreur serveur ou si le réseau coupe, l'appel est réessayé avec un délai
croissant (jusqu'à environ 2 minutes). Un mail qui échoue quand même est repris au passage
suivant. Le passage s'arrête (code de sortie 2) si le quota du jour est épuisé ou après 5 erreurs
d'affilée, et immédiatement (code 1) si l'accès Gmail ou Jev est refusé.

| Commande | Effet |
| --- | --- |
| `mailsort resort --all` | Repose toutes les questions à Jev, en gardant tes corrections |
| `mailsort eval [--compare "Label Gmail=id"]` | Compare le classement à un label Gmail existant, sans rien modifier |
| `mailsort corrections` | Liste tes corrections par label, avec la probabilité donnée par Jev |
| `mailsort labels delete <id> --confirm` | Supprime de Gmail un label retiré de `labels.yaml` |

## Image Docker (pour le VPS)

```bash
docker build -t mailsort .                     # construit l'image (aucun secret dedans)
docker run --rm mailsort labels check          # vérifie labels.yaml dans l'image
```

Sur le VPS, l'image est lancée par `deploy/compose.yml` (copié dans `/srv/mailsort/`), avec les
secrets dans `/srv/mailsort/.env` (modèle `deploy/.env.example`) et les données dans
`/srv/mailsort/data/` (`token.json`, `mailsort.db`, `logs/`). Détail dans la spec, section
« Déploiement sur le VPS ».
