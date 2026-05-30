# Template — scénario navigable (outil de table)

> Convention pour structurer un scénario homebrew jouable **en direct à la table**, pas seulement lisible. Pilote de référence : [[Hub|Le réveil d'Altdorf]] (refonte 2026-05-30).
>
> **Trois métiers, trois supports — ne pas les mélanger** :
> 1. **Dispatch** (où vont les PJ) → le **Hub**, et rien d'autre.
> 2. **Navigation** (atteindre chaque page) → la **sidebar gauche du site** (auto-générée, groupée par thread).
> 3. **Référence live / de fond** (par heure, variantes, pacing, juridictions…) → des **pages dédiées**.
>
> Quand le Hub fait les trois à la fois, il gonfle et cesse d'être un hub. Le test : *un joueur annonce une intention → le MJ trouve en 3 secondes où aller.*

## Les douleurs de table à éviter

1. **« À qui parler pour X ? »** introuvable → info rangée par lieu/trigger, pas par intention.
2. **« On va à Y »** sans trajet/ambiance par destination → arrivée au mauvais endroit.
3. **Un même beat raconté deux fois** dans deux fichiers, dialogues contradictoires → on ne sait pas lequel jouer.
4. **Un Hub fourre-tout** → trop d'info, plus un hub.
5. **Une action proposée aux PJ sans rien de scénarisé en face** (promesse non tenue).

## Structure cible

### 1. `Hub.md` = page d'aiguillage **maigre**

Le Hub ne contient que :
- **En-tête** : 3-4 lignes (date, point de départ, durée, comment lire).
- **Composition table** : 2-3 lignes (PJ, hors-champ, rattrapage absent).
- **🧭 Index d'intentions — « les PJ veulent… »** *(le cœur)* : table `Les PJ veulent… | Où / Qui | Scène`. Une ligne par intention plausible (objectifs reçus + émergentes). Le joueur annonce → on lit la ligne → on ouvre la scène.

**C'est tout.** Pas de table Horloge, Destinations, Juridictions, Variables, Triggers, Flux, ni liste de pages : ça vit ailleurs (ci-dessous). La nav inter-pages, c'est la sidebar.

### 2. La **sidebar gauche** porte la navigation

`_site_build.py` génère, pour chaque page de scénario, un menu gauche dédié (`_render_scenario_sidebar`) : les pages du scénario **groupées par thread** (le préfixe numérique pilote le groupe), Hub épinglé en haut, page courante surlignée. → Le Hub n'a donc **jamais** à lister les pages ni les triggers.

### 3. **Écran live** = un seul doc « par heure » à garder ouvert

Un fichier (pilote : [[Ambiance]]) consolide tout ce qui se passe **autour** des PJ, sur une page : **table maîtresse par tranche horaire** (ville ‖ rumeurs ‖ traque) + **horloge in-game** (événements NPC à heure fixe) + **déplacements & destinations** + **pools de rumeurs** + **heat-clock de la traque** (avec angles morts). Évite trois docs à feuilleter en séance.

### 4. `## En bref` en tête de **chaque** scène

Juste après le bloc `> Lieu / Quand / Durée`, avant la prose :

```
## En bref
- **Objectif** : <ce que la scène apporte, 1 phrase>
- **PNJ présents** : <liste, wikilinks [[Nom]]>
- **Ce que les PJ peuvent faire ici** : <puces, une par jet/option DISPONIBLE — situations, pas tâches assignées>
- **Sorties / et après** : → <scène suivante> · → retour [[Hub]]
```

Ne **jamais scripter** d'action PJ : lister situations + jets disponibles ([[feedback_ne-pas-scripter-pj]], [[feedback_situations-jets-pas-roles]]).

### 5. Couche de référence (fichiers nommés, pas numérotés)

**Écran live** (ambiance/rumeurs/traque/horloge/déplacements), **Carte des juridictions** (si voies officielles), **Pacing** (flux par défaut + ce que les PJ savent par palier + table 3h), **Gestion table** (variantes globales + état à tracer + cas particuliers + XP), **Cap suivant**.

## Règles d'or

- **Le Hub reste un hub.** Toute table qui n'est pas l'index d'intentions → la déporter vers sa page et la laisser à un clic dans la sidebar.
- **Un beat = un seul propriétaire.** Si un événement (arrestation, embuscade, bascule) est partagé par deux scènes, **une seule** le met en scène (dialogues compris) ; l'autre **passe un état** et renvoie (« → joue X en lui indiquant l'état A/B »). Jamais deux mises en scène concurrentes.
- **Pas de promesse non tenue.** Toute action/lieu/jet proposé doit avoir **quelque chose en face** (issue, réaction PNJ, info, conséquence, où ça mène), même une ligne. Le style « situations + jets » ne dispense pas d'un résultat.
- **Numérotation unique, par thread.** Un seul système (les numéros de fichier), groupés par fil (`0x` ouverture, `1x`/`2x`/… par arc thématique). Le préfixe **pilote aussi les groupes de la sidebar**. Pas de double système « Scène 3 / Module 5 » en prose → wikilinks descriptifs (`[[51 - Glissement Reikerbahn|le Glissement]]`). Fichiers de **référence en noms** (Hub, Ambiance, Pacing…), les plus liés.

## Vérifications (à passer avant publication)

1. **Liens** : tous les wikilinks du dossier résolvent vers un `.md` existant (sinon stub volontaire ou typo). Grep aussi les ancres `[[Hub#…]]`/refs en prose (« scène 04 », « Module N ») après tout renommage/déplacement de section.
2. **Promesses non tenues** : relire chaque scène et lister les actions proposées **sans rien en face** ; combler. *(Un audit de liens ne le détecte PAS.)*
3. **Rendu** : `python _site_build.py` (liens cassés en rouge) ; preview navigateur (sidebar + Hub maigre).

## Après une renumérotation

1. Backup du dossier.
2. Renames two-phase (anti-collision) + propagation **textuelle** `[[old]]`→`[[new]]` dans tout `Notes MJ` (script jetable).
3. Grep refs en prose + ancres de section cassées.
4. Vérif liens + promesses non tenues (ci-dessus).
5. Déploiement : `python _site_build.py --clean --deploy` puis `python _sync_to_pipeline.py` + commit/push (sinon le cron rebuild depuis une source périmée et annule la refonte).

## Liens

- Pilote : [[Hub|Le réveil d'Altdorf — Hub]], [[Ambiance]] (écran live), [[Carte des juridictions]], [[Gestion table]], [[Pacing]].
- Template fiches PNJ/Lieux/Factions : [[00 - Template fiches MJ]].
