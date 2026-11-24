# Tri automatique des e-mails par jev

Nov 24, 2026 · @Alexis

Un programme hébergé sur le VPS lit les nouveaux e-mails Gmail 3 fois par jour, demande à Jev quels labels s'appliquent, puis pose ces labels. Il ne supprime jamais de mail. Spécification v1 à commenter avant de la confier à Claude Code.

## Décisions prises

Tout le tri passe par Jev : les filtres Gmail actuels seront supprimés une fois le programme en service.

| Sujet | Décision |
| --- | --- |
| Moteur de tri | Jev, en mode hybride : un choix unique pour les types, une question oui/non pour chaque autre label |
| Langage | Python |
| Développement | Avec Claude Code, testé en local avant le déploiement sur le VPS |
| Hébergement | Le VPS d'Alexis, 3 passages par jour |
| Périmètre | Toute la boîte, historique compris |
| Changement de label | Retri de tous les mails |
| Création des labels Gmail | Faite par le programme, pas à la main |
| Noms des labels | En anglais, au pluriel, avec un emoji, rangés en sous-labels par famille |
| Descriptions des labels | En anglais, génériques, sans aucune marque ni organisme cité |
| Plusieurs labels par mail | Oui : 1 ou 2 types (le 2e par un second passage sans le type gagnant), et autant de domaines que nécessaire |
| ✉️ To Reply | Demandes de réponse et demandes d'action, qu'elles viennent d'une personne ou d'un organisme |
| 🗑️ Deletable | Label seulement : Alexis supprime lui-même par lots |
| Texte envoyé à Jev | Nettoyé, puis limité à 5 000 tokens |
| Modifier les labels | Éditer `labels.yaml` avec Claude Code ; une interface web en v2 |
| Accès Jev | API directe de TypeSafe (endpoint System One), clé dans `.env` ; version épinglée (`jev-1.14.0`) |
| Secrets | Le dépôt ne contient que des modèles `*.example` ; Alexis les duplique sans le suffixe et les remplit |
| Accès Gmail | OAuth, avec l'app Google publiée en « In production » ; jeton créé sur le Mac par `mailsort auth`, puis copié sur le VPS |
| Bilan des passages | Le journal sur le VPS uniquement, erreurs comprises ; aucune notification envoyée |
| Suppression de mails | Jamais par le programme |

## Fonctionnement

Un cron lance le programme à 8h30, 12h30 et 18h (heure de Paris). Un verrou empêche deux passages de tourner en même temps.

1. **Synchroniser les labels.** Le programme lit `labels.yaml`, crée les labels Gmail manquants, y compris les labels parents, et renomme ceux qui ont changé de nom. Il garde le lien entre chaque label du fichier et l'identifiant du label Gmail, donc un renommage conserve les mails déjà classés.
2. **Repérer les changements de config.** Deux sortes d'empreintes sont comparées à celles du dernier passage :
   - l'**empreinte de question** (nom et description) : si elle change, la question est reposée à Jev sur tous les mails. Les types forment une seule question, donc ils partagent une seule empreinte, qui couvre aussi la description de « None of the above » ;
   - l'**empreinte des règles** (seuils, `enabled`, `never_with`, `protects_from`) : si elle change, les règles sont recalculées sur tous les mails à partir des probabilités enregistrées, sans appeler Jev.
3. **Lister les mails à traiter.** Ce sont les messages jamais traités, plus tous les messages si un label est à re-évaluer. Le programme ignore les spams, la corbeille, les brouillons et les messages envoyés par Alexis.
4. **Préparer chaque mail.** Il extrait l'expéditeur, les destinataires, l'objet, la date, les en-têtes de liste de diffusion, les noms des pièces jointes et le texte nettoyé et tronqué (voir la section sur les données envoyées).
5. **Interroger Jev.** Un appel par mail : le mail est l'« état ». Les types forment une seule question à choix (question `choice` : les 7 types plus « None of the above ») ; chaque autre label est une question oui/non (question `noul`) construite à partir de sa description. Jev renvoie une probabilité par option et par label. Un second appel, limité aux types, peut suivre pour chercher un type secondaire (section Règles de décision).
6. **Appliquer les règles.** Le code applique les seuils, les exclusions et les protections (section Règles de décision).
7. **Mettre à jour Gmail.** Le programme ajoute les labels gagnés et retire ceux qu'il avait posés et qui ne s'appliquent plus. Il ne touche jamais un label qu'Alexis a corrigé à la main.
8. **Enregistrer.** Une base SQLite garde, pour chaque message, les probabilités de chaque question avec son empreinte, les labels posés par le programme et l'empreinte des règles utilisée. Elle garde aussi les verrous, l'historique des corrections, le lien entre chaque label et son label Gmail, et le bilan des passages. Chaque message est enregistré d'un bloc (transaction) : un passage interrompu ne laisse pas de message à moitié traité. Le schéma est versionné, et une nouvelle version du programme met la base à jour au démarrage.
9. **Faire le bilan.** Le passage écrit un journal : nombre de mails traités, nombre par label, nombre de 🤔 To Review, erreurs éventuelles. Aucune notification n'est envoyée.

**Erreurs d'accès.** Si Gmail refuse l'accès (jeton expiré ou révoqué, erreur `invalid_grant`) ou si Jev refuse la clé, le passage s'arrête sans rien modifier et écrit dans le journal une ligne `ERROR` explicite avec la cause et la marche à suivre, par exemple : « Accès Gmail refusé : jeton expiré ou révoqué. Lancer `mailsort auth` sur le Mac, puis copier `token.json` sur le VPS. » Chaque passage suivant réécrit cette ligne tant que l'accès n'est pas rétabli.

Les labels Gmail s'appliquent message par message : dans la boîte, un fil affiche les labels de tous ses messages.

## Les labels

18 labels en 5 familles, rangés dans Gmail sous 3 labels parents : Types, Domains et Actions (par exemple « Types/💳 Payments » ; To Reply, Deletable et To Review vont sous Actions). Les types disent ce qu'est le mail, les domaines à quelle partie de la vie il se rapporte, l'action ce qu'il faut en faire. Les types forment une seule question à choix, avec une option « None of the above » décrite ainsi : « None of the other types applies. This email is not a proof of payment or refund, not the tracking of a shipment, return or download, not a ticket or booking, not an official or contractual document, not an account or security notification, not a newsletter, digest or changelog, and not a promotional or marketing message. » Les autres labels sont des questions oui/non, donc un mail peut cumuler plusieurs domaines.

Les descriptions sont en anglais, car c'est ce que Jev lira. Elles décrivent des catégories, jamais une marque, un site ou un organisme.

| Label | Famille | Couleur | Description envoyée à Jev |
| --- | --- | --- | --- |
| 💳 Payments | Type | `#17a765` | Proof that the user paid for something or was refunded: invoices, receipts, paid order confirmations (including free orders), payment confirmations, rent receipts, charged subscription renewals, refunds. Not: payment reminders before paying, failed payments, quotes, price change announcements, money received by the user. |
| 📦 Deliveries | Type | `#ff7537` | Tracking of something the user ordered or obtained until it reaches them: shipping, tracking, delivery, pickup points, returns and exchanges, a device sent for repair, a file or resource made available for download, even if free. |
| 🎟️ Tickets & Bookings | Type | `#7858c2` | An access ticket or reservation tied to a date or place: transport tickets, show or event tickets, boarding passes, accommodation, restaurant or appointment bookings, event registrations, and changes or cancellations to them. |
| 📄 Official Documents | Type | `#1e53b8` | A document with administrative, contractual or legal value worth keeping: contracts, certificates, attestations, payslips, tax notices, official statements, summons, electronically signed documents. |
| 🔐 Accounts & Security | Type | `#757575` | Management of one of the user's online accounts: account creation, email verification, login or verification codes, password resets, sign-in or new device alerts, two-factor authentication, account detail changes, updates to terms of use or privacy policies. |
| 📰 Newsletters & Digests | Type | `#007286` | Informational content sent regularly by subscription: newsletters, digests, summaries, release notes, changelogs, announcements of new features in a product the user uses, activity recaps. Not: messages whose main goal is to sell. |
| 📣 Marketing | Type | `#c2175b` | A commercial message trying to make the user buy, consume or come back: promotions, sales, discount codes, offers, new products, abandoned carts, recommendations, referral programs, review requests, satisfaction surveys. |
| 🏠 Housing | Domaine | `#ffc8af` | The user's home: renting, rent, landlord or agency, flatsharing, service charges, energy, water, internet access, home insurance, repairs, moving, housing benefits. |
| 🏛️ Administration | Domaine | `#ebdbde` | Formalities with public administrations and public bodies: taxes, identity documents, civil status, social security, benefits, vehicles, fines, justice. |
| 🩺 Health | Domaine | `#fcd2e0` | The user's physical and mental health: medical appointments, prescriptions, test results, healthcare reimbursements, health insurance, pharmacy. |
| 🏦 Banking & Investments | Domaine | `#b4efd3` | The user's money: bank accounts, cards, transfers, direct debits, bank fees, savings, investments, loans, cryptocurrencies. |
| 💼 Work | Domaine | `#b6cff5` | The user's professional life, as an employee or self-employed: employer, colleagues, recruiting, job applications, employment contracts, payroll, work tools, clients, prospects, quotes and invoices sent by the user, freelance activity and status, professional network. |
| 🎓 Studies & Training | Domaine | `#e3d7ff` | Learning: school, university, the school side of a work-study program, courses, exams, diplomas, online training, certifications, alumni. |
| ✈️ Travels | Domaine | `#99d7e4` | Trips outside daily life: long-distance transport, accommodation, stays, visas and travel formalities, tax-free shopping refunds, car rental, travel insurance, activities at the destination. |
| 👨‍👩‍👧 Family & Friends | Domaine | `#fbe983` | Personal exchanges with family, friends, a partner or acquaintances: messages written by someone close to the user, emails forwarded by them, personal invitations, group money pots between friends. |
| ✉️ To Reply | Action | `#b1b1b1` | A message that expects an answer or a specific action from the user: a person asking a question, making a request or waiting for a confirmation, or an organization asking for a document or an action before a deadline. Not: automatic notifications that require nothing. |
| 🗑️ Deletable | Nettoyage | `#d93025` | A message with no value once read or once its date has passed: advertising, social network notifications, generic reminders, expired offers, mailing list subscription confirmations, surveys. |
| 🤔 To Review | Technique | `#ffae47` | Posé par le code, pas par Jev (voir Règles de décision). |

## Règles de décision

Jev donne une probabilité entre 0 et 1 pour chaque label ; le code décide ensuite, avec les règles ci-dessous, dans cet ordre.

1. **Type principal.** Jev choisit une option parmi les 7 types et « None of the above ». Le type gagnant est posé si sa probabilité atteint 0,60. Si « None of the above » gagne, le mail n'a pas de type.
2. **Type secondaire, par un second passage.** Si un autre type a obtenu au moins 0,30 au premier passage, le programme repose la question des types à Jev, sans le type gagnant. La consigne précise : « This email has already been classified as \<type gagnant>. Is it also one of the following? ». Si Jev choisit alors ce même type avec au moins 0,70, il est posé en plus. S'il choisit « None of the above » ou un autre type, rien n'est ajouté. Un mail a au plus 2 types.
3. **Paires interdites.** Le type secondaire n'est jamais posé s'il forme l'une de ces paires avec le type principal :
   - 💳 Payments et 📦 Deliveries ;
   - 📰 Newsletters & Digests et 📣 Marketing.
4. **Autres labels.** Domaines, ✉️ To Reply et 🗑️ Deletable sont posés si leur probabilité oui/non atteint 0,80. Chaque label peut avoir son propre seuil dans `labels.yaml`.
5. **Protections.** 🗑️ Deletable n'est jamais posé si le mail a aussi 💳 Payments, 📄 Official Documents, 🎟️ Tickets & Bookings, 👨‍👩‍👧 Family & Friends ou ✉️ To Reply.
6. **Déjà répondu.** ✉️ To Reply est retiré dès que le dernier message du fil vient d'Alexis. Le programme le vérifie à chaque passage, sans appeler Jev.
7. **Zone de doute.** Le mail reçoit 🤔 To Review si le type gagnant est sous 0,60, ou si une question oui/non tombe entre 0,50 et 0,80. Le label douteux n'est alors pas posé.
8. **Mail sans repère.** Si un mail ne reçoit ni type, ni domaine, ni 🗑️ Deletable, il reçoit 🤔 To Review.

🤔 To Review disparaît dès qu'Alexis pose ou retire à la main un label de la famille concernée : sa correction tranche.

**Précisions appliquées par le code :**

- si « None of the above » gagne avec moins de 0,60, le mail reçoit aussi 🤔 To Review : le choix de type est incertain ;
- en cas d'égalité entre deux options, la première dans l'ordre de `labels.yaml` gagne ;
- les paires interdites valent dans les deux sens. Le second passage n'est pas demandé si le type candidat forme une paire interdite avec le gagnant : il ne pourrait pas être posé, l'appel serait inutile ;
- le type secondaire ne crée jamais de doute : sous 0,70, il est simplement ignoré ;
- un doute sur un label bloqué par une protection, ou sur ✉️ To Reply alors qu'Alexis a déjà répondu, n'en est pas un ;
- une fois ✉️ To Reply retiré par la règle 6, il ne protège plus de 🗑️ Deletable ;
- les corrections manuelles passent avant les protections : un label posé à la main n'est jamais retiré par une protection, et il protège comme un label posé par le programme ;
- ✉️ To Reply ne compte pas comme repère pour la règle 8 ;
- retirer à la main 🤔 To Review le verrouille « absent » : il n'est plus jamais reposé sur ce mail.

## Configuration des labels

Tous les labels vivent dans un seul fichier, `labels.yaml`. Alexis le modifie avec Claude Code, puis l'envoie sur le VPS. Le programme crée lui-même les labels Gmail : rien à créer à la main.

```yaml
settings:
  types:
    primary_min: 0.60        # seuil du type principal
    secondary_trigger: 0.30  # déclenche le second passage
    secondary_min: 0.70      # seuil du type secondaire
    none_description: >
      None of the other types applies. This email is not a proof of payment
      or refund, not the tracking of a shipment, return or download, not a
      ticket or booking, not an official or contractual document, not an
      account or security notification, not a newsletter, digest or
      changelog, and not a promotional or marketing message.
  yes_no_min: 0.80           # seuil par défaut des questions oui/non
  doubt_min: 0.50            # en dessous : non ; entre les deux : To Review
  parents:                   # label parent Gmail de chaque famille
    type: Types
    domain: Domains
    action: Actions
    cleanup: Actions
    system: Actions
  visibility:                # appliquée seulement à la création du label
    type:    {label_list: show,           message_list: show}
    domain:  {label_list: show_if_unread, message_list: show}
    action:  {label_list: show,           message_list: hide}
    cleanup: {label_list: show,           message_list: hide}
    system:  {label_list: show_if_unread, message_list: hide}

labels:
  - id: payments             # identifiant stable, ne change jamais
    name: "💳 Payments"        # dans Gmail : Types/💳 Payments
    family: type             # type | domain | action | cleanup | system
    color: "#17a765"         # appliquée seulement à la création du label
    enabled: true
    never_with: [deliveries] # optionnel : paire interdite
    protects_from: [deletable] # optionnel : bloque ces labels
    description: >
      Proof that the user paid for something or was refunded: ...
```

| Champ | Rôle |
| --- | --- |
| `settings.types` | Seuils du choix de type et description de « None of the above » |
| `settings.yes_no_min`, `doubt_min` | Seuils par défaut des questions oui/non |
| `settings.visibility` | Visibilité par famille, appliquée seulement à la création du label |
| `id` | Lien permanent entre le fichier, la base et le label Gmail |
| `name` | Nom du sous-label, rangé sous le parent de sa famille (`settings.parents`) ; le changer renomme le label sans perdre les mails classés |
| `family` | `type` = une option du choix de type ; `domain`, `action`, `cleanup` = une question oui/non ; `system` = posé par le code (🤔 To Review) |
| `color` | Couleur de fond en hexadécimal, appliquée seulement à la création du label |
| `enabled` | `false` = le label n'est plus évalué, mais reste dans Gmail |
| `threshold` | Seuil propre à un label oui/non (optionnel) |
| `never_with` | Types qui ne peuvent pas être type secondaire avec celui-ci |
| `protects_from` | Labels interdits quand celui-ci est posé |
| `description` | Texte envoyé à Jev, en anglais et générique |

**Ajouter un label** : ajouter une entrée avec un nouvel `id`. Au passage suivant, le programme crée le label Gmail et pose seulement cette question sur tous les mails. Pour un nouveau type, c'est tout le choix de type qui est reposé.

**Modifier un label** : changer son nom ou sa description repose sa question à Jev sur tous les mails (pour un type, tout le choix de type). Changer son seuil ou ses règles recalcule seulement les règles, sans appeler Jev.

**Vérifier le fichier** : `mailsort labels check` valide `labels.yaml` sans rien toucher et résume son contenu.

**Supprimer un label** : retirer l'entrée. Le programme ne supprime pas le label Gmail tout seul : il l'indique dans le journal, et la commande `mailsort labels delete <id> --confirm` le supprime.

**Apparence des labels** : à la création d'un label, le programme applique la couleur choisie dans la colonne Couleur de la section Les labels et la visibilité de sa famille. Ensuite, il ne modifie plus que le nom : Alexis peut changer couleur et visibilité dans Gmail à tout moment, sans que le programme y touche.

| Famille | In label list | In message list |
| --- | --- | --- |
| Types | Show | Show |
| Domaines | Show if unread | Show |
| ✉️ To Reply et 🗑️ Deletable | Show | Hide |
| 🤔 To Review | Show if unread | Hide |

La couleur de fond est le code hexadécimal de la colonne Couleur. Le programme choisit la couleur du texte : blanc sur un fond foncé, gris très foncé sur un fond clair. L'API Gmail n'accepte qu'une palette fixe : si un code n'en fait pas partie, le programme prend la valeur acceptée la plus proche et l'indique dans le journal.

Au démarrage, le programme valide le fichier : `id` en double, famille inconnue, paire interdite ou protection qui vise un label inexistant, `never_with` posé sur un label ou visant un label qui n'est pas un type, champ inconnu (faute de frappe), couleur ou seuil invalide, `threshold` sur un label qui n'est pas oui/non ou inférieur à `doubt_min`, description manquante, nom Gmail en double ou contenant « / », label `to_review` absent. En cas d'erreur, il s'arrête sans rien toucher et liste toutes les erreurs d'un coup.

## Retri et corrections manuelles

Un changement de label déclenche le retri de toute la boîte, mais seules les questions des labels modifiés sont reposées à Jev. Les règles, elles, sont recalculées pour tous les labels.

**Retri complet forcé** : la commande `mailsort resort --all` repose toutes les questions sur tous les mails.

**Nouvelle version de Jev** : la version est épinglée dans `.env` (`JEV_MODEL`). L'empreinte enregistrée pour chaque réponse inclut cette version et celle des consignes envoyées à Jev : changer `JEV_MODEL` fait donc reposer toutes les questions au passage suivant, sans commande à lancer.

**Corrections manuelles** : à chaque passage, le programme compare les labels présents dans Gmail avec ceux qu'il a posés la dernière fois.

- Un label posé par le programme puis retiré par Alexis est verrouillé « absent » pour ce message.
- Un label posé par Alexis et que le programme n'avait pas posé est verrouillé « présent ».
- Un label verrouillé n'est plus jamais modifié sur ce message, même lors d'un retri complet.

Les corrections sont enregistrées avec le mail et les probabilités de Jev. La commande `mailsort corrections` les liste par label : c'est la matière pour améliorer une description avec Claude Code.

## Données envoyées à Jev et confidentialité

Jev reçoit un résumé structuré de chaque mail. Le texte est d'abord nettoyé, puis limité à 5 000 tokens (environ 20 000 caractères). L'ordre compte : certains mails commencent par des milliers de caractères invisibles, et tronquer avant de nettoyer ne laisserait voir à Jev que ces caractères.

| Donnée | Pourquoi |
| --- | --- |
| Expéditeur (nom et adresse) | Distinguer une personne d'un envoi automatique |
| Place d'Alexis : destinataire direct, en copie ou via une liste (adresse du compte Gmail, plus les alias de `MAILSORT_USER_EMAILS`) | Repérer les envois de masse |
| Objet et date | Signal principal |
| Présence d'un lien de désinscription ou d'un en-tête de liste de diffusion | Fort indice de 📰 Newsletters ou 📣 Marketing |
| Noms des pièces jointes | Une « facture.pdf » aide 💳 Payments |
| Alexis a-t-il déjà répondu dans le fil | Utile pour ✉️ To Reply |
| Texte du mail, nettoyé | HTML converti en texte ; caractères invisibles, liens, séparateurs de tableaux, citations des messages précédents et signatures retirés ; puis début du texte gardé. Le contenu des pièces jointes n'est jamais lu, seulement leur nom |

**Nettoyage du texte** : fait par le programme, avec du code classique (sans IA), juste avant l'appel à Jev, dans cet ordre.

1. **Choisir la version du mail** : la version texte si elle a du contenu, sinon le HTML converti en texte (BeautifulSoup ou html2text), sans styles, scripts ni blocs cachés comme les pré-en-têtes invisibles.
2. **Retirer les caractères invisibles** : espaces de largeur nulle, traits d'union conditionnels et autres caractères Unicode qui ne s'affichent pas.
3. **Retirer la mise en forme** : URL (le texte du lien est gardé), barres de tableaux, lignes vides en série.
4. **Retirer l'historique des réponses** : lignes citées et tout ce qui suit « Le … a écrit : », « On … wrote: », « -----Original Message----- » ou un bloc « De : … Envoyé : … À : … Objet : ». Ces règles sont écrites dans le programme plutôt que confiées à `email-reply-parser`, qui vise surtout les formules anglaises.
5. **Garder le contenu transféré** : pour un transfert, le message d'origine est l'information utile, il n'est donc pas retiré.
6. **Retirer la signature** quand elle est repérable.
7. **Limiter à 5 000 tokens**, en gardant le début du texte. Le tokeniseur de Jev n'étant pas connu, la limite est estimée à 4 caractères par token, soit 20 000 caractères ; un texte coupé se termine par « […] ».

`mailsort explain` affiche le texte nettoyé, tel que Jev l'a reçu. `mailsort clean <fichier.eml>` fait de même pour un mail téléchargé depuis Gmail, sans réseau. Des tests automatiques couvrent les cas types : mail à pré-en-tête invisible, réponse avec historique, transfert, newsletter longue, ainsi que les formats Outlook, Apple Mail et Gmail en français et en anglais.

**Sécurité :**

- l'accès Gmail utilise le droit « modifier » (scope `gmail.modify`), qui permet de gérer les labels mais pas de supprimer définitivement un mail ;
- l'app Google est publiée en statut « In production », sans vérification Google (dispense pour un usage personnel). En statut « Testing », le jeton de rafraîchissement expirerait au bout de 7 jours. À la connexion, Google affiche « Google hasn't verified this app » : c'est attendu ;
- le jeton est créé sur le Mac par `mailsort auth`, qui ouvre le navigateur et écrit `token.json`. Le fichier est ensuite copié sur le VPS (`scp`), qui n'a pas de navigateur. Le jeton peut encore être révoqué si Alexis change son mot de passe Google ou retire l'accès à l'app : il suffit alors de relancer `mailsort auth` et de recopier le fichier ;
- les clés (Gmail et Jev) restent dans un fichier `.env`, jamais dans le dépôt. Le dépôt contient `.env.example`, qu'Alexis duplique en `.env` et remplit ;
- le texte des mails n'est pas stocké dans la base, seulement les identifiants, les probabilités et les labels ;
- conservation des données chez TypeSafe (vérifié en novembre 2026) : Jev n'est pas entraîné sur les requêtes des clients. La non-conservation (« zero data retention ») n'est proposée qu'aux comptes entreprise, sur demande au support ; la documentation ne prévoit aucune option à activer dans les requêtes.

## Coût estimé

Trier 1 000 mails coûte environ 0,10 à 0,13 $ (au pire 0,30 $ avec beaucoup de longues newsletters), et un retri complet de 10 000 mails environ 1 à 2 $. Jev facture 0,042 $ par million de tokens envoyés, et ses réponses sont gratuites ([source](https://flaviocopes.com/jev-pricing/), prix relevé en septembre 2026).

| Poste par mail | Tokens (estimation) |
| --- | --- |
| Partie fixe de l'appel | ≈ 300 |
| Texte nettoyé (moyenne ; 5 000 au maximum) | ≈ 750 |
| 1 choix de type (8 options) et 10 questions oui/non, avec leurs descriptions | ≈ 1 350 |
| **Total** | **≈ 2 400, soit ≈ 0,0001 $** |

**Accès à Jev :** Alexis a un compte TypeSafe et crée sa clé dans la console. Le programme appelle directement l'API (`https://api.typesafe.ai/v1/systemone`) et la version de Jev est épinglée (`jev-1.14.0`). Le second passage des types ne concerne qu'une partie des mails et ne coûte qu'un petit appel de plus. `mailsort classify` affiche le nombre de tokens et le coût réel de chaque mail.

**Limites de débit :** elles sont fixées par organisation et visibles dans la console TypeSafe ; la documentation ne publie pas de valeurs. Le programme se limite à 10 appels par seconde par défaut (`JEV_MAX_REQUESTS_PER_SECOND` dans `.env`). En cas de surcharge (429, 529) ou d'erreur serveur ou réseau, il réessaie jusqu'à 5 fois avec un délai croissant, en suivant l'en-tête `Retry-After` s'il est présent. L'API Gmail restera le vrai facteur limitant.

## Mise en route

Le programme passe d'abord par un mode test qui ne touche pas Gmail, puis remplace les filtres actuels.

1. **Test à blanc.** `mailsort run --dry-run` classe les mails et écrit un CSV (mail, probabilités, labels qui seraient posés) sans rien modifier.
2. **Mesure de qualité.** `mailsort eval` compare le résultat aux labels actuels « 💳 Paiements » (≈ 90 fils) et « 📦 Réceptions » (≈ 45 fils), classés à la main et par filtres. Il affiche les mails où Jev n'est pas d'accord, pour ajuster descriptions et seuils.
3. **Reprise des labels existants.** Le programme renomme « 💳 Paiements » en « Types/💳 Payments » et « 📦 Réceptions » en « Types/📦 Deliveries », en gardant leurs mails. Ces labels existants ne comptent pas comme des corrections manuelles.
4. **Premier passage réel.** Il classe toute la boîte, puis produit un rapport des labels ajoutés et retirés.
5. **Bascule.** Alexis supprime les 3 filtres Gmail (Settings → See all settings → Filters and Blocked Addresses), puis le cron est activé.

| Commande | Effet |
| --- | --- |
| `mailsort auth` | Connexion à Gmail dans le navigateur ; crée ou remplace `token.json` (à lancer sur le Mac) |
| `mailsort run` | Passage normal : nouveaux mails et labels modifiés |
| `mailsort run --dry-run` | Même chose sans toucher Gmail, résultat en CSV |
| `mailsort resort --all` | Repose toutes les questions sur tous les mails |
| `mailsort eval` | Compare le classement aux labels existants |
| `mailsort messages [--query …] [--limit N]` | Liste les derniers mails (identifiant, date, expéditeur, objet), en lecture seule |
| `mailsort explain <id du mail>` | Affiche ce que Jev reçoit pour un mail, ses probabilités et les labels décidés, sans modifier Gmail ; `--text-only` n'appelle pas Jev |
| `mailsort clean <fichier.eml>` | Affiche le texte nettoyé d'un mail téléchargé, sans réseau |
| `mailsort classify <fichier.eml>` | Classe un mail téléchargé avec Jev et affiche probabilités, labels et coût, sans toucher Gmail |
| `mailsort corrections` | Liste les corrections manuelles par label |
| `mailsort labels check` | Valide `labels.yaml` sans rien toucher |
| `mailsort labels delete <id> --confirm` | Supprime un label retiré du fichier |

## Questions ouvertes

Aucune pour l'instant : toutes les questions de la v1 sont tranchées (voir Décisions prises). Les seuils restent des valeurs de départ, à ajuster avec `mailsort eval` pendant les tests en local.
