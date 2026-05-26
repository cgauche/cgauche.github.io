"""Strict English-fragment scanner for migrated MJ fiches.

Usage:
    python _audit_anglicismes.py [path/to/fiche.md] [path/to/fiche2.md ...]
    python _audit_anglicismes.py                # scan default set (last batch migrated)

What it catches beyond `whilst`/`baronial`/`scolar`:
- VO citations (`*« ... »*`) outside Phrases canon
- Single-word italic English (`*listlessness*`, `*setting*`)
- Faux amis (`malpractice`, `cult` sans accent, `suggestibility`, `outlawer`)
- Anglicisms in H2 titles
- Common English suffixes (-ing/-ness/-ship/-ought/-ould) not in WFRP system list
- Composite English place/title patterns left untranslated (Council of X, Black X)

What it doesn't catch (rely on 2e passe humaine):
- Narrator paraphrases hidden mid-sentence (`*« the civil war ends soon »*` inline)
- Subtle false friends in context (e.g. `gestion` vs `management`)
- Word-order anglicisms (`Sigmarite partisan` vs `partisan Sigmarite`)
"""
import re, sys, os
sys.stdout.reconfigure(encoding='utf-8')

# Default file set if none passed — adjust when starting a new batch
DEFAULT_FILES = [
    'Notes MJ/PNJ/Prince Luitpold.md',
    'Notes MJ/PNJ/Ludwig Schwartzhelm.md',
    'Notes MJ/Documents/Ghal Maraz.md',
    'Notes MJ/Lieux/Palais Impérial.md',
    'Notes MJ/Lieux/Reikland.md',
    'Notes MJ/Lieux/Mordheim.md',
    'Notes MJ/Factions/Champions du Marteau.md',
]

# Faux amis et anglicismes confirmés (bug words)
BUG_WORDS = re.compile(
    r'\b(?:cornered|backup|takeover|outburst|toady|scolar|whilst|malpractice|'
    r'suggestibility|baronial|preceptor|setting|outlawer|nope|smolders|smolder|'
    r'Physicians?|Privy Council|High Chamberlain|Lady at Court|Council of State|'
    r'Council of Altdorf|Reikland Council|Wilhelm Chamber|Great Hospice|'
    r'Black Fire Pass|Black Rock Castle|Black Mountains|Grey Mountains|'
    r'sourcebook|NPC sheet|runesmithery|privies|Cult of the Possessed|'
    r'Champions of the Hammer|Caves of Chaos|High Lord (?:Steward|Treasurer|'
    r'Ambassador|Judge|Chancellor|Chamberlain|Constable|Admiral|of the Chair)|'
    r'Sleepless Skavens|Undead\b)\b',
    re.IGNORECASE,
)

# Mots autorisés en VO (système WFRP + noms propres canon)
SYSTEM_VO = {
    'reiksguard','reiksmarshall','reiksmarshal','spionwerber','graukappen',
    'magister','magistri','ordo','terribilis','septenarius','schemer',
    'vitality','draught','ranald','delight','schlafenkraut','moonflower',
    'fatigued','stunned','prone','surprised','unconscious','bleeding','broken',
    'sleight','charm','cool','dodge','athletics','bribery','perception',
    'endurance','intuition','leadership','stealth','channelling','heraldry',
    'politics','warfare','bretonnian','classical','wastelander','magick',
    'willpower','acute','etiquette','nobles','soldiers','servants','petty',
    'roughrider','wealthy','carouser','blather','commanding','presence','noble',
    'crowns','powder','parchment','rapier','dagger','cloak','clothing',
    'reach','average','quality','qualities','damaging','pummel','unbreakable',
    'magical','weapon','radiant','nimbus','unstable','wounds','smednir',
    'goblin','bane','impact','hobgoblin','fear','terror','ablaze','spell',
    'breaking','casting','resilience','fate','corruption','wound','combat',
    'aware','master','riposte','melee','fencing','trade','power','score',
    'lector','feeble','smile','wave','outdoor','survival','climb',
    'doomed','talents','skills','traits','trappings','spells','arcane','lore',
    'high','helm','helms','knights','knight','order',
    # ending/win acceptés comme noms canon de scènes ch.13
    'ending','endings','win','wins','lose','loss',
    # Mots français en -ing rares
    'shopping','meeting','briefing','planning','dressing','jogging',
    # Noms propres canon WHFB en -ing (faux amis suffixe)
    'helboring','hellboring','sterling','wenring','dolring',
    # Noms canon WHFB / Talents WFRP4 en -ing kept VO
    'changeling','lipreading','shining','speedreader','speedreading',
}


def scan(fpath: str) -> list[tuple[int, str, str]]:
    """Return list of (line_no, kind, snippet) for each suspect hit."""
    if not os.path.exists(fpath):
        return [(0, 'MISSING', fpath)]
    with open(fpath, encoding='utf-8') as f:
        md = f.read()

    # Strip sections that legitimately contain VO
    body = md
    for sec in ['## Phrases canon', '## Statbloc', '## Effets canon',
                '## Sanction']:
        idx = body.find(sec)
        if idx >= 0:
            nxt = body.find('\n## ', idx + 5)
            body = body[:idx] + (body[nxt:] if nxt != -1 else '')
    body = re.sub(r'`[^`]+`', '', body)              # canon refs
    body = re.sub(r'\[[^\]]+\]\([^)]+\)', '', body)  # links
    body = re.sub(r'\[\[[^\]]+\]\]', '', body)       # wikilinks

    hits: list[tuple[int, str, str]] = []

    # VO citations
    for m in re.finditer(r'\*«[^»]{6,}»\*|\*"[^"]{6,}"\*', body):
        q = m.group(0)
        if any(c in q for c in 'éèàçôîâêûï'):
            continue
        ln = body[:m.start()].count('\n') + 1
        hits.append((ln, 'VO', q[:70]))

    # Single-word italic lowercase
    for m in re.finditer(r'(?<!\*)\*([a-z][a-z\s\-]{4,})\*(?!\*)', body):
        s = m.group(1).strip()
        if any(c in s for c in 'éèàçôîâêûï'):
            continue
        if s.split()[0].lower() in SYSTEM_VO:
            continue
        ln = body[:m.start()].count('\n') + 1
        hits.append((ln, 'ITAL', f'*{s}*'))

    # Known bug words
    for m in BUG_WORDS.finditer(body):
        ln = body[:m.start()].count('\n') + 1
        hits.append((ln, 'BUG', m.group(0)))

    # English suffixes (-ing/-ness/-ship/-ought/-ould)
    for m in re.finditer(r'\b[A-Z]?[a-z]{2,}(?:ing|ness|ship|ould|ought)\b',
                         body):
        w = m.group(0)
        if any(c in w for c in 'éèàçôîâêûï'):
            continue
        if w.lower() in SYSTEM_VO:
            continue
        # Skip French -tion endings (occupation, gestion, ...)
        if w.lower().endswith('tion'):
            continue
        ln = body[:m.start()].count('\n') + 1
        hits.append((ln, 'SUFF', w))

    return hits


def main(argv: list[str]) -> int:
    files = argv[1:] if len(argv) > 1 else DEFAULT_FILES
    total_hits = 0
    for fpath in files:
        hits = scan(fpath)
        name = fpath.split('/')[-1]
        if not hits:
            print(f'OK {name}')
            continue
        print(f'\n=== {name} ({len(hits)} hits) ===')
        for ln, kind, snippet in hits:
            print(f'  L{ln} [{kind}] {snippet}')
        total_hits += len(hits)
    return 1 if total_hits else 0


if __name__ == '__main__':
    sys.exit(main(sys.argv))
