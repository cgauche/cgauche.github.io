"""Génère un schéma top-down de la rue déformée pour la scène 51 - Glissement Reikerbahn.

Sortie: 51 - Battlemap Glissement Reikerbahn.png (~1800x1320)

Conventions :
- Vue de dessus, fond parchemin légèrement plus terne que la bibliothèque (zone de bleed).
- Échelle 1 case = 1 m (30 px/m, soit 30 m de rue visible).
- Distance PJ↔Sigmarites = 30 m (canon).
- Ruelle latérale est avec fade-out (lengthening effect).
"""

from PIL import Image, ImageDraw, ImageFont
import os
import math

W, H = 1800, 1320
BG = (228, 222, 205)        # parchemin terne (zone de bleed)
COBBLE = (188, 178, 155)    # rue pavée
ALLEY_FADE = (170, 165, 150) # ruelle qui s'allonge
WALL = (40, 30, 22)
INK = (40, 30, 22)
BUILDING = (148, 130, 102)
BUILDING_OUT = (78, 62, 40)
ALCOVE = (118, 100, 75)
GRID = (208, 198, 180)
SIGMAR = (140, 30, 30)
BEGGAR = (95, 92, 88)
SILHOUETTE = (45, 40, 50)
PJ_COL = (40, 90, 50)
ANNOT_GREY = (90, 80, 60)
ARRIVAL = (40, 110, 60)
EFFACED = (140, 130, 110)

img = Image.new("RGB", (W, H), BG)
d = ImageDraw.Draw(img)


def load_font(size, bold=False):
    cand = [
        r"C:\Windows\Fonts\georgiab.ttf" if bold else r"C:\Windows\Fonts\georgia.ttf",
        r"C:\Windows\Fonts\arialbd.ttf" if bold else r"C:\Windows\Fonts\arial.ttf",
    ]
    for p in cand:
        if os.path.exists(p):
            try:
                return ImageFont.truetype(p, size)
            except OSError:
                continue
    return ImageFont.load_default()


F_TITLE = load_font(36, bold=True)
F_H2 = load_font(22, bold=True)
F_BODY = load_font(18)
F_SMALL = load_font(14)
F_TAG = load_font(16, bold=True)
F_FADE = load_font(20, bold=True)


def text(xy, s, font=F_BODY, fill=INK, anchor="lt"):
    d.text(xy, s, font=font, fill=fill, anchor=anchor)


SCALE = 30  # px par mètre

# Zone carte (gauche, haut, droite, bas)
MAP = (180, 200, 1380, 1180)
mx0, my0, mx1, my1 = MAP

# Rue principale verticale (sud→nord), 5 m de large
CX = 700  # centre x rue
STREET_W = 5 * SCALE       # 150 px = 5 m
SL = CX - STREET_W // 2    # rue gauche
SR = CX + STREET_W // 2    # rue droite

# Rue verticale s'étend du bas (PJ) au haut (Sigmarites)
STREET_TOP = my0 + 40
STREET_BOT = my1 - 40

# Ruelle latérale est (au milieu)
ALLEY_Y = (STREET_TOP + STREET_BOT) // 2 + 30
ALLEY_H = 3 * SCALE  # 3 m
ALLEY_TOP = ALLEY_Y - ALLEY_H // 2
ALLEY_BOT = ALLEY_Y + ALLEY_H // 2
ALLEY_LEFT = SR
ALLEY_RIGHT = mx1 - 60  # se prolonge jusqu'au bord droit

# --- Fond pavés rue ---
d.rectangle([(SL, STREET_TOP), (SR, STREET_BOT)], fill=COBBLE)
# Ruelle latérale - dégradé fade
ALLEY_GRAD_STEPS = 30
for i in range(ALLEY_GRAD_STEPS):
    x_a = ALLEY_LEFT + i * (ALLEY_RIGHT - ALLEY_LEFT) // ALLEY_GRAD_STEPS
    x_b = ALLEY_LEFT + (i + 1) * (ALLEY_RIGHT - ALLEY_LEFT) // ALLEY_GRAD_STEPS
    t = i / ALLEY_GRAD_STEPS
    c = tuple(int(COBBLE[k] * (1 - t) + BG[k] * t) for k in range(3))
    d.rectangle([(x_a, ALLEY_TOP), (x_b, ALLEY_BOT)], fill=c)

# --- Grille discrète sur la rue et la ruelle ---
for gx in range(SL, SR + 1, SCALE):
    d.line([(gx, STREET_TOP), (gx, STREET_BOT)], fill=GRID, width=1)
for gy in range(STREET_TOP, STREET_BOT + 1, SCALE):
    d.line([(SL, gy), (SR, gy)], fill=GRID, width=1)
for gx in range(ALLEY_LEFT, ALLEY_RIGHT + 1, SCALE):
    d.line([(gx, ALLEY_TOP), (gx, ALLEY_BOT)], fill=GRID, width=1)
for gy in range(ALLEY_TOP, ALLEY_BOT + 1, SCALE):
    d.line([(ALLEY_LEFT, gy), (ALLEY_RIGHT, gy)], fill=GRID, width=1)


# --- Bâtiments flanquant la rue (façades) ---
# Façades ouest : 3 blocs
west_blocks = [
    (mx0 + 80, STREET_TOP, SL, STREET_TOP + 280),
    (mx0 + 80, STREET_TOP + 290, SL, STREET_TOP + 560),
    (mx0 + 80, STREET_TOP + 570, SL, STREET_BOT),
]
# Façades est : 2 blocs avant ruelle, 2 après
east_blocks = [
    (SR, STREET_TOP, mx1 - 60, ALLEY_TOP - 10),
    (SR, ALLEY_BOT + 10, mx1 - 60, STREET_BOT),
]

for (a, b, c, dd) in west_blocks + east_blocks:
    d.rectangle([(a, b), (c, dd)], fill=BUILDING, outline=BUILDING_OUT, width=3)

# Hachures faibles sur les façades (toits) pour signifier "fermé"
def light_hatch(rect, color, spacing=14):
    a, b, c, dd = rect
    for off in range(-(dd - b), c - a, spacing):
        x_a = max(a, a + off)
        y_a = b + max(0, -off)
        x_b = min(c, a + off + (dd - b))
        y_b = dd - max(0, (a + off + (dd - b)) - c)
        if x_a < x_b and y_a < y_b:
            d.line([(x_a, y_a), (x_b, y_b)], fill=color, width=1)

for r in west_blocks + east_blocks:
    light_hatch(r, BUILDING_OUT, spacing=18)


# --- Portes/alcôves avec mendiants ---
def doorway(x, y, w=24, h=14, with_beggar=True, side="east"):
    """Petite alcôve avec mendiant accroupi."""
    if side == "east":  # alcôve dans façade ouest, ouverte vers la rue (vers l'est)
        d.rectangle([(x - w, y - h), (x, y + h)], fill=ALCOVE, outline=BUILDING_OUT, width=2)
        if with_beggar:
            d.ellipse([(x - 18, y - 8), (x - 4, y + 6)], fill=BEGGAR, outline=WALL, width=1)
    else:  # alcôve dans façade est, ouverte vers la rue (vers l'ouest)
        d.rectangle([(x, y - h), (x + w, y + h)], fill=ALCOVE, outline=BUILDING_OUT, width=2)
        if with_beggar:
            d.ellipse([(x + 4, y - 8), (x + 18, y + 6)], fill=BEGGAR, outline=WALL, width=1)


# Mendiants dans alcôves
doorway(SL, STREET_TOP + 160, side="east")   # ouest, haut
doorway(SL, STREET_TOP + 420, side="east")   # ouest, milieu (pas de mendiant ici)
doorway(SL, STREET_TOP + 680, side="east")   # ouest, bas
doorway(SR, STREET_TOP + 220, side="west")   # est, haut


# --- Sigmarites au nord (30 m de l'entrée PJ) ---
# PJ entrent par le sud (en bas) ; Sigmarites groupés au nord (en haut)
# 30 m × 30 px = 900 px => PJ à y = STREET_BOT - 30, Sigmarites à y = STREET_BOT - 30 - 900
PJ_Y = STREET_BOT - 50
SIG_Y = PJ_Y - 30 * SCALE  # = 30m de distance, mais clampé dans la rue
SIG_Y = max(SIG_Y, STREET_TOP + 60)  # garde-fou si rue trop courte


def marker(cx, cy, label, color, r=15, ring=False):
    if ring:
        d.ellipse([(cx - r - 4, cy - r - 4), (cx + r + 4, cy + r + 4)],
                  outline=color, width=2)
    d.ellipse([(cx - r, cy - r), (cx + r, cy + r)], fill=color, outline=WALL, width=2)
    text((cx, cy), label, font=F_TAG, fill=(255, 255, 255), anchor="mm")


# Leader au centre, 4 zélotes en V, 1 jeune en retrait
marker(CX, SIG_Y, "L", SIGMAR, r=17)
marker(CX - 45, SIG_Y + 40, "Z", SIGMAR, r=13)
marker(CX + 45, SIG_Y + 40, "Z", SIGMAR, r=13)
marker(CX - 30, SIG_Y - 30, "Z", SIGMAR, r=13)
marker(CX + 30, SIG_Y - 30, "Z", SIGMAR, r=13)
marker(CX, SIG_Y - 60, "j", SIGMAR, r=11)  # jeune en retrait nord

# Annotation des pistolets
text((CX - 45 - 25, SIG_Y + 40), "(P)", font=F_SMALL, fill=SIGMAR, anchor="rm")
text((CX + 45 + 25, SIG_Y + 40), "(P)", font=F_SMALL, fill=SIGMAR, anchor="lm")

# Label des Sigmarites
text((CX, SIG_Y - 95), "Sigmarites post-assaut Helstein", font=F_TAG, fill=SIGMAR, anchor="mm")
text((CX, SIG_Y - 75), "(5-6 hommes — civils + signes Sigmar)", font=F_SMALL, fill=SIGMAR, anchor="mm")


# --- PJ marker (point d'arrivée) ---
def pj_marker(cx, cy):
    d.polygon([(cx, cy - 14), (cx - 12, cy + 8), (cx + 12, cy + 8)],
              fill=PJ_COL, outline=WALL)
    text((cx, cy), "PJ", font=F_SMALL, fill=(255, 255, 255), anchor="mm")

pj_marker(CX - 25, PJ_Y)
pj_marker(CX, PJ_Y + 10)
pj_marker(CX + 25, PJ_Y)
# Annotation au-dessus des marqueurs pour ne pas chevaucher le pied de carte
text((CX, PJ_Y - 70), "Position d'arrivée PJ", font=F_TAG, fill=PJ_COL, anchor="mm")
text((CX, PJ_Y - 50), "(après la « rue qui n'aurait", font=F_SMALL, fill=PJ_COL, anchor="mm")
text((CX, PJ_Y - 34), "pas dû être là »)", font=F_SMALL, fill=PJ_COL, anchor="mm")


# --- Flèche provenance (sud) ---
def arrow(p1, p2, color, width=3):
    d.line([p1, p2], fill=color, width=width)
    dx, dy = p2[0] - p1[0], p2[1] - p1[1]
    a = math.atan2(dy, dx)
    a1 = (p2[0] - 18 * math.cos(a - 0.35), p2[1] - 18 * math.sin(a - 0.35))
    a2 = (p2[0] - 18 * math.cos(a + 0.35), p2[1] - 18 * math.sin(a + 0.35))
    d.polygon([p2, a1, a2], fill=color)

arrow((CX, my1 - 5), (CX, PJ_Y + 40), ARRIVAL, width=3)
text((CX, my1 + 18), "venant du sud — pharmacie verte, Stollenplatz",
     font=F_SMALL, fill=ARRIVAL, anchor="mm")


# --- Mesure 30 m PJ ↔ Sigmarites ---
def dashed_line(p1, p2, fill, width=2, dash=10, gap=6):
    x1d, y1d = p1
    x2d, y2d = p2
    dx, dy = x2d - x1d, y2d - y1d
    length = math.hypot(dx, dy)
    if length == 0:
        return
    ux, uy = dx / length, dy / length
    pos = 0
    while pos < length:
        a = (x1d + ux * pos, y1d + uy * pos)
        bp = min(pos + dash, length)
        b = (x1d + ux * bp, y1d + uy * bp)
        d.line([a, b], fill=fill, width=width)
        pos += dash + gap

# Ligne pointillée le long du bord ouest de la rue
mes_x = SL + 18
dashed_line((mes_x, PJ_Y - 5), (mes_x, SIG_Y + 60), ANNOT_GREY, width=2)
# Petits tirets transverses tous les 5 m
for m in range(5, 31, 5):
    yy = PJ_Y - m * SCALE
    if STREET_TOP < yy < STREET_BOT:
        d.line([(mes_x - 6, yy), (mes_x + 6, yy)], fill=ANNOT_GREY, width=2)
        text((mes_x - 12, yy), f"{m} m", font=F_SMALL, fill=ANNOT_GREY, anchor="rm")
text((mes_x + 18, (PJ_Y + SIG_Y) // 2), "30 m", font=F_TAG, fill=ANNOT_GREY, anchor="lm")


# --- Silhouette enfant (post-résolution) ---
# Au coin de la ruelle latérale est, côté rue
SIL_X = ALLEY_LEFT + 18
SIL_Y = ALLEY_TOP + 14
d.ellipse([(SIL_X - 10, SIL_Y - 14), (SIL_X + 10, SIL_Y + 14)], fill=SILHOUETTE, outline=WALL, width=1)
d.ellipse([(SIL_X - 8, SIL_Y - 22), (SIL_X + 8, SIL_Y - 8)], fill=SILHOUETTE, outline=WALL, width=1)
# Annotation (au-dessus de la ruelle pour ne pas chevaucher la silhouette)
text((ALLEY_LEFT + 50, ALLEY_TOP - 60), "Silhouette 1/3 — enfant coupe au bol",
     font=F_TAG, fill=SILHOUETTE, anchor="lm")
text((ALLEY_LEFT + 50, ALLEY_TOP - 40), "apparaît à 20-30 m APRÈS résolution",
     font=F_SMALL, fill=SILHOUETTE, anchor="lm")
text((ALLEY_LEFT + 50, ALLEY_TOP - 22), "Perception (-20) pour la remarquer",
     font=F_SMALL, fill=SILHOUETTE, anchor="lm")
# Flèche fine de l'annotation vers la silhouette
d.line([(ALLEY_LEFT + 60, ALLEY_TOP - 14), (SIL_X + 4, SIL_Y - 18)],
       fill=SILHOUETTE, width=1)


# --- Ruelle qui s'allonge ---
# Petits chevrons fade le long de la ruelle est
n_chev = 8
for i in range(n_chev):
    t = (i + 1) / (n_chev + 1)
    cx_c = ALLEY_LEFT + int(t * (ALLEY_RIGHT - ALLEY_LEFT))
    cy_c = ALLEY_Y
    alpha = 1 - t
    col = tuple(int(SILHOUETTE[k] * alpha + BG[k] * (1 - alpha)) for k in range(3))
    d.polygon([(cx_c, cy_c - 6), (cx_c + 10, cy_c), (cx_c, cy_c + 6)], fill=col)
text((ALLEY_RIGHT - 10, ALLEY_BOT + 28),
     "« la ruelle semble s'allonger »", font=F_FADE, fill=ANNOT_GREY, anchor="rm")


# (Ambiance volontairement non affichée sur la carte — couverte par titre et fiche scène)


# --- Légende latérale droite ---
LEG_X = mx1 + 60
LEG_Y = my0 + 80
d.rectangle([(LEG_X - 10, LEG_Y - 10), (LEG_X + 300, LEG_Y + 540)],
            outline=WALL, width=2, fill=(248, 242, 222))
text((LEG_X, LEG_Y), "Légende", font=F_H2, anchor="lt")
ly = LEG_Y + 38


def legend_row(y, fn, lbl, sub=None):
    fn(LEG_X + 14, y + 14)
    text((LEG_X + 40, y + 14), lbl, font=F_BODY, anchor="lm")
    if sub:
        text((LEG_X + 40, y + 34), sub, font=F_SMALL, anchor="lm", fill=(80, 60, 40))


def dot_sigmar_L(cx, cy):
    marker(cx, cy, "L", SIGMAR, r=12)
def dot_sigmar_Z(cx, cy):
    marker(cx, cy, "Z", SIGMAR, r=10)
def dot_sigmar_j(cx, cy):
    marker(cx, cy, "j", SIGMAR, r=8)
def dot_beggar(cx, cy):
    d.ellipse([(cx - 8, cy - 6), (cx + 8, cy + 8)], fill=BEGGAR, outline=WALL, width=1)
def dot_sil(cx, cy):
    d.ellipse([(cx - 8, cy - 12), (cx + 8, cy + 12)], fill=SILHOUETTE, outline=WALL, width=1)
def dot_pj(cx, cy):
    d.polygon([(cx, cy - 10), (cx - 8, cy + 6), (cx + 8, cy + 6)], fill=PJ_COL, outline=WALL)

legend_row(ly, dot_pj, "PJ (arrivée sud)"); ly += 36
legend_row(ly, dot_sigmar_L, "Leader laïc",
           "CC 45 · E 40 · PV 14 · épée+dague"); ly += 50
legend_row(ly, dot_sigmar_Z, "Zélote (×4)",
           "CC 35 · E 35 · PV 12 · arme courte"); ly += 50
text((LEG_X + 14, ly + 6), "(P) = pistolet (2 zélotes sur 4)", font=F_SMALL, fill=SIGMAR, anchor="lt"); ly += 28
legend_row(ly, dot_sigmar_j, "Jeune zélote",
           "CC 30 · PV 10 · fuit au 1er mort"); ly += 50
legend_row(ly, dot_beggar, "Mendiant accroupi",
           "immobile, regard vide"); ly += 38
legend_row(ly, dot_sil, "Silhouette (Gideon)",
           "post-résolution uniquement"); ly += 38

# Mini cartouche options
ly += 10
d.rectangle([(LEG_X - 4, ly), (LEG_X + 296, ly + 130)],
            outline=ANNOT_GREY, width=1, fill=(252, 248, 232))
text((LEG_X + 6, ly + 8), "Options PJ", font=F_TAG, anchor="lt")
text((LEG_X + 6, ly + 32), "A · Retrait discret (recommandé)", font=F_SMALL, anchor="lt", fill=ARRIVAL)
text((LEG_X + 6, ly + 52), "B · Confrontation verbale", font=F_SMALL, anchor="lt")
text((LEG_X + 16, ly + 70), "Charme / Intimidation -10", font=F_SMALL, anchor="lt", fill=ANNOT_GREY)
text((LEG_X + 6, ly + 90), "C · Combat (4 Z + L + j)", font=F_SMALL, anchor="lt", fill=SIGMAR)
text((LEG_X + 16, ly + 108), "si jeune fuit → PJ wanted (var. B)", font=F_SMALL, anchor="lt", fill=SIGMAR)


# --- Titre / cartouche en-tête ---
text((W // 2, 50), "Glissement Reikerbahn — rue déformée", font=F_TITLE, anchor="mm")
text((W // 2, 95), "Scène 51 · matinée · zone de bleed Altdorf (anomalie native)", font=F_BODY, anchor="mm")
text((W // 2, 125), "Distance d'engagement : 30 m  •  option A (retrait) recommandée",
     font=F_TAG, fill=(140, 30, 30), anchor="mm")

# Nord
text((mx1 + 30, my0 + 30), "N", font=F_TITLE, anchor="lm")
arrow((mx1 + 45, my0 + 75), (mx1 + 45, my0 + 25), WALL, width=3)

# Échelle
SC_X, SC_Y = mx0 + 40, my1 + 60
d.line([(SC_X, SC_Y), (SC_X + 10 * SCALE, SC_Y)], fill=WALL, width=3)
for i in range(11):
    d.line([(SC_X + SCALE * i, SC_Y - 6), (SC_X + SCALE * i, SC_Y + 6)], fill=WALL, width=2)
text((SC_X + 5 * SCALE, SC_Y + 22), "10 m  (1 case = 1 m)", font=F_SMALL, anchor="mm")

# Pied de page
text((W // 2, H - 30),
     "Topologie : voir 51 - Glissement Reikerbahn  •  schéma indicatif, l'orientation de la rue dans Altdorf est volontairement floue (bleed)",
     font=F_SMALL, anchor="mm", fill=(100, 80, 50))

out = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                   "51 - Battlemap Glissement Reikerbahn.png")
img.save(out, "PNG", optimize=True)
print(f"Wrote {out}")
