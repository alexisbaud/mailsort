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
| Hébergement | Le VPS d'Alexis, 3 passages par jour : une image Docker construite par GitHub, lancée par un timer systemd (voir Déploiement sur le VPS) |
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

Un timer systemd du VPS lance le programme à 8h30, 12h30 et 18h (heure de Paris), et rattrape au redémarrage du serveur un passage manqué pendant qu'il était éteint. Un verrou (fichier `mailsort.db.lock`, libéré même si le programme s'arrête brutalement) empêche deux passages de tourner en même temps : le second s'arrête aussitôt sans rien toucher.

1. **Synchroniser les labels.** Le programme lit `labels.yaml`, crée les labels Gmail manquants, y compris les labels parents, et renomme ceux qui ont changé de nom. Il garde le lien entre chaque label du fichier et l'identifiant du label Gmail, donc un renommage conserve les mails déjà classés.
2. **Repérer les changements de config.** Deux sortes d'empreintes sont comparées à celles du dernier passage :
   - l'**empreinte de question** (nom et description) : si elle change, la question est reposée à Jev sur tous les mails. Les types forment une seule question, donc ils partagent une seule empreinte, qui couvre aussi la description de « None of the above » ;
   - l'**empreinte des règles** (seuils, `enabled`, `never_with`, `protects_from`) : si elle change, les règles sont recalculées sur tous les mails à partir des probabilités enregistrées, sans appeler Jev.
3. **Lister les mails à traiter.** Ce sont les messages jamais traités, plus tous les messages si un label est à re-évaluer. Le programme ignore les spams, la corbeille, les brouillons et les messages envoyés par Alexis.
4. **Préparer chaque mail.** Il extrait l'expéditeur, les destinataires, l'objet, la date, les en-têtes de liste de diffusion, les noms des pièces jointes et le texte nettoyé et tronqué (voir la section sur les données envoyées).
5. **Interroger Jev.** Un appel par mail : le mail est l'« état ». Les types forment une seule question à choix (question `choice` : les 9 types plus « None of the above ») ; chaque autre label est une question oui/non (question `noul`) construite à partir de sa description. Jev renvoie une probabilité par option et par label. Un second appel, limité aux types, peut suivre pour chercher un type secondaire (section Règles de décision).
6. **Appliquer les règles.** Le code applique les seuils, les exclusions et les protections (section Règles de décision).
7. **Mettre à jour Gmail.** Le programme ajoute les labels gagnés et retire ceux qu'il avait posés et qui ne s'appliquent plus, en un seul appel par mail et seulement si quelque chose change : un passage sans nouveauté ne modifie rien. Il ne touche jamais un label qu'Alexis a corrigé à la main, ni un label qu'il n'a pas posé.
8. **Enregistrer.** Une base SQLite garde, pour chaque message, les probabilités de chaque question avec son empreinte, les labels posés par le programme et l'empreinte des règles utilisée. Elle garde aussi les verrous, l'historique des corrections, le lien entre chaque label et son label Gmail, et le bilan des passages. Chaque message est enregistré d'un bloc (transaction) : un passage interrompu ne laisse pas de message à moitié traité. Entre l'enregistrement des labels voulus et leur écriture dans Gmail, le message est marqué « écriture en cours » : si le passage s'arrête entre les deux, le suivant sait que l'écart entre la base et Gmail vient de lui, et non d'une correction manuelle, et termine l'écriture. Le schéma est versionné (version 2), et une nouvelle version du programme met la base à jour au démarrage.
9. **Faire le bilan.** Le passage écrit un journal (`logs/mailsort.log`, 5 fichiers de 1 Mo en rotation) : nombre de mails traités et nouveaux, labels ajoutés et retirés, nombre par label, nombre de 🤔 To Review, doutes, corrections manuelles repérées, erreurs éventuelles, coût Jev. Le journal ne contient que des identifiants de mails, jamais leur contenu. Le bilan est aussi enregistré dans la table `runs` de la base. Aucune notification n'est envoyée.

**Erreurs d'accès.** Si Gmail refuse l'accès (jeton expiré ou révoqué, erreur `invalid_grant`) ou si Jev refuse la clé, le passage s'arrête sans rien modifier et écrit dans le journal une ligne `ERROR` explicite avec la cause et la marche à suivre, par exemple : « Accès Gmail refusé : jeton expiré ou révoqué. Lancer `mailsort auth` sur le Mac, puis copier `token.json` sur le VPS. » Chaque passage suivant réécrit cette ligne tant que l'accès n'est pas rétabli. Il en va de même si Jev répond 402 (crédit TypeSafe épuisé).

**Limites de Gmail et pannes.** Chaque appel à l'API Gmail coûte des unités de quota (5 pour lire ou modifier un mail, 10 pour lire un fil, 1 pour lister les labels), et Gmail limite les unités par minute et par utilisateur (15 000). Le programme :

- étale ses appels pour rester sous un budget (`GMAIL_QUOTA_UNITS_PER_MINUTE` dans `.env`, 6 000 par défaut, pour laisser de la marge au client Gmail lui-même), sans rafale de plus de 2 secondes de budget : Gmail lisse sa limite sur quelques secondes, et une centaine de lectures de fils d'affilée suffisait à se faire refuser (mesuré le 25 novembre 2026) ;
- réessaie un appel refusé pour excès de débit (429, ou 403 avec une limite par minute), une erreur serveur (5xx) ou une coupure réseau : jusqu'à 8 tentatives, avec un délai qui double à chaque fois (1, 2, 4… 64 secondes, soit plus de 2 minutes au total), en suivant l'en-tête `Retry-After` s'il est fourni ;
- donne à chaque requête un délai d'attente de 60 secondes : un réseau bloqué ne fige pas le passage ;
- passe au mail suivant si un mail échoue malgré tout (il est repris au passage suivant), ignore sans erreur un mail supprimé entre la liste et sa lecture ;
- s'arrête proprement, avec le code de sortie 2, si le quota du jour est épuisé ou après 5 erreurs d'affilée (Gmail ou Jev sans doute en panne) ; le passage suivant reprend là où celui-ci s'est arrêté ;
- traite un arrêt demandé par le système (SIGTERM, redémarrage du serveur) comme une interruption : le mail en cours est abandonné, la base reste cohérente.

Jev a ses propres nouvelles tentatives (section Coût estimé).

Les labels Gmail s'appliquent message par message : dans la boîte, un fil affiche les labels de tous ses messages.

## Les labels

20 labels en 5 familles, rangés dans Gmail sous 3 labels parents : Types, Domains et Actions (par exemple « Types/💳 Payments » ; To Reply, Deletable et To Review vont sous Actions). Les types disent ce qu'est le mail, les domaines à quelle partie de la vie il se rapporte, l'action ce qu'il faut en faire. Les types forment une seule question à choix, avec une option « None of the above » décrite ainsi : « None of the other types applies. This email is not a proof of payment or refund, not the tracking of a shipment, return or download, not a ticket or booking, not an official or contractual document, not an account or security notification, not a subscription or service notice, not a customer support exchange, not a newsletter, digest or changelog, and not a promotional or marketing message. » Les autres labels sont des questions oui/non, donc un mail peut cumuler plusieurs domaines.

Les descriptions sont en anglais, car c'est ce que Jev lira. Elles décrivent des catégories, jamais une marque, un site ou un organisme. Chacune se termine par « Not: … », ce que le label ne doit pas contenir, pour lever les ambiguïtés vues au passage à blanc.

| Label | Famille | Couleur | Description envoyée à Jev |
| --- | --- | --- | --- |
| 💳 Payments | Type | `#17a765` | Proof that money actually moved for the user: invoices, receipts, paid order confirmations (including free orders), payment confirmations, rent receipts, charged subscription renewals, refunds. Not: upcoming charges, renewal or expiry reminders, failed or declined payments, payment method problems, quotes, price or fee change announcements, money received by the user, shipping or delivery updates. |
| 📦 Deliveries | Type | `#ff7537` | Tracking of something the user ordered or obtained until it reaches them: shipping, tracking, delivery, pickup points and pickup appointments, returns and exchanges, a device sent for repair, a file or resource made available for download, even if free. Not: the order or payment confirmation itself, promotions about delivery offers, customer service replies about an order. |
| 🎟️ Tickets & Bookings | Type | `#7858c2` | An access ticket or reservation tied to a date or place: transport tickets, show or event tickets, boarding passes, accommodation, restaurant or appointment bookings, event registrations, and changes or cancellations to them. Not: travel or event promotions and discount codes, review requests after a trip, subscriptions to a mobility service, generic reminders with no specific reservation. |
| 📄 Official Documents | Type | `#1e53b8` | A document with administrative, contractual or legal value worth keeping: contracts, certificates, attestations, payslips, tax notices, official statements, summons, electronically signed documents. Not: newsletters or general information from an administration, terms of use or privacy policy updates, simple receipts for a purchase, verification codes. |
| 🔐 Accounts & Security | Type | `#757575` | Identity and access to one of the user's online accounts: account creation, email verification, login or verification codes, password resets, sign-in or new device alerts, two-factor authentication, permissions or data shared with a third-party app, account detail changes. Not: subscription status, renewals, plan, price or terms changes, service activation or configuration, customer service replies, newsletters about security tips. |
| 📰 Newsletters & Digests | Type | `#007286` | Content whose purpose is to inform, with no incentive to buy: newsletters, digests, summaries, release notes, changelogs, announcements about a product or service the user uses, awareness and prevention messages (safety tips, responsible use, a company's commitments), activity recaps. Not: messages that push the user to buy, subscribe or come back, discount codes, notices about the user's own subscription or account. |
| 📣 Marketing | Type | `#c2175b` | A message whose purpose is to make the user buy, subscribe or come back: promotions, sales, discount codes, offers, new products to buy, abandoned carts, recommendations, referral programs, win-back messages, requests to review a product, a service or a trip. Not: purely informative messages with no incentive, surveys about a customer service request, receipts or confirmations of something already bought. |
| 🎧 Support | Type | `#4a86e8` (provisoire) | An exchange with a company's customer service about a request the user made: acknowledgment of a request or ticket, a reply from an agent, a follow-up question, a satisfaction survey about how the request was handled. Not: security or sign-in notifications, order or payment confirmations, newsletters, promotional surveys unrelated to a request. |
| 🔁 Subscriptions & Services | Type | `#a46a21` (provisoire) | The life of a subscription or service the user uses, when no money has moved yet: upcoming renewals and expiry reminders, plan, price, fee or terms changes, failed payments or missing payment methods, activation, configuration or cancellation of a service or option, invitations to join a team workspace. Not: receipts for charges already made, sign-in or security alerts, promotional offers to subscribe to something new, customer service exchanges. |
| 🏠 Housing | Domaine | `#ffc8af` | The user's home: renting, rent, landlord or agency, flatsharing, service charges, energy, water, internet access, home insurance, repairs, moving, housing benefits. Not: hotels or short stays during a trip, mobile phone plans, purchases of household items. |
| 🏛️ Administration | Domaine | `#ebdbde` | Formalities with public administrations and public bodies: taxes, identity documents, civil status, social security, benefits, vehicles, fines, justice. Not: tax-free shopping refunds for travelers, invoices or terms from private companies, medical care itself. |
| 🩺 Health | Domaine | `#fcd2e0` | The user's physical and mental health: medical appointments, prescriptions, test results, healthcare reimbursements, health insurance, pharmacy. Not: generic communication from a health company with nothing about the user's own care, fitness or wellness promotions. |
| 🏦 Banking & Investments | Domaine | `#b4efd3` | The user's money: bank accounts, cards, transfers, direct debits, bank fees, savings, investments, loans, cryptocurrencies. Not: purchases, trips or receipts paid by card, payments to a merchant, tax-free refunds, invoices from a company that is not a bank. |
| 💼 Work | Domaine | `#b6cff5` | The user's professional life, as an employee or self-employed: employer, colleagues, recruiting, job applications, employment contracts, payroll, work tools, clients, prospects, quotes and invoices sent by the user, freelance activity and status, freelance or side-project collaborations even from personal email addresses, professional network. Not: promotions or newsletters from professional tools and platforms, school or training matters, generic job-market marketing. |
| 🎓 Studies & Training | Domaine | `#e3d7ff` | Learning: school, university, the school side of a work-study program, courses, exams, diplomas, online training, certifications, alumni. Not: marketing for paid or free courses the user did not enroll in, professional collaborations, newsletters about a profession. |
| ✈️ Travels | Domaine | `#99d7e4` | Trips outside daily life: long-distance transport, accommodation, stays, visas and travel formalities, tax-free shopping refunds, car rental, travel insurance, activities at the destination. Not: daily commuting or local mobility, travel promotions and discount codes, newsletters from transport companies. |
| 👨‍👩‍👧 Family & Friends | Domaine | `#fbe983` | Personal exchanges with family, friends, a partner or acquaintances: messages written by someone close to the user, including a sender sharing the user's family name, emails forwarded by them, personal invitations, group money pots between friends. Not: automated messages, even when signed with a first name; colleagues or clients in a professional context; customer service agents. |
| ✉️ To Reply | Action | `#b1b1b1` | A message that expects an answer or a specific action from the user: a person asking a question, making a request or waiting for a confirmation, or an organization asking for a document or an action before a deadline. Not: automatic notifications that require nothing, promotional calls to action, review requests and satisfaction surveys, verification codes the user requested. |
| 🗑️ Deletable | Nettoyage | `#d93025` | A message with no value once read or once its date has passed: advertising, social network notifications, generic reminders, expired offers, mailing list subscription confirmations, surveys. Not: invitations addressed personally to the user (collaboration, team, code repository), alerts about unusual account activity, anything the user may need to act on or prove later. |
| 🤔 To Review | Technique | `#ffae47` | Posé par le code, pas par Jev (voir Règles de décision). |

## Règles de décision

Jev donne une probabilité entre 0 et 1 pour chaque label ; le code décide ensuite, avec les règles ci-dessous, dans cet ordre.

1. **Type principal.** Jev choisit une option parmi les 9 types et « None of the above ». Le type gagnant est posé si sa probabilité atteint 0,60, ou si les deux premières options sont des types dont les probabilités cumulées atteignent 0,80 (`primary_pair_sum`) : Jev partage alors sa certitude entre deux types, comme pour une quittance de loyer (💳 Payments 0,55 et 📄 Official Documents 0,44). Si « None of the above » gagne, le mail n'a pas de type.
2. **Type secondaire, par un second passage.** Si un autre type a obtenu au moins 0,30 au premier passage, le programme repose la question des types à Jev, sans le type gagnant. La consigne précise : « This email has already been classified as \<type gagnant>. Is it also one of the following? ». Si Jev choisit alors ce même type avec au moins 0,70, il est posé en plus. S'il choisit « None of the above » ou un autre type, rien n'est ajouté. Un mail a au plus 2 types.
3. **Paires interdites.** Le type secondaire n'est jamais posé s'il forme l'une de ces paires avec le type principal :
   - 💳 Payments et 📦 Deliveries ;
   - 📰 Newsletters & Digests et 📣 Marketing ;
   - 🔁 Subscriptions & Services et 💳 Payments : un rappel d'échéance n'est pas une preuve de paiement.
4. **Autres labels.** Chaque question oui/non a un seuil : 0,70 pour les domaines (`yes_no_min_by_family`), 0,55 pour 🗑️ Deletable (seuil propre au label), 0,80 pour ✉️ To Reply (`yes_no_min`, par défaut). Le label est posé si sa probabilité atteint son seuil.
5. **Protections.** 🗑️ Deletable n'est jamais posé si le mail a aussi 💳 Payments, 📄 Official Documents, 🎟️ Tickets & Bookings, 👨‍👩‍👧 Family & Friends ou ✉️ To Reply.
6. **Déjà répondu.** ✉️ To Reply est retiré dès que le dernier message du fil vient d'Alexis. Le programme le vérifie à chaque passage, sans appeler Jev : une seule liste des messages envoyés par Alexis lui dit dans quels fils il a écrit, et seuls ces fils sont lus (lire un fil coûte cher en quota Gmail).
7. **Zone de doute.** Un type incertain (ni 0,60, ni deux types à 0,80) ou une question oui/non entre 0,50 et son seuil est un doute : le label n'est pas posé. Le doute est enregistré dans la base, écrit dans le journal et affiché par `mailsort explain`, mais il n'envoie pas en 🤔 To Review.
8. **Mail sans repère.** 🤔 To Review est posé uniquement si le mail ne reçoit ni type, ni domaine, ni 🗑️ Deletable.

🤔 To Review disparaît dès qu'Alexis pose ou retire à la main un type, un domaine ou 🗑️ Deletable : sa correction tranche. De même, une correction sur un label d'une famille efface les doutes de cette famille.

**Précisions appliquées par le code :**

- si « None of the above » gagne avec moins de 0,60, ou s'il arrive en deuxième position, la règle des deux types ne s'applique pas : seul le seuil de 0,60 compte, car ce partage traduit un doute sur le type, pas un mail qui relève de deux types ;
- en cas d'égalité entre deux options, la première dans l'ordre de `labels.yaml` gagne ;
- les paires interdites valent dans les deux sens. Le second passage n'est pas demandé si le type candidat forme une paire interdite avec le gagnant : il ne pourrait pas être posé, l'appel serait inutile ;
- le second passage suit la même condition que le type principal : il n'est demandé que si le gagnant est posé (0,60, ou deux types à 0,80) ;
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
    primary_pair_sum: 0.80   # ou : somme des deux premiers types (mail qui relève de deux types)
    secondary_trigger: 0.30  # déclenche le second passage
    secondary_min: 0.70      # seuil du type secondaire
    none_description: >
      None of the other types applies. This email is not a proof of payment
      or refund, ...
  user:
    full_name: "Alexis Baud"   # donné à Jev en tête de chaque mail
  yes_no_min: 0.80           # seuil par défaut des questions oui/non (To Reply)
  yes_no_min_by_family:      # seuil d'une famille, à la place de yes_no_min
    domain: 0.70
  doubt_min: 0.50            # entre doubt_min et le seuil : doute noté, label non posé
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
      Proof that money actually moved for the user: ...
```

| Champ | Rôle |
| --- | --- |
| `settings.types` | Seuils du choix de type et description de « None of the above » |
| `settings.user.full_name` | Nom d'Alexis, envoyé à Jev en tête de chaque mail ; le changer repose toutes les questions |
| `settings.yes_no_min`, `yes_no_min_by_family`, `doubt_min` | Seuil par défaut des questions oui/non, seuil par famille, début de la zone de doute |
| `settings.visibility` | Visibilité par famille, appliquée seulement à la création du label |
| `id` | Lien permanent entre le fichier, la base et le label Gmail |
| `name` | Nom du sous-label, rangé sous le parent de sa famille (`settings.parents`) ; le changer renomme le label sans perdre les mails classés |
| `family` | `type` = une option du choix de type ; `domain`, `action`, `cleanup` = une question oui/non ; `system` = posé par le code (🤔 To Review) |
| `color` | Couleur de fond en hexadécimal, appliquée seulement à la création du label |
| `enabled` | `false` = le label n'est plus évalué, mais reste dans Gmail |
| `threshold` | Seuil propre à un label oui/non (optionnel, prioritaire sur celui de sa famille) |
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

La couleur de fond est le code hexadécimal de la colonne Couleur. Le programme choisit la couleur du texte : blanc sur un fond foncé, gris très foncé sur un fond clair. L'API Gmail n'accepte qu'une palette fixe : si un code n'en fait pas partie, le programme prend la valeur acceptée la plus proche à l'œil (distance dans l'espace de couleurs Lab) et l'indique dans le journal. Avec le fichier actuel, les 20 labels gardent 20 couleurs distinctes. Si Gmail refuse quand même la couleur, le label est créé sans couleur.

Au démarrage, le programme valide le fichier : `id` en double, famille inconnue, paire interdite ou protection qui vise un label inexistant, `never_with` posé sur un label ou visant un label qui n'est pas un type, champ inconnu (faute de frappe), couleur ou seuil invalide, `threshold` sur un label qui n'est pas oui/non ou inférieur à `doubt_min`, seuil de famille inférieur à `doubt_min` ou sur une famille qui n'est pas oui/non, `primary_pair_sum` inférieur à `primary_min`, nom d'utilisateur vide, description manquante, nom Gmail en double ou contenant « / », label `to_review` absent. En cas d'erreur, il s'arrête sans rien toucher et liste toutes les erreurs d'un coup.

## Retri et corrections manuelles

Un changement de label déclenche le retri de toute la boîte, mais seules les questions des labels modifiés sont reposées à Jev. Les règles, elles, sont recalculées pour tous les labels.

**Retri complet forcé** : la commande `mailsort resort --all` repose toutes les questions sur tous les mails.

**Nouvelle version de Jev** : la version est épinglée dans `.env` (`JEV_MODEL`). L'empreinte enregistrée pour chaque réponse inclut cette version, celle des consignes envoyées à Jev et le nom de l'utilisateur : changer `JEV_MODEL` fait donc reposer toutes les questions au passage suivant, sans commande à lancer.

**Corrections manuelles** : à chaque passage, le programme liste les mails qui portent chacun de ses labels (quelques appels pour toute la boîte), et compare avec ceux qu'il a posés la dernière fois.

- Un label posé par le programme puis retiré par Alexis est verrouillé « absent » pour ce message.
- Un label posé par Alexis et que le programme n'avait pas posé est verrouillé « présent ».
- Un label verrouillé n'est plus jamais modifié sur ce message, même lors d'un retri complet. Si Alexis change d'avis (il repose un label retiré, ou retire un label ajouté), le verrou suit sa dernière correction.
- Un label du fichier déjà présent sur un mail que le programme n'a encore jamais traité compte comme posé par Alexis.
- Un label supprimé dans Gmail est recréé au passage suivant, sans verrou : le programme le repose là où il s'applique.

Les corrections sont enregistrées avec le mail et les probabilités de Jev. La commande `mailsort corrections` les liste par label : c'est la matière pour améliorer une description avec Claude Code.

## Données envoyées à Jev et confidentialité

Jev reçoit un résumé structuré de chaque mail. Le texte est d'abord nettoyé, puis limité à 5 000 tokens (environ 20 000 caractères). L'ordre compte : certains mails commencent par des milliers de caractères invisibles, et tronquer avant de nettoyer ne laisserait voir à Jev que ces caractères.

| Donnée | Pourquoi |
| --- | --- |
| Nom d'Alexis, en tête : « User: Alexis Baud (the owner of this mailbox) » (tiré de `settings.user.full_name`) | Reconnaître un proche qui porte le même nom de famille |
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
- pour pouvoir publier, Google exige dans Branding une page d'accueil, une page de règles de confidentialité et leur domaine parmi les domaines autorisés. Ces pages vivent dans `docs/` et sont publiées par GitHub Pages depuis `main` : `https://alexisbaud.github.io/mailsort/` et `https://alexisbaud.github.io/mailsort/privacy.html`, domaine autorisé `alexisbaud.github.io`. Les règles de confidentialité décrivent ce que fait le code : elles sont à mettre à jour si ce qui est lu, envoyé à Jev ou stocké change ;
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

1. **Test à blanc.** `mailsort run --dry-run` lit les vrais mails, interroge Jev et applique les règles, sans rien modifier dans Gmail : aucun label n'est créé, posé ou retiré. Il affiche le nombre de mails par label, le nombre de mails sans aucun label, ceux dont le seul label serait 🤔 To Review, et le total trié. Avant ce bilan, il liste chaque mail qui irait dans 🤔 To Review (donc sans repère) : identifiant, date, expéditeur, objet, autres labels, ses doutes avec leurs scores (type incertain : les trois meilleurs types et les seuils ; doute : le score et le seuil du label) et les repères les plus proches (meilleur type, trois domaines ou nettoyages les mieux notés). Après le bilan, il compte les mails par doute et par absence de repère, avec le nombre de ceux qui partent en 🤔 To Review. `--no-review` masque la liste. Il écrit aussi un CSV : mail, probabilités, labels qui seraient posés, doutes et absence de repère (`review_reasons`), et `to_review` (`yes` ou `no`) qui dit si le mail part réellement en 🤔 To Review. Par défaut il traite les 200 mails les plus récents ; `--limit N`, `--all` et `--query` changent le périmètre. Au-delà de 500 mails à envoyer à Jev, il affiche le coût estimé et demande confirmation. Les réponses de Jev sont enregistrées dans la base (pas les décisions) : relancer le test après un changement de seuil ne coûte rien, et seules les questions modifiées sont reposées. Si aucun jeton Gmail n'existe encore, il ouvre le navigateur pour l'autorisation.
2. **Mesure de qualité.** `mailsort eval` compare le résultat aux labels « 💳 Paiements » et « 📦 Réceptions », classés à la main et par filtres, ou à toute autre paire passée par `--compare "Nom du label Gmail=id"`. Il classe les mails de référence (réponses reprises si elles existent), puis affiche l'accord, le rappel, la précision et les mails où Jev n'est pas d'accord, pour ajuster descriptions et seuils. Rien n'est modifié dans Gmail. Au 25 novembre 2026, ces deux labels n'existent plus dans la boîte : il n'y a rien à comparer, et les corrections manuelles (`mailsort corrections`) prennent le relais.
3. **Reprise des labels existants.** Un label Gmail qui porte déjà le nom complet d'un label du fichier (par exemple « Types/💳 Payments ») est repris au lieu d'être recréé. Les anciens labels « 💳 Paiements » et « 📦 Réceptions » n'existant plus, il n'y a rien d'autre à reprendre.
4. **Premier passage réel.** D'abord sur un petit lot (`mailsort run -q "newer_than:7d"`), puis sur toute la boîte (`mailsort run --csv`) : le bilan donne les labels ajoutés et retirés, le CSV le détail par mail.
5. **Bascule.** Alexis supprime les 3 filtres Gmail (Settings → See all settings → Filters and Blocked Addresses), puis le timer est activé.

| Commande | Effet |
| --- | --- |
| `mailsort auth` | Connexion à Gmail dans le navigateur ; crée ou remplace `token.json` (à lancer sur le Mac) |
| `mailsort run [--limit N] [--query …] [--csv] [--yes]` | Passage normal : nouveaux mails, labels modifiés, corrections manuelles, règle « déjà répondu » ; `--csv` écrit le détail des mails modifiés. Code de sortie 0, 1 (erreur d'accès), 2 (arrêté avant la fin) |
| `mailsort run --dry-run [--limit N \| --all] [--query …] [--no-review]` | Classe sans rien modifier dans Gmail : mails 🤔 To Review avec doutes et scores, bilan par label, sans label et total, décompte des doutes, plus un CSV |
| `mailsort resort --all` | Repose toutes les questions sur tous les mails, en gardant les verrous |
| `mailsort eval [--compare "Label Gmail=id"]` | Compare le classement aux labels existants, sans rien modifier |
| `mailsort messages [--query …] [--limit N]` | Liste les derniers mails (identifiant, date, expéditeur, objet), en lecture seule |
| `mailsort explain <id du mail>` | Affiche ce que Jev reçoit pour un mail, ses probabilités et les labels décidés, sans modifier Gmail ; `--text-only` n'appelle pas Jev |
| `mailsort clean <fichier.eml>` | Affiche le texte nettoyé d'un mail téléchargé, sans réseau |
| `mailsort classify <fichier.eml>` | Classe un mail téléchargé avec Jev et affiche probabilités, labels et coût, sans toucher Gmail |
| `mailsort corrections [--show N]` | Liste les corrections manuelles par label, avec la probabilité de Jev |
| `mailsort labels check` | Valide `labels.yaml` sans rien toucher |
| `mailsort labels delete <id> --confirm` | Supprime de Gmail un label retiré du fichier (les mails restent) |

## Déploiement sur le VPS

Le programme suit les règles de la plateforme du VPS (dépôt `vps-platform`) : une image Docker construite par GitHub et rangée dans GHCR, un dossier par site sous `/srv/`, des secrets qui ne quittent pas le serveur. Ce n'est pas un site : ni Caddy, ni domaine, ni port ouvert.

- **Image** (`Dockerfile`) : Python 3.12, dépendances figées par `uv.lock`, `labels.yaml` compris, compte sans droits (uid 10001), heure de Paris. Le `.dockerignore` est une liste blanche : seuls le code, `labels.yaml` et les fichiers du projet Python y entrent, jamais `.env`, `token.json`, `credentials.json`, la base, le journal ni les CSV.
- **Pile** (`deploy/compose.yml`, copié en `/srv/mailsort/compose.yml`) : un service `mailsort` sous un profil, pour que `docker compose up` ne lance jamais de passage par erreur. Système de fichiers en lecture seule, aucun droit spécial, 256 Mo de mémoire au plus.
- **Données** (`/srv/mailsort/data/`, monté sur `/data`) : `token.json` (copié du Mac, renouvelé par le programme), `mailsort.db`, `logs/`. C'est le seul dossier où le programme écrit.
- **Secrets** (`/srv/mailsort/.env`, modèle `deploy/.env.example`) : clé Jev dédiée au VPS, version de Jev, adresses de l'utilisateur, budget Gmail. `credentials.json` reste sur le Mac : il ne sert qu'à créer le jeton.
- **Lancement** : le timer systemd lance `docker compose run --rm mailsort run`. Un passage à la main sur le VPS : `site mailsort run --rm mailsort run`.
- **Une seule base** : une fois le VPS en service, le Mac ne lance plus de vrai passage (passage à blanc et `explain` restent possibles), sinon les deux bases divergent.

## Questions ouvertes

Aucune pour l'instant : toutes les questions de la v1 sont tranchées (voir Décisions prises). Les seuils restent des valeurs de départ, à ajuster avec `mailsort eval` pendant les tests en local.
