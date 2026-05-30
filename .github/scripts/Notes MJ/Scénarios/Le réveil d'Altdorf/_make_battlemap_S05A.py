"""Génère un schéma top-down de la salle de lecture du temple de Verena pour la scène 13 (Arrestation et fuite) Phase A.

Sortie: 13 - Battlemap Salle de lecture.png (~1600x1200)

Conventions visuelles:
- Vue de dessus, style "blueprint MJ" : fond crème, traits noirs, annotations FR.
- Échelle indicative 1m.
- Repères Templiers (T), Veilleurs (V), Fassbinder (F), table cible (●).
- Sorties numérotées 1-5 avec libellés et difficulté.
"""

from PIL import Image, ImageDraw, ImageFont
import os

W, H = 1800, 1320
BG = (245, 238, 220)     # parchemin crème
WALL = (40, 30, 22)      # mur noir-brun
INK = (40, 30, 22)
TABLE = (180, 145, 95)
TABLE_OUTLINE = (110, 80, 40)
SHELF = (90, 65, 45)
SHELF_FILL = (160, 130, 90)
TARGET = (170, 30, 30)   # rouge pour table Fassbinder + casier
TARGET_LIGHT = (220, 150, 150)
TEMPLAR = (130, 30, 30)  # pourpre Templiers
WATCH = (40, 70, 140)    # bleu Watch
EXIT_OK = (40, 110, 60)  # vert sortie libre
EXIT_BAD = (170, 30, 30) # rouge sortie bloquée
EXIT_RISK = (200, 130, 30) # orange sortie risquée
ANNOT = (40, 30, 22)
GRID = (200, 190, 170)

img = Image.new("RGB", (W, H), BG)
d = ImageDraw.Draw(img)


def load_font(size, bold=False):
    candidates = [
        r"C:\Windows\Fonts\georgiab.ttf" if bold else r"C:\Windows\Fonts\georgia.ttf",
        r"C:\Windows\Fonts\arialbd.ttf" if bold else r"C:\Windows\Fonts\arial.ttf",
    ]
    for path in candidates:
        if os.path.exists(path):
            try:
                return ImageFont.truetype(path, size)
            except OSError:
                continue
    return ImageFont.load_default()


F_TITLE = load_font(36, bold=True)
F_H2 = load_font(22, bold=True)
F_BODY = load_font(18)
F_SMALL = load_font(14)
F_TAG = load_font(16, bold=True)


def text(xy, s, font=F_BODY, fill=INK, anchor="lt"):
    d.text(xy, s, font=font, fill=fill, anchor=anchor)


# --- Cadre extérieur de la pièce ---
# Salle rectangulaire orientée sud (entrée) -> nord (fond), proportions ~20m x 14m.
# Coordonnées dessin: y croît vers le bas. Sud = bas du dessin, Nord = haut.
ROOM = (300, 290, 1300, 1100)  # left, top, right, bottom (room shifted right & down for margins)
x0, y0, x1, y1 = ROOM

# Murs
d.rectangle(ROOM, outline=WALL, width=6)

# Grille discrète 1m (50 px = 1m)
SCALE = 50
for gx in range(x0, x1 + 1, SCALE):
    d.line([(gx, y0), (gx, y1)], fill=GRID, width=1)
for gy in range(y0, y1 + 1, SCALE):
    d.line([(x0, gy), (x1, gy)], fill=GRID, width=1)
# Re-trace murs sur grille
d.rectangle(ROOM, outline=WALL, width=6)


# --- Sorties (numérotées) ---
# 1. Porte principale (nef) - sud, bloquée par 2 Veilleurs du Watch
d.rectangle([(x0 + 420, y1 - 6), (x0 + 580, y1 + 30)], fill=BG, outline=EXIT_BAD, width=5)
text((x0 + 500, y1 + 50), "(1) Porte principale — nef", font=F_TAG, fill=EXIT_BAD, anchor="mm")
text((x0 + 500, y1 + 75), "BLOQUÉE — 2 Veilleurs du Watch", font=F_SMALL, fill=EXIT_BAD, anchor="mm")

# 2. Porte de service arrière - nord, libre (cloître intérieur)
d.rectangle([(x0 + 440, y0 - 30), (x0 + 560, y0 + 6)], fill=BG, outline=EXIT_OK, width=5)
text((x0 + 500, y0 - 55), "(2) Porte de service arrière", font=F_TAG, fill=EXIT_OK, anchor="mm")
text((x0 + 500, y0 - 80), "Libre  •  Discrétion +0  •  → cloître", font=F_SMALL, fill=EXIT_OK, anchor="mm")

# 3. Escalier mezzanine - est
d.rectangle([(x1 - 6, y0 + 280), (x1 + 30, y0 + 400)], fill=BG, outline=EXIT_OK, width=5)
text((x1 + 50, y0 + 320), "(3) Escalier mezzanine", font=F_TAG, fill=EXIT_OK, anchor="lm")
text((x1 + 50, y0 + 350), "Disc. -10  • toits cloître", font=F_SMALL, fill=EXIT_OK, anchor="lm")
text((x1 + 50, y0 + 372), "(saut 2-4 m selon voie)", font=F_SMALL, fill=EXIT_OK, anchor="lm")

# 4. Porte annexe (salle des manuscrits) - ouest, près du casier
d.rectangle([(x0 - 30, y0 + 360), (x0 + 6, y0 + 480)], fill=BG, outline=EXIT_OK, width=5)
text((x0 - 50, y0 + 400), "(4) Porte annexe", font=F_TAG, fill=EXIT_OK, anchor="rm")
text((x0 - 50, y0 + 425), "→ salle des manuscrits", font=F_SMALL, fill=EXIT_OK, anchor="rm")
text((x0 - 50, y0 + 447), "Disc. +0  • ruelle copistes", font=F_SMALL, fill=EXIT_OK, anchor="rm")

# 5. Fenêtres rez-de-chaussée - nord (jardins cloître)
for fx in (x0 + 120, x0 + 220, x0 + 700, x0 + 820):
    d.rectangle([(fx, y0 - 4), (fx + 60, y0 + 4)], fill=BG, outline=EXIT_RISK, width=3)
text((x0 + 170, y0 - 30), "(5) Fenêtres cloître", font=F_TAG, fill=EXIT_RISK, anchor="mm")
text((x0 + 170, y0 - 52), "Bruit modéré  • Athl. +0", font=F_SMALL, fill=EXIT_RISK, anchor="mm")
text((x0 + 760, y0 - 30), "(5) Fenêtres cloître", font=F_TAG, fill=EXIT_RISK, anchor="mm")


# --- Rayonnages (étagères de livres) ---
# Rangées internes parallèles sud-nord, créent des allées entre lesquelles se faufiler
shelf_w, shelf_h = 60, 280
shelves_rows = [
    # (x, y, label) deux rangées de rayonnages au centre + une rangée près des murs est/ouest
    (x0 + 100, y0 + 80, "Rayonnages"),
    (x0 + 100, y0 + 480, "Rayonnages"),
    (x0 + 340, y0 + 80, "Rayonnages"),
    (x0 + 580, y0 + 80, "Rayonnages"),
    (x0 + 820, y0 + 80, "Rayonnages"),
    (x0 + 820, y0 + 480, "Rayonnages"),
]
for sx, sy, lbl in shelves_rows:
    d.rectangle([(sx, sy), (sx + shelf_w, sy + shelf_h)], fill=SHELF_FILL, outline=SHELF, width=2)


# --- Tables de lecture (lecteurs ordinaires) ---
def draw_table(cx, cy, w=80, h=50, color=TABLE):
    d.rectangle([(cx - w // 2, cy - h // 2), (cx + w // 2, cy + h // 2)], fill=color, outline=TABLE_OUTLINE, width=2)

# Disposées en quinconce dans l'allée centrale (entre rayonnages)
reader_tables = [
    (x0 + 270, y0 + 200), (x0 + 270, y0 + 330), (x0 + 270, y0 + 470),
    (x0 + 510, y0 + 200), (x0 + 510, y0 + 330), (x0 + 510, y0 + 470),
    (x0 + 750, y0 + 200), (x0 + 750, y0 + 470),
    (x0 + 270, y0 + 620), (x0 + 510, y0 + 620), (x0 + 750, y0 + 620),
    (x0 + 270, y0 + 720), (x0 + 510, y0 + 720), (x0 + 750, y0 + 720),
]
for tx, ty in reader_tables:
    draw_table(tx, ty)
    # petit point lecteur
    d.ellipse([(tx - 6, ty - 6), (tx + 6, ty + 6)], fill=(60, 50, 40))


# --- TABLE FASSBINDER (cible #1) ---
F_TABLE = (x0 + 750, y0 + 330)
draw_table(*F_TABLE, w=100, h=60, color=TARGET_LIGHT)
d.rectangle([(F_TABLE[0] - 50, F_TABLE[1] - 30), (F_TABLE[0] + 50, F_TABLE[1] + 30)], outline=TARGET, width=4)
# Fassbinder (F)
d.ellipse([(F_TABLE[0] - 12, F_TABLE[1] - 12), (F_TABLE[0] + 12, F_TABLE[1] + 12)], fill=TARGET, outline=WALL, width=2)
text(F_TABLE, "F", font=F_TAG, fill=(255, 255, 255), anchor="mm")
text((F_TABLE[0] + 60, F_TABLE[1] - 10), "Table Fassbinder", font=F_TAG, fill=TARGET, anchor="lm")
text((F_TABLE[0] + 60, F_TABLE[1] + 12), "Yodri vol.1 · notes · cahier", font=F_SMALL, fill=TARGET, anchor="lm")


# --- CASIER / ALCÔVE DES CHERCHEURS (cible #2) ---
LOCKER = (x0 + 60, y0 + 420)
d.rectangle([(LOCKER[0] - 25, LOCKER[1] - 35), (LOCKER[0] + 25, LOCKER[1] + 35)],
            fill=TARGET_LIGHT, outline=TARGET, width=3)
text((LOCKER[0], LOCKER[1]), "Casier", font=F_SMALL, fill=TARGET, anchor="mm")
text((LOCKER[0], LOCKER[1] + 50), "Crochetage -20", font=F_SMALL, fill=TARGET, anchor="mm")
text((LOCKER[0], LOCKER[1] + 70), "(clés au cou de F)", font=F_SMALL, fill=TARGET, anchor="mm")

# Ligne pointillée Fassbinder → casier (10 m)
def dashed_line(p1, p2, fill, width=2, dash=10, gap=6):
    x1d, y1d = p1
    x2d, y2d = p2
    import math
    dx, dy = x2d - x1d, y2d - y1d
    length = math.hypot(dx, dy)
    if length == 0:
        return
    ux, uy = dx / length, dy / length
    pos = 0
    while pos < length:
        a = (x1d + ux * pos, y1d + uy * pos)
        b_pos = min(pos + dash, length)
        b = (x1d + ux * b_pos, y1d + uy * b_pos)
        d.line([a, b], fill=fill, width=width)
        pos += dash + gap

dashed_line(F_TABLE, LOCKER, TARGET, width=2)
# distance label
mid = ((F_TABLE[0] + LOCKER[0]) // 2, (F_TABLE[1] + LOCKER[1]) // 2 - 14)
text(mid, "~10 m  •  Discrétion -10", font=F_SMALL, fill=TARGET, anchor="mm")


# --- TEMPLIERS ---
def draw_marker(cx, cy, label, color, r=14):
    d.ellipse([(cx - r, cy - r), (cx + r, cy + r)], fill=color, outline=WALL, width=2)
    text((cx, cy), label, font=F_TAG, fill=(255, 255, 255), anchor="mm")

# T1 = Templier Répurgateur senior dans l'allée centrale, avance vers Fassbinder
draw_marker(x0 + 510, y1 - 120, "T1", TEMPLAR, r=16)
text((x0 + 510, y1 - 90), "Répurgateur senior", font=F_SMALL, fill=TEMPLAR, anchor="mm")

# T2-T5 = 4 Initiés/Zélotes qui se déploient pour bloquer les allées
for cx, cy in [(x0 + 350, y1 - 200), (x0 + 670, y1 - 200), (x0 + 400, y1 - 60), (x0 + 620, y1 - 60)]:
    draw_marker(cx, cy, "T", TEMPLAR, r=12)
text((x0 + 350, y1 - 175), "Initié", font=F_SMALL, fill=TEMPLAR, anchor="mm")
text((x0 + 670, y1 - 175), "Initié", font=F_SMALL, fill=TEMPLAR, anchor="mm")

# V1, V2 = 2 Veilleurs du Watch à la porte principale
draw_marker(x0 + 445, y1 - 25, "V", WATCH, r=12)
draw_marker(x0 + 555, y1 - 25, "V", WATCH, r=12)
text((x0 + 500, y1 - 5), "Veilleurs", font=F_SMALL, fill=WATCH, anchor="mm")


# --- Flèche de direction d'avance du Templier senior ---
def arrow(p1, p2, color, width=3):
    d.line([p1, p2], fill=color, width=width)
    import math
    dx, dy = p2[0] - p1[0], p2[1] - p1[1]
    angle = math.atan2(dy, dx)
    a1 = (p2[0] - 18 * math.cos(angle - 0.35), p2[1] - 18 * math.sin(angle - 0.35))
    a2 = (p2[0] - 18 * math.cos(angle + 0.35), p2[1] - 18 * math.sin(angle + 0.35))
    d.polygon([p2, a1, a2], fill=color)

arrow((x0 + 510, y1 - 105), (F_TABLE[0] - 30, F_TABLE[1] + 40), TEMPLAR, width=3)


# --- Légende / panneau latéral ---
LEG_X = x1 + 130
LEG_Y = y0 + 480
d.rectangle([(LEG_X - 10, LEG_Y - 10), (LEG_X + 280, LEG_Y + 380)],
            outline=WALL, width=2, fill=(252, 247, 232))
text((LEG_X, LEG_Y), "Légende", font=F_H2, anchor="lt")
ly = LEG_Y + 36

def legend_row(y, color, lbl, sub=None, square=False):
    if square:
        d.rectangle([(LEG_X + 4, y + 4), (LEG_X + 24, y + 24)], fill=color, outline=WALL, width=2)
    else:
        d.ellipse([(LEG_X + 4, y + 4), (LEG_X + 24, y + 24)], fill=color, outline=WALL, width=2)
    text((LEG_X + 36, y + 14), lbl, font=F_BODY, anchor="lm")
    if sub:
        text((LEG_X + 36, y + 34), sub, font=F_SMALL, anchor="lm", fill=(80, 60, 40))

legend_row(ly, TEMPLAR, "Templiers (1+4)"); ly += 50
legend_row(ly, WATCH, "Veilleurs (2) — porte sud"); ly += 30
legend_row(ly, TARGET, "F = Fassbinder + table", square=True); ly += 30
legend_row(ly, TARGET_LIGHT, "Objectifs (table, casier)", square=True); ly += 30
legend_row(ly, EXIT_OK, "Sortie libre", square=True); ly += 30
legend_row(ly, EXIT_RISK, "Sortie bruyante", square=True); ly += 30
legend_row(ly, EXIT_BAD, "Sortie bloquée", square=True); ly += 30
legend_row(ly, SHELF_FILL, "Rayonnages (couvert)", square=True); ly += 30
legend_row(ly, TABLE, "Tables lecteurs (12-15)", square=True); ly += 30


# --- Titre + cartouche fenêtre tactique ---
text((W // 2, 50), "Bibliothèque-temple de Verena — Salle de lecture", font=F_TITLE, anchor="mm")
text((W // 2, 95), "Arrestation publique de Fassbinder (scène 13, Phase A) · ~11h", font=F_BODY, anchor="mm")
text((W // 2, 125), "Fenêtre tactique : 60-90 sec table  /  25-30 sec in-game", font=F_TAG, fill=(140, 30, 30), anchor="mm")

# Nord
text((x1 + 50, y0 - 30), "N", font=F_TITLE, anchor="lm")
arrow((x1 + 65, y0 + 40), (x1 + 65, y0 - 10), WALL, width=3)

# Échelle
SC_X, SC_Y = x0 + 40, y1 + 110
d.line([(SC_X, SC_Y), (SC_X + 250, SC_Y)], fill=WALL, width=3)
for i in range(6):
    d.line([(SC_X + 50 * i, SC_Y - 6), (SC_X + 50 * i, SC_Y + 6)], fill=WALL, width=2)
text((SC_X + 125, SC_Y + 22), "5 m  (1 case = 1 m)", font=F_SMALL, anchor="mm")


# --- Note bas de page ---
text((W // 2, H - 30),
     "Topologie : voir 13 - Arrestation et fuite §Phase A & Phase B.1   •   schéma indicatif, ajuster table",
     font=F_SMALL, anchor="mm", fill=(100, 80, 50))

out = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                   "13 - Battlemap Salle de lecture.png")
img.save(out, "PNG", optimize=True)
print(f"Wrote {out}")
