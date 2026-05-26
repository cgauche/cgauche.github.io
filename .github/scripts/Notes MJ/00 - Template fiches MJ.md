# Template fiches MJ — instructions de rédaction

> **Référence pour Claude et le MJ humain.** Ce fichier documente la structure
> standard des fiches Notes MJ et la procédure d'adaptation des fiches
> existantes au template canonique. Fiche pilote PNJ :
> `Notes MJ/PNJ/Yann Zuntermein.md`.

## Principes fondateurs

Ces fiches servent **deux usages simultanés** :

1. **MJ humain en préparation / session** : trouver en moins d'une minute l'info dont il a besoin pour décrire, faire parler, manipuler ou affronter une entité.
2. **Claude (sessions futures)** : pouvoir préparer un scénario, citer le canon, ou répondre à une question sur l'entité, sans risque d'inventer.

D'où les règles directrices :

- **Tout fait factuel est sourcé canon** via une ref en backticks `` `EiR Intro l.682` `` placée juste après l'affirmation.
- **Aucune invention narrative non sourcée**. Si le canon ne dit pas, on ne dit pas. Le statbloc + les plots canon donnent au MJ tout ce qu'il faut pour improviser à table — pas besoin de réinventer.
- **Pas de doublon entre sections**. Si une info est dans le bandeau, elle ne se répète pas dans le corps.
- **Noms complets** pour déclencher les popovers : "Liepmund Holzkrug", pas "Holzkrug". Si la fiche blog porte une orthographe différente du canon, l'aliasing se fait via `_MJ_MANUAL_ALIASES` dans `_site_build.py`.

## Critères de qualité (checklist)

Une fiche est considérée comme **conforme au template** si elle valide les
critères suivants. Toute fiche qui échoue à un critère est **incomplète** et
doit être retravaillée avant d'être considérée prête.

### Citations canon

- [ ] **Chaque affirmation factuelle** porte une ref canon en backticks placée à la fin de la phrase ou du bullet. Exemples d'affirmations factuelles :
  - "Yann mesure plus d'1m85" → factuel sourçable canon
  - "Il dirige le Spionwerber" → factuel sourçable canon
  - "Il sait que l'Empereur est malade" → factuel sourçable canon
  - "Si les PJ ramènent une note signée, l'écriture est reconnue" → factuel sourçable canon
- [ ] **Aucune ref `de mémoire`** : chaque numéro de ligne a été vérifié dans le fichier source avant rédaction. Méthode : `awk 'NR>=X && NR<=Y {print NR": "$0}' "Source/.../<file>.md"`.
- [ ] **Aucune ref pointant sur du contenu cassé** : pas d'extrait vide, pas de bouillie OCR `<br>`, pas de heading isolé. Si la ligne cible tombe sur une zone cassée, chercher la ligne alternative qui pointe les paragraphes lisibles équivalents.
- [ ] **Aucune affirmation inventée** présentée sans drapeau "Inférence MJ" explicite. Si on doit inférer (extrapolation depuis le statbloc ou les plots), soit on sourcerait l'extrapolation avec la ref du matériel de base, soit on retire la phrase.
- [ ] **Citations directes en VO** : phrases canon prononcées par le PNJ restent en anglais (entre `*« »*`), non traduites.

Ce que **ne sont pas** des affirmations factuelles sourçables (donc pas besoin de ref) :

- Connecteurs narratifs ("D'où", "Ainsi", "En conséquence").
- Synthèses internes de la fiche (un sous-titre, un titre de section).
- Indices d'ordre (S57, S60 — métadonnée table).
- Renvois vers d'autres sections de la même fiche.

### Popovers entités

- [ ] **Toute mention d'un PNJ, Lieu, Faction utilisé dans une autre fiche** déclenche un popover. Vérification : sur la page rendue, le nom est wrappé en `<a class="entity-pop">`.
- [ ] **Noms complets uniquement** : "Liepmund Holzkrug", "Karl-Franz Holswig-Schliestein", "Wolfgang Holswig-Abenauer". Pas "Holzkrug", "Karl-Franz" seuls — ils ne déclenchent rien (la map popover indexe les titres complets).
- [ ] **Sous-titre obligatoire** : chaque fiche commence par `**Sous-titre** : <5-12 mots>` extrait par le builder pour le popover MJ-only. Pas de sous-titre = popover MJ sans information.
- [ ] **Pas de wikilinks `[[Nom]]`** autour des noms d'entités dans le corps — le système popover s'en occupe seul. Les `[[]]` restent uniquement pour la navigation entre fiches scénario (Hub → scènes).
- [ ] **Si nom ambigu (deux personnages partagent un prénom)** : ne pas raccourcir. Toujours forme complète. Le matching strict (`_norm_match_keys_strict` dans `_site_build.py`) refuse les sous-tokens pour éviter les faux positifs (cas "Wolfgang Kellermann" matchait à tort "Wolfgang Holswig-Abenhauer" sur "wolfgang" seul).

### Densité indicative (ordre de grandeur)

Pour calibration, la fiche pilote `Yann Zuntermein.md` contient :
- ~110 lignes utiles
- ~28 refs canon (≈1 ref par 4 lignes utiles)
- ~70 popovers entités déclenchés sur la page rendue
- 0 invention narrative non sourcée
- 0 doublon de section

Une fiche "vide de refs" sur les sujets factuels = anormale. Une fiche "noyée de refs sur tout" = sur-référencement (les connecteurs n'ont pas besoin de ref).

### Procédure de validation

Avant de considérer une fiche comme terminée :

```bash
# 1. Lister les refs canon
grep -oE '`(EiR|EiS|DoR|PBT|HR|EiR Companion|EiS Companion|DoR Companion|PBT Companion|HR Companion|Altdorf|Middenheim|Salzenmund|Up in Arms|RN&HD|Archives Vol [IVX]+) (Intro|ch\.[0-9]+)[^`]*`' "Notes MJ/<dossier>/<Fiche>.md"

# 2. Re-builder le site
python _site_build.py

# 3. Compter popovers et refs sur la page rendue
python -c "
import re
with open('_site/<categorie>/<fiche-slug>.html', encoding='utf-8') as f:
    html = f.read()
m = re.search(r'<section class=.mj-only mj-entity-enrichment.>(.+?)</section>', html, flags=re.DOTALL)
body = m.group(1)
print(f'Canon refs : {len(re.findall(chr(34) + chr(99) + chr(108) + chr(97) + chr(115) + chr(115) + chr(34) + chr(61) + chr(34) + chr(99) + chr(97) + chr(110) + chr(111) + chr(110) + chr(45) + chr(114) + chr(101) + chr(102) + chr(34), body))}')
print(f'Popovers entit : {len(re.findall(chr(34) + chr(99) + chr(108) + chr(97) + chr(115) + chr(115) + chr(34) + chr(61) + chr(34) + chr(101) + chr(110) + chr(116) + chr(105) + chr(116) + chr(121) + chr(45) + chr(112) + chr(111) + chr(112) + chr(34), body))}')
"

# 4. Pour CHAQUE ref, vérifier l'extrait
# Ouvrir _site/<categorie>/<fiche-slug>.html?mj=<TOKEN> dans le navigateur
# Hover sur chaque superscript numéroté
# L'extrait doit être pertinent à l'affirmation citée — pas un heading orphelin, pas une table cassée
```

### Critère de validation final

> Une fiche est conforme si **chaque hover** sur un superscript canon
> affiche un extrait pertinent, **chaque popover entité** se déclenche
> correctement, et **chaque affirmation factuelle** est tracée à une
> source canon.

## Conventions techniques

### Citations canon (refs backticks)

Format strict : `` `<BOOK> <CHAPTER> l.<LINES>` `` ou `` `<BOOK> <CHAPTER> p.<PAGE>` `` (mais `p.NNN` n'est plus résolvable, la pagination PDF est perdue à la conversion).

**Livres reconnus** par le builder (cf. `_CANON_BOOK_DIRS` dans `_site_build.py`) :

| Abréviation | Livre |
|---|---|
| `EiS` | Enemy in Shadows (vol 1) |
| `DoR` | Death on the Reik (vol 2) |
| `PBT` | Power Behind the Throne (vol 3) |
| `HR` | The Horned Rat (vol 4) |
| `EiR` | Empire in Ruins (vol 5) |
| `EiS Companion`, `DoR Companion`, `PBT Companion`, `HR Companion`, `EiR Companion` | Les 5 Companions |
| `Altdorf` | Altdorf — Crown of the Empire |
| `Middenheim` | Middenheim — City of the White Wolf |
| `Salzenmund` | Salzenmund |
| `Up in Arms` | Up in Arms |
| `RN&HD` | Rough Nights & Hard Days |
| `Archives Vol I/II/III` | Archives of the Empire |

**Chapitres** :
- `Intro` pour l'introduction d'un volume
- `ch.N` pour le chapitre N (pas `chN`, pas `Chapter N`)
- `Appendix` pour un appendix

**Lignes** :
- `l.205` — une seule ligne
- `l.205-218` — plage continue
- `l.215+217` — deux lignes non contiguës
- `l.215+217+220` — trois lignes ou plus non contiguës (utiliser `+` comme séparateur, jamais virgule)

Le rendu HTML transforme `` `EiR Intro l.682` `` en superscript numéroté `<sup>N</sup>`, navigable par survol (extrait) et clic (page source canon avec ancre `#L682`).

### Conventions de précision

- **Toujours vérifier le contenu de la ligne** avant de la citer. Ne pas citer "de mémoire". Utilise `awk 'NR>=X && NR<=Y {print NR}'` sur le fichier source pour confirmer.
- **Si une ligne tombe sur un heading ou une table cassée par l'OCR**, chercher une ref alternative qui pointe sur les paragraphes lisibles équivalents. Le système popover sert de garde-fou : un extrait vide ou bizarre = ref mal placée.
- **Citations en VO** : les phrases canon directes (entre `*« »*` ou `*"..."*`) restent en VO anglais. Les autres mentions des entités utilisent le français canon (cf. `Notes MJ/Orthographe canon - corrections à appliquer.md`).

### Popovers entités

- Toute mention du **nom complet** d'une fiche existante (PNJ, Lieu, Faction) déclenche un popover automatique.
- Si la fiche blog utilise une orthographe différente du canon Lexicanum/Fandom (ex. blog "Aldorf" / canon "Altdorf"), l'aliasing doit être ajouté à `_MJ_MANUAL_ALIASES` dans `_site_build.py`.
- Pas de `[[wikilinks]]` autour des noms d'entités dans le corps de la fiche — le système popover s'occupe seul du linking.

### Sous-titre extractible

La ligne `**Sous-titre** : <courte description>` est extraite par `_extract_mj_entity_metadata` et utilisée comme `data-subtitle` dans le popover MJ. Format :

```markdown
**Sous-titre** : Magister Magistri de la cellule Main Pourpre d'Altdorf
```

5-12 mots maximum. Décrit la fonction principale de l'entité.

### Statut

Ligne en gras juste après le sous-titre :

```markdown
**Statut** : [ENNEMI ACTIF]
```

Valeurs canon : `[VIVANT]`, `[MORT]`, `[DISPARU]`, `[ENNEMI ACTIF]`, `[ALLIÉ]`, `[INACTIF]`. Pour entités secondaires : adapter (`[CULTE ACTIF]`, `[LIEU OPÉRATIONNEL]`, etc.).

#### Statut évolutif

Pour un PNJ dont l'état a changé au fil des sessions (par ex. présumé
mort puis revenu vivant), conserver la trace de l'évolution avec des
flèches et des références de session entre crochets :

```markdown
**Statut** : [PRÉSUMÉ MORT S39 → REVENU VIVANT S61 → ENNEMI ACTIF en disgrâce]
```

Format :
- Chaque étape entre majuscules.
- Référence de session `[Sxx]` juste après l'état pivot.
- Flèche `→` pour la transition.
- Éventuelle nuance qualitative en minuscules à la fin (`en disgrâce`, `affaibli`, `réfugié`).

Voir `Notes MJ/PNJ/Karl-Heinz Wasmeier.md` pour un exemple complet.

### Conventions de naming des fichiers

- Chemin : `Notes MJ/<type>/<Nom Canon>.md`
- `<type>` ∈ `PNJ`, `Lieux`, `Factions`, `Documents`, `Arcs`, `Turmoil`, `Scénarios`.
- `<Nom Canon>` :
  - Forme canonique Lexicanum/Fandom du nom (cf. `Notes MJ/Orthographe canon - corrections à appliquer.md`).
  - Espaces autorisés ("Karl-Heinz Wasmeier.md").
  - Accents autorisés ("Île Noire.md", "Collège Gris.md").
  - Apostrophes droites typographiques selon le canon ("Cellule Shornaal d'Ubersreik.md").
- **Pas de** : caractères ASCII de remplacement (`oe` au lieu de `œ`), doublons d'orthographe, casse fantaisiste.
- **Variants d'un personnage** : si un même PNJ a plusieurs apparitions distinctes documentées séparément, utiliser le suffixe `(2)`, `(3)` (par ex. `Boris Todbringer (2).md`) — pas une duplication, mais un état/apparition distincte. Convention déjà acceptée dans CLAUDE.md.

### PNJ décédés mais cités

Les PNJ morts pendant la campagne (par ex. Kastor Lieberung mort à
Bögenhafen S8, Brunhilde Klaglich morte S38) restent **dans la map
popover** : ils sont mentionnés par les autres fiches qui leur survivent,
et le MJ a souvent besoin de retrouver leur contexte historique.

- Garder leur fiche `Notes MJ/PNJ/<Nom>.md`.
- Mettre le statut `[MORT Sxx]` ou `[MORT canon ch.X]` avec la session/le chapitre de la mort.
- Conserver leurs Apparitions canon — y compris la mort si elle est dans le canon.
- Le popover continue de se déclencher sur leur nom complet, ce qui permet aux fiches futures de référencer leur passé.

## Template PNJ

Structure canonique (voir `Notes MJ/PNJ/Yann Zuntermein.md`) :

```markdown
# Nom Complet

**Sous-titre** : Fonction principale
**Statut** : [STATUT]

## Apparence et manières

Paragraphe physique + indices sociaux + comportement. Sourcer la
description physique.

## Phrases canon

- *« Citation littérale du livre »* — contexte + ref `BOOK ch.N l.NNN`.
- *(2-4 phrases dans des registres différents si possible)*

> **Note importante** : un Doomed (Talent WFRP4) **n'est jamais** une
> phrase prononcée par le personnage — c'est une prophétie de mort lue
> à sa naissance. Sa place est dans le statbloc, pas ici.

## Réseau

- **Supérieur direct** : Nom — fonction `BOOK ref`.
- **Subordonnés** : ...
- **Allié(s)** : ...
- **Manipulés** : ...
- **Rivaux** : ...

Chaque lien sourcé.

## Objectifs et angle mort

**Ce qu'il veut** `BOOK ref` : motivation explicite.

**Ce qu'il sait** `BOOK ref` : informations détenues.

**Ce qu'il ignore** `BOOK ref` : angle mort exploitable (souvent le levier dramatique principal).

## Plans en cours `BOOK ref`

- **Plan A** : description courte.
- **Plan B** : description courte.

## Démasquage et confrontation (ch.N)

- **Déclencheur** `BOOK ref` : conditions de déclenchement.
- **Confrontation** `BOOK ref` : modes de résolution.
- **Variantes** : situations alternatives sourcées.

## Apparitions canon Arc N

- SXX — Action ; éventuelle citation. `BOOK ref`.

## Statbloc — Profil (Carrière Niveau) `BOOK ref`

Table M/WS/.../W + Skills + Talents + Trappings + Spells. Sans gras
arbitraire dans les listes (seulement les labels de section en gras).

## Liens externes

- [Nom — Lexicanum](URL)
- [Nom — Fandom](URL)
- [Nom — Bibliothèque Impériale](URL)
```

**Ordre canonique des liens externes** :
1. **Lexicanum** (`https://whfb.lexicanum.com/`) — plus fiable, à mettre en premier.
2. **Fandom Warhammer** (`https://warhammerfantasy.fandom.com/`) — complément, à mettre en deuxième.
3. **Bibliothèque Impériale** (`https://bibliotheque-imperiale.com/`) — source FR pour traduction canon, en troisième seulement si l'entité y est documentée.

N'ajouter que les liens **réellement existants** — ne pas inventer une URL Lexicanum si la page n'existe pas. Vérifier avant.

### Sections optionnelles

- **Identité publique / Identité secrète** : seulement pour PNJ à double identité significative. Sinon, le sous-titre + Réseau suffisent.
- **Faction principale** : ligne après Statut si l'appartenance est essentielle et pas implicite par le sous-titre.

## Template Lieu

Pilote : `Notes MJ/Lieux/Volkshalle.md`.

Substitutions par rapport au PNJ :

| Section PNJ | Section Lieu |
|---|---|
| Apparence et manières | **Description** (architecture, atmosphère) |
| Phrases canon | *(supprimer — un lieu ne parle pas)* |
| Réseau | **Composition / qui s'y trouve** (PNJ associés) |
| Objectifs et angle mort | *(supprimer)* |
| Plans en cours | **Rôle dans l'intrigue** (à quoi sert ce lieu) |
| Démasquage et confrontation | **Mécanique de scène** (DCs, pièges, salles remarquables, accès) |
| Apparitions canon Arc N | *(garder, parfois implicite par les scènes qui s'y déroulent)* |
| Statbloc | *(supprimer — sauf si lieu fortifié avec garnison)* |

Sections nouvelles utiles pour un lieu :
- **Géographie / accès** : où c'est, comment on y va.
- **Reliquaire / objets remarquables** : si le lieu contient des artefacts importants.

## Template Faction

Pilote : `Notes MJ/Factions/Spionwerber.md`.

Substitutions par rapport au PNJ :

| Section PNJ | Section Faction |
|---|---|
| Apparence et manières | *(supprimer)* |
| Phrases canon | *(supprimer ou conserver si slogans canon)* |
| Réseau | **Composition / Hiérarchie** (PNJ membres + structure) |
| Objectifs et angle mort | **Doctrine** (croyances, méthodes) |
| Plans en cours | **Rôle officiel vs Rôle réel** (si infiltrée ou double agenda) |
| Démasquage et confrontation | **Hooks** (comment les PJ interagissent) |
| Statbloc | *(supprimer)* |

Sections utiles :
- **Personnages clés** : liens vers PNJ membres avec une ligne chacun.
- **Influence** : où la faction agit, ressources, secrets.
- **Vent / Spécialité** : pour les ordres magiques (cf. `Notes MJ/Factions/Collège Gris.md`).

## Template Document

Distinguer deux cas :

### Handout in-game

Texte à donner aux PJ. Pas de template — pure prose, citation in-extenso du document. Notes MJ-only à part dans une section finale clairement marquée.

Exemple : `Notes MJ/Documents/Fassbinder - documents bureau.md`.

### Fiche-document de référence (rare)

Si on documente un objet/lettre/livre comme entité canon (référencé par d'autres fiches) :

```markdown
# Nom du Document

**Sous-titre** : Type + provenance
**Statut** : [...]

## Source canon
`BOOK ref` + résumé du contenu

## Texte (citation in extenso)
*« ... »*

## Provenance et contexte de découverte
...

## Effets / conséquences
...

## Liens
...
```

## Anti-patterns à éviter

### 1. Inventer ce qui n'est pas dans le canon

Pas de "tactique de combat round par round", pas de "modus operandi détaillé", pas de "voix imaginée" si ce n'est pas dans le livre. Le canon donne le statbloc et les Plots — le MJ improvise le reste à table avec ces matériaux. La fiche ne doit pas duplicer son cerveau.

### 2. Section "MJ-only" sans contenu canon

Si on est tenté d'écrire une section MJ-only, vérifier qu'elle contient bien des faits canon (avec refs). Sinon c'est de l'invention déguisée.

### 3. Blockquote-pitch d'introduction

Évité. Le sous-titre + statut + sections suivantes donnent l'info sans phrase-pitch redondante.

### 4. Tags `#xxx`

Inutiles : le builder ne les indexe pas pour la recherche. Le système de popovers + l'index `00 - Index.md` font le travail.

### 5. Section "Hooks" générique en pied

Évitée : doublonne souvent "Démasquage" et "Plans en cours" qui couvrent les leviers narratifs. Si un hook est vraiment isolé, il s'intègre à "Démasquage / Plans" plutôt qu'à une section dédiée.

### 6. Section "Liens" en pied (Arc / Factions / Lieux)

Évitée : doublonne les popovers déjà présents dans le corps. Le système popover + backlinks `_mj_site_build.py` couvrent la navigation.

### 7. Wikilinks `[[Nom]]` autour des entités

À éviter pour les noms d'entités — le système popover s'en occupe. Les `[[wikilinks]]` restent uniquement pour la navigation entre fichiers scénario (Hub → scènes).

### 8. Citer "de mémoire"

Ne jamais inscrire `BOOK ref l.NNN` sans avoir vérifié le contenu réel de la ligne dans le fichier source. Le système popover révèle les erreurs immédiatement (extrait incorrect), mais autant ne pas en créer.

### 9. Fragmentation en sous-mots

Les popovers déclenchent uniquement sur les **noms complets** des fiches. "Holzkrug" seul ne match pas. Toujours écrire "Liepmund Holzkrug" si on veut le popover.

### 10. Citations canon traduites en français

Les citations directes du livre (entre `*« »*` ou `*"..."*`) restent en VO anglais. Les paraphraser en français = perdre la référence textuelle.

## Création de fiches pour entités mentionnées mais absentes

Quand on rédige ou retravaille une fiche, on mentionne des PNJ, Lieux,
Factions, Documents. Certains n'ont pas encore de fiche dédiée mais
**mériteraient d'en avoir une** — ce qui permettrait au système de
popovers de les rendre interactifs.

### Quand créer une nouvelle fiche

Créer une fiche pour une entité mentionnée **si** :

- L'entité est **canon WFRP** (présente dans un livre Source/, sur Lexicanum/Fandom, ou sur la Bibliothèque Impériale).
- L'entité est mentionnée dans **au moins 2-3 autres fiches MJ**, ou dans la fiche en cours de rédaction comme élément récurrent.
- L'entité a une **densité d'information suffisante** : au moins quelques lignes canon ou un sous-rôle dans l'intrigue. Pas pour un PNJ figurant cité une seule fois sans description.

**Ne pas créer** :

- Pour une entité purement décorative citée en passant ("un garde", "un marchand").
- Pour un concept générique (Empire, Tzeentch, etc.) — ce sont des termes implicites, pas des entités.
- Pour un PNJ joueur ou un personnage homebrew table — ils n'ont pas leur place côté Notes MJ canon.

### Hiérarchie des sources (ordre d'importance)

Lorsqu'on collecte les informations pour créer la nouvelle fiche, **respecter
strictement** cette hiérarchie :

| Priorité | Source | Quand utiliser |
|---|---|---|
| **1** | **Source/<livre>.md** (canon C7 converti) | Toujours en premier. Citations directes en backticks. C'est le canon de la campagne. |
| **2** | **Lexicanum** (`https://whfb.lexicanum.com/`) | Orthographe canon des noms propres + contexte WHFB officiel. Plus fiable que Fandom. |
| **3** | **Fandom Warhammer Wiki** (`https://warhammerfantasy.fandom.com/`) | Complément si Lexicanum incomplet ou absent. Vérifier que la source est canon C7/WHFB et pas Total War. |
| **4** | **Bibliothèque Impériale** (`https://bibliotheque-imperiale.com/`) | Pour la traduction FR des termes canon. À consulter avant toute francisation. |
| **5** | **Mon Ennemi Intérieur Blog/Résumés/** | Pour le contexte table local (ce que les PJ ont vu / fait avec cette entité). |
| **6** | **Inférence MJ explicite** | En **dernier recours**, drapeautée par un blockquote `> Inférence MJ extrapolée de [ref canon]`. Jamais pour combler un vide canon — uniquement pour relier des éléments canon entre eux. |

**Règle critique** : ne jamais utiliser une source de priorité inférieure
pour **contredire** une source de priorité supérieure. Si Source/ dit X et
Fandom dit Y, on retient X. Si Source/ ne dit rien et Lexicanum dit X,
on retient X avec ref Lexicanum.

### Hiérarchie pour les conflits d'éditions WFRP

Cf. mémoire `feedback_wfrp4-supersedes-other-editions` : pour tout
conflit canon **WFRP4 vs WFRP2/3** → WFRP4 fait foi.

### Stub minimum acceptable

Une fiche stub créée à la volée doit respecter **au minimum** :

```markdown
# Nom Complet

**Sous-titre** : Fonction principale (5-12 mots)
**Statut** : [VIVANT] / [ACTIF] / etc.

## <Description selon type — Apparence pour PNJ, Description pour Lieu, etc.>

[Au moins 1 paragraphe sourcé canon, idéalement 3-5 lignes.]

## <2-3 sections additionnelles selon type>

[Au moins 5-10 bullets sourcés canon.]

## Liens externes

- [Nom — Lexicanum](URL) [si disponible]
- [Nom — Fandom](URL) [si disponible]
```

**Critère stub minimum** : la fiche doit déclencher un popover utile (sous-titre clair + statut), et le clic vers la fiche doit donner au lecteur un contenu canon substantiel — pas juste un titre vide.

### Workflow de création d'une fiche stub

1. **Vérifier l'absence** : la fiche existe-t-elle déjà sous un autre nom ? (`grep -ri "Nom complet" "Notes MJ/"`).
2. **Identifier le type** : PNJ, Lieu, Faction, Document.
3. **Collecter les sources** dans l'ordre :
   a. Grep dans `Source/` pour toutes les mentions canon (`grep -rn "Nom" "Source/"`).
   b. Si quasi-rien dans Source/, vérifier Lexicanum et Fandom via WebSearch.
   c. Si traduction FR ambigüe, consulter Bibliothèque Impériale.
4. **Choisir l'orthographe canonique** : Lexicanum > Fandom > PDF OCR. Cf. `Notes MJ/Orthographe canon - corrections à appliquer.md`.
5. **Créer le fichier** sous `Notes MJ/<dossier>/<Nom Canon>.md` avec le template adapté.
6. **Rédiger en respectant les critères de qualité** : chaque affirmation factuelle sourcée canon, sous-titre extractible, noms complets pour popovers internes.
7. **Si le titre canon FR diffère du titre blog** : ajouter une entrée à `_MJ_MANUAL_ALIASES` dans `_site_build.py` (par ex. blog "Aldorf" / canon "Altdorf").
8. **Rebuild + vérifier** : la fiche est-elle générée ? Le popover se déclenche-t-il sur les autres fiches qui la mentionnent ?

### Exemples récents

Pour la migration de Yann, 5 fiches stubs ont été créées : Spionwerber, Ordo Terribilis, Volkshalle, Collège Gris, Île Noire. Voir ces fichiers comme exemples concrets de stubs.

## Workflow de migration d'une fiche existante

Procédure recommandée pour adapter une fiche actuelle au template :

1. **Identifier le type** : PNJ / Lieu / Faction / Document.
2. **Sauvegarder l'original** (`cp Fiche.md Fiche.md.before-template`).
3. **Extraire le contenu canon** déjà présent dans la fiche : chaque affirmation factuelle doit être tracée à une ref canon. Si une affirmation n'a pas de source canon identifiable, soit la sourcer (chercher dans `Source/`), soit la supprimer.
4. **Réorganiser selon les sections du template** correspondant.
5. **Supprimer** : blockquote-pitch d'intro, tags, section Hooks générique, section Liens en pied, wikilinks autour des entités.
6. **Vérifier les noms complets** pour les popovers : remplacer "Holzkrug" → "Liepmund Holzkrug", etc.
7. **Ajouter `**Sous-titre** :`** et `**Statut** :` après le titre.
8. **Vérifier chaque ref canon** : ouvrir le fichier source à la ligne citée, confirmer que le contenu est pertinent. Si la ligne tombe sur un heading orphelin ou une table cassée, chercher une ligne alternative.
9. **Re-builder** (`python _site_build.py`) et **vérifier** sur la page rendue :
   - Tous les popovers d'entité se déclenchent au survol (pas de nom non lié).
   - Toutes les refs canon affichent un extrait pertinent.
   - Pas de doublon visible.
10. **Audit critique honnête** : section par section, "à quel cas d'usage MJ ou Claude sert-elle ?". Si une section ne sert à rien, la supprimer.

## Commandes de vérification utiles

```bash
# Tester un fichier source pour confirmer le contenu d'une ligne
awk 'NR>=X && NR<=Y {print NR": "$0}' "Source/<book>/<chapter>.md"

# Lister tous les refs canon dans une fiche
grep -oE '`(EiR|EiS|DoR|PBT|HR|...)[^`]+`' "Notes MJ/PNJ/<Fiche>.md"

# Vérifier les noms complets utilisés (popover trigger)
grep -oE 'class="entity-pop"[^>]*data-title="[^"]+"' "_site/pnj/<fiche>.html" | sort -u

# Détecter les refs orphelines (qui ne matchent rien)
# (lancer python _site_build.py ; les refs cassées apparaissent comme <span> au lieu de <a>)
grep -E 'class="canon-ref"[^>]*>(?!<sup>)' "_site/pnj/<fiche>.html"
```

## Quand le contenu canon est cassé

Le `.md` issu de la conversion PDF peut être désordonné (paragraphes orphelins) ou contenir des tables écrabouillées en `<br>`. Le script `_convert_pdfs.py` fait une passe `_splice_clean_tables` pour reconstituer les tables, mais ne réorganise pas les paragraphes.

Pour les refs qui tombent sur du contenu cassé :
1. Chercher si les paragraphes lisibles équivalents existent **avant** ou **après** la zone cassée.
2. Re-pointer la ref vers ces paragraphes (par ex. `l.302` (table cassée) → `l.282-299` (paragraphes explicatifs lisibles)).
3. Si nécessaire, lancer `python _convert_pdfs.py --force --only "<book>.pdf"` pour re-générer le `.md` avec le script amélioré (les backups sont créés en `.md.before-fix`).

## Workflow d'audit critique

Une fois la fiche rédigée ou migrée, **avant de la considérer comme
terminée**, mener l'audit critique suivant. C'est plus strict que la
checklist qualité initiale : on cherche activement les défauts.

### Étape 1 — Inventaire de la fiche rendue

Lancer `python _site_build.py`, ouvrir la page MJ rendue de la fiche, et faire :

```python
import re
with open('_site/<categorie>/<fiche-slug>.html', encoding='utf-8') as f:
    html = f.read()
m = re.search(r'<section class="mj-only mj-entity-enrichment">(.+?)</section>', html, re.DOTALL)
body = m.group(1)
print(f"Canon refs : {len(re.findall(r'class=.canon-ref.', body))}")
print(f"Popovers entité : {len(re.findall(r'class=.entity-pop.', body))}")
print("Sections :")
for h in re.findall(r'<h2[^>]*>([^<]+)</h2>', body):
    print(f"  - {h.strip()}")
```

### Étape 2 — Audit popovers entité

Lister toutes les entités déclenchées (`grep -oE 'data-title="[^"]+"' fiche.html | sort -u`).

Pour chaque popover, vérifier :

- [ ] **L'entité existe-t-elle vraiment ?** (pas un faux positif sur sous-mot).
- [ ] **Le matching est-il correct ?** (par ex. "Wolfgang Holswig-Abenauer" ne doit PAS pointer vers la fiche "Wolfgang Kellermann").
- [ ] **Le sous-titre / portrait s'affiche-t-il ?** (data-subtitle / data-portrait non vides).
- [ ] **Le clic mène-t-il à la bonne fiche ?** (href correct).

Si une entité est mentionnée dans le texte mais n'a pas de popover : soit forme courte (à corriger en nom complet), soit fiche manquante (à créer selon "Création de fiches pour entités absentes").

### Étape 3 — Audit refs canon

Pour chaque ref canon de la fiche, **survoler le superscript** sur la page rendue et vérifier l'extrait.

Critères de validité d'un extrait :

- [ ] **Pertinent** : l'extrait parle bien du sujet cité (pas un heading orphelin, pas une bouillie OCR).
- [ ] **Complet** : l'extrait montre suffisamment de contexte (pas un seul mot tronqué).
- [ ] **Cohérent** : l'affirmation dans la fiche correspond bien à ce qui est dit dans l'extrait (pas une affirmation déformée).

Si l'extrait est cassé :
1. Vérifier la ligne sur le fichier source (`awk 'NR>=X && NR<=Y' Source/.../<file>.md`).
2. Si le contenu canon existe mais à une autre ligne : corriger la ref.
3. Si la zone est OCR-cassée : chercher la ref alternative qui pointe aux paragraphes lisibles équivalents.
4. Si le canon ne dit pas ce qu'on prétend : retirer ou reformuler l'affirmation.

### Étape 4 — Audit fonctionnel par section

Section par section, se poser pour chacune :

- [ ] **À quel cas d'usage** (MJ live / MJ prep / Claude futur) sert-elle ?
- [ ] **Si elle est supprimée, qu'est-ce qui devient impossible ?** Si la réponse est "rien", la section est inutile.
- [ ] **Y a-t-il une duplication** avec une autre section de la fiche ?
- [ ] **Le contenu est-il du canon sourcé** ou de l'invention non drapeautée ?

Toute section qui ne passe pas l'audit fonctionnel doit être **soit reformulée pour devenir utile, soit supprimée**. Pas de bavardage MJ déguisé en canon.

### Étape 5 — Test "lecture froide"

Si possible, faire lire la fiche par quelqu'un (ou Claude) qui n'a pas
travaillé dessus :

- A-t-il compris **qui est cette entité** en 30 secondes ?
- Sait-il **ce qu'il faut savoir avant la prochaine session** ?
- Peut-il **citer** une source canon pour une affirmation clé ?
- Identifie-t-il des **doublons / incohérences** ?

### Anti-pattern d'audit

> "Telle autre fiche a la section X, donc cette fiche devrait l'avoir."

**Faux raisonnement**. Chaque section doit être justifiée par sa **fonction**, pas par sa présence ailleurs. Comparer avec d'autres fiches sert à découvrir des angles fonctionnels potentiels — pas à imposer un format uniforme par mimétisme.

### Critère final d'audit

> Une fiche passe l'audit si **chaque section est justifiée**, **chaque
> ref pointe sur un contenu pertinent**, **chaque popover entité se
> déclenche**, et **aucune affirmation n'est inventée sans drapeau**.

## Référence

- Fiche pilote PNJ : `Notes MJ/PNJ/Yann Zuntermein.md`
- Fiche pilote Lieu : `Notes MJ/Lieux/Volkshalle.md`
- Fiche pilote Faction : `Notes MJ/Factions/Spionwerber.md`
- Source FR canon : [Bibliothèque Impériale](https://bibliotheque-imperiale.com/) — à consulter pour toute traduction de terme canon
- Orthographe canon et corrections : `Notes MJ/Orthographe canon - corrections à appliquer.md`
- Builder : `_site_build.py` (`inject_canon_refs`, `inject_entity_popovers`, `_extract_mj_entity_metadata`, `_MJ_MANUAL_ALIASES`)
