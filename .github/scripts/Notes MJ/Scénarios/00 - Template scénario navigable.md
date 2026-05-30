# Template — scénario navigable (outil de table)

> Convention pour structurer un scénario homebrew jouable **en direct à la table**, pas seulement lisible. Pilote de référence : [[Hub|Le réveil d'Altdorf]] (refonte 2026-05-30).
>
> Principe : un scénario bac-à-sable n'est pas un document de référence rangé par lieu — c'est une **console de répartition** où, dès qu'un joueur annonce une intention, le MJ trouve en 3 secondes *où aller, qui voir, quelle scène ouvrir*.

## Les trois douleurs de table à éviter

1. **« À qui parler pour X ? »** introuvable → l'info est rangée par lieu/trigger, pas par intention joueur.
2. **« On va à Y »** sans trajet/ambiance par destination → le MJ improvise mal ou fait arriver les PJ au mauvais endroit.
3. **Un même beat raconté deux fois** dans deux fichiers, avec des variantes contradictoires → le MJ ne sait pas lequel jouer.

## Structure cible

### 1. Un fichier `Hub.md` = console de répartition

Sections, dans cet ordre d'utilité-table :

- **En-tête** : 3-4 lignes (date, point de départ, durée, l'ordre de lecture des sections).
- **⏱ Horloge in-game** : table des événements à horaire fixe, NPC-driven (ce qui tombe tout seul, indépendamment des PJ).
- **🧭 Index d'intentions — « les PJ veulent… »** *(le cœur)* : table `Les PJ veulent… | Où / Qui | Scène`. Une ligne par intention plausible (objectifs reçus + intentions émergentes). C'est le réflexe : le joueur annonce → on lit la ligne → on ouvre la scène.
- **🗺 Destinations & déplacements** : table par lieu nommé (`Lieu | rive/district | comment y aller | arrivée en 1 ligne d'ambiance`). Renvoi au fichier Ambiance pour le détail.
- **⚖ Carte des juridictions** (si le scénario a des voies officielles) : `Le PJ veut… | Autorité | Accès | aboutit dans la journée ?`.
- **🎚 Variables d'état** : ce que le MJ trace mentalement (réputations, flags qui changent les variantes).
- **Triggers** : conditions de déclenchement de chaque scène.
- **Flux par défaut & pacing** : l'orientation suggérée (pas un railroad) + ce que les PJ savent à chaque palier.
- **Liens** : pages de référence transversales.

### 2. En-tête `## En bref` en tête de CHAQUE scène

Juste après le bloc d'intro `> **Lieu** / Moment / Durée`, avant la prose :

```
## En bref

- **Objectif** : <ce que la scène apporte, 1 phrase>
- **PNJ présents** : <liste, wikilinks [[Nom]]>
- **Ce que les PJ peuvent faire ici** : <puces, une par jet/option DISPONIBLE — situations, pas tâches assignées>
- **Sorties / et après** : → <scène suivante> · → retour [[Hub]]
```

C'est le résumé que le MJ lit en ouvrant la scène. Il ne **scripte jamais** d'action PJ : il liste des situations et des jets disponibles ([[feedback_ne-pas-scripter-pj]], [[feedback_situations-jets-pas-roles]]).

### 3. Couche de référence transversale (fichiers nommés, pas numérotés)

- **Ambiance** : déplacements généralisés (trajets par destination, arrivée, escalation horaire, factions de rue). Évite de câbler le trajet sur une seule destination.
- **Carte des juridictions** (si pertinent) : qui traite quel type de requête officielle.
- **Rumeurs**, **Pacing**, **Gestion table** (variantes globales + cas particuliers + XP), **Cap suivant**.

## Règles anti-duplication

- **Un beat = un seul propriétaire.** Si un événement (arrestation, embuscade, bascule) est partagé par deux scènes, **une seule** le met en scène (dialogues compris) ; l'autre **passe un état** et renvoie (« → joue X en lui indiquant l'état A/B »). Jamais deux mises en scène concurrentes.
- **Numérotation unique.** Un seul système de numéros (les numéros de fichier). Pas de double système « Scène 3 / Module 5 » en prose à côté des numéros de fichier — utiliser des wikilinks descriptifs (`[[51 - Glissement Reikerbahn|le Glissement]]`).
- **Numéroter par thread** : grouper les fichiers par fil d'intention (ex. `1x` marteau, `2x` officiel, `3x` empereur, `4x` ville, `5x` déplacement, `6x` départ). Garder les fichiers de **référence en noms** (Hub, Ambiance, Rumeurs, Pacing…) car ce sont les plus liés.

## Après une renumérotation

1. Backup du dossier.
2. Renames two-phase (anti-collision) + propagation **textuelle** de tous les `[[old]]`→`[[new]]` dans tout `Notes MJ` (script jetable).
3. Grep des refs **en prose** non captées par le stem (« scène 04 », « Scène 3 », « Module N »).
4. Grep des ancres `[[Hub#…]]` cassées par un renommage de section.
5. Vérif statique : tous les wikilinks du dossier résolvent vers un `.md` existant (sinon stub volontaire ou typo à corriger).
6. `python _site_build.py` si déploiement voulu (rend les liens cassés en rouge).

## Liens

- Pilote : [[Hub|Le réveil d'Altdorf — Hub]], [[Ambiance]], [[Carte des juridictions]].
- Template fiches PNJ/Lieux/Factions : [[00 - Template fiches MJ]].
