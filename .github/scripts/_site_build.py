#!/usr/bin/env python3
"""
_site_build.py — Generate a static HTML site from the blog content.

Outputs to "_site/" :
    _site/
        index.html              landing page (7 arcs + category shortcuts)
        style.css               theme
        arc-1.html ... arc-7.html  per-arc landing pages (sessions in that arc)
        resumes/01.html ... 62.html    one page per session
        resumes/index.html             full session list (no 10-post limit)
        pj/<slug>.html, pj/index.html
        pnj/<slug>.html, pnj/index.html
        lieux/<slug>.html, lieux/index.html
        documents/<slug>.html, documents/index.html
        annexes/<slug>.html, annexes/index.html

The site shows the original HTML of each blog post (preserved verbatim) with
in-content `/search/label/X` URLs rewritten to point at the local character or
place page when one exists.

Usage:
    python _site_build.py                # full rebuild
    python _site_build.py --clean        # delete _site/ first
    python _site_build.py --serve 8000   # rebuild then serve locally

Dependencies: requests, beautifulsoup4 (already required by _blog_sync.py)
"""

from __future__ import annotations

import argparse
import hashlib
import html
import json
import re
import shutil
import subprocess
import sys
import unicodedata
from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from urllib.parse import urlparse, unquote, quote

import requests
from bs4 import BeautifulSoup

# Reuse the Atom fetcher, classifier and shared helpers from the sync script.
from _blog_sync import (
    fetch_all_posts, read_existing_index, classify,
    _normalise_url, _SEARCH_LABEL, BLOG_HOST,
    ALL_FOLDERS, ROOT, Post,
    relative_url, setup_utf8_stdout,
)

OUT = Path(__file__).parent / "_site"

# --------------------------------------------------------------------------- #
# MJ overlay configuration (Phase 1)
# --------------------------------------------------------------------------- #
# MJ-private content lives at _site/mj-{MJ_TOKEN}/ and is gated client-side
# by a JS flag (cf. mj-mode.js). Public HTML contains nothing MJ-specific
# except the section markers ".mj-only" which are CSS-hidden by default.
#
# Token loaded from (in order):
#   1. Environment variable MJ_TOKEN
#   2. File ./.mj-token (project-local, should be gitignored)
#   3. File ~/.foundry-mj-token (user-global)
# No fallback: missing token → MJ overlay is disabled (public site builds normally).

NOTES_MJ_DIR = Path(__file__).parent / "Notes MJ"


def _load_mj_token() -> str | None:
    import os
    v = os.environ.get("MJ_TOKEN")
    if v and v.strip():
        return v.strip()
    for candidate in (
        Path(__file__).parent / ".mj-token",
        Path.home() / ".foundry-mj-token",
    ):
        if candidate.exists():
            t = candidate.read_text(encoding="utf-8").strip()
            if t:
                return t
    return None


MJ_TOKEN: str | None = _load_mj_token()
MJ_OUT_DIR: Path | None = (OUT / f"mj-{MJ_TOKEN}") if MJ_TOKEN else None

# Narrative arcs. The blog uses each post's <published> month to encode the
# arc: posts published in 2024-10 are arc 1, 2024-09 are arc 2, … Listing the
# months explicitly here is the source of truth — the session-number ranges
# (s_start / s_end) are computed from the data once it's loaded.
@dataclass
class Arc:
    num: int
    title: str
    intro_title: str             # post title of the arc's intro page
    pub_months: list[str]        # YYYY-MM strings; a post falls in this arc
                                 # if its published month matches one of these
    s_start: int = 0             # filled in by compute_arc_session_ranges()
    s_end:   int = 0

ARCS: list[Arc] = [
    Arc(1, "Prologue",                       "Prologue",                       ["2024-10"]),
    Arc(2, "L'Ennemi dans l'Ombre",          "L’ennemi dans l’Ombre",          ["2024-09"]),
    Arc(3, "Aventures à Ubersreik",          "Aventures à Ubersreik",          ["2024-08"]),
    Arc(4, "Mort sur le Reik",               "Mort sur le Reik",               ["2024-07"]),
    Arc(5, "Le Pouvoir Derrière le Trône",   "Le Pouvoir Derrière le Trône",   ["2024-06"]),
    Arc(6, "Le Rat Cornu",                   "Le Rat Cornu (partie 1)",        ["2024-05", "2024-04"]),
    Arc(7, "L'Empire en Ruine",              "L'Empire en ruine",              ["2024-03"]),
]

# (out-folder name, source folder name, human label, nav order)
CATEGORIES: list[tuple[str, str, str]] = [
    ("resumes",   "Résumés",   "Résumés"),
    ("pj",        "PJ",        "PJ"),
    ("pnj",       "PNJ",       "PNJ"),
    ("lieux",     "Lieux",     "Lieux & Organisations"),
    ("documents", "Documents", "Documents"),
    ("univers",   "Univers",   "Univers"),
]
# Folders whose pages are generated (so arc bodies / variant resolution work)
# but which are NOT in the top nav and have no index page.
HIDDEN_FOLDERS: list[tuple[str, str]] = [
    ("tomes",     "Tomes"),     # arc-intro pages — already shown as arc body
]
FOLDER_TO_OUT = {src: out for out, src, _ in CATEGORIES}
FOLDER_TO_OUT.update({src: out for out, src in HIDDEN_FOLDERS})
FOLDER_TO_LABEL = {src: lbl for _, src, lbl in CATEGORIES}

# Source folders whose pages cross-link with session résumés (i.e. the
# "compendium" entities that appear inside one or more sessions). Used to
# filter pages when building per-session apparition lists and arc buckets.
ENTITY_FOLDERS = {"PJ", "PNJ", "Lieux", "Documents", "Univers"}

# Public site root. Used to emit absolute URLs in Open Graph tags + sitemap.
SITE_URL = "https://cgauche.github.io/mon-ennemi-interieur"


@dataclass
class OgMeta:
    """Open Graph / social-preview metadata for one page."""
    description: str = ""
    image: str | None = None     # absolute URL (Blogger CDN URL or similar)
    og_type: str = "website"     # "website" for landings, "article" for posts
    url: str = ""                # canonical absolute URL of this page


def absolute_url(site_rel: Path | str) -> str:
    """Site-relative path → canonical absolute URL."""
    rel = site_rel.as_posix() if isinstance(site_rel, Path) else str(site_rel)
    if rel in ("", "index.html", "./"):
        return f"{SITE_URL}/"
    return f"{SITE_URL}/{rel}"


# --------------------------------------------------------------------------- #
# Slug + URL helpers
# --------------------------------------------------------------------------- #


_FIRST_IMG = re.compile(r'<img\b[^>]*\bsrc=["\']([^"\']+)["\']', re.IGNORECASE)
_SESSION_PREFIX = re.compile(r"^\s*\d{1,3}\s*[\)\.]\s*")


def session_short_title(title: str) -> str:
    """Strip the leading 'NN) ' from a session title for use in cards/lists
    where the session number is already shown separately."""
    return _SESSION_PREFIX.sub("", title or "")


def session_card_html(pg: 'Page', href: str) -> str:
    """Standard session card (thumbnail + 'Session NN' eyebrow + short title).
    A native `title` attribute fallback shows the full title on hover, since
    the visible label is line-clamped to 2 lines."""
    short = session_short_title(pg.post.title)
    return (
        f'<li><a class="thumb-card session-card" href="{html.escape(href)}" '
        f'title="Session {pg.session_num:02d} — {html.escape(short)}">'
        f'{_thumb_html(pg)}'
        f'<div class="thumb-card-body">'
        f'<span class="snum">Session {pg.session_num:02d}</span>'
        f'<span class="stitle">{html.escape(short)}</span>'
        f'</div></a></li>')


def session_link_html(pg: 'Page', href: str) -> str:
    """Compact session line (used in apparitions and similar lists)."""
    return (
        f'<li><a href="{html.escape(href)}">'
        f'<span class="snum">Session {pg.session_num:02d}</span>'
        f'<span class="stitle">{html.escape(session_short_title(pg.post.title))}</span>'
        f'</a></li>')


def entry_card_html(pg: 'Page', href: str) -> str:
    """Standard PJ/PNJ/Lieu/Doc/Univers card — thumbnail + name + optional
    subtitle. A native `title` attribute carries the full name (and subtitle,
    when present) so the browser shows it on hover when the visible label
    is line-clamped."""
    tooltip = pg.post.title + (f" — {pg.subtitle}" if pg.subtitle else "")
    sub = (f'<span class="entry-sub">{html.escape(pg.subtitle)}</span>'
           if pg.subtitle else '')
    return (
        f'<li><a class="thumb-card entry-card" href="{html.escape(href)}" '
        f'title="{html.escape(tooltip)}">'
        f'{_thumb_html(pg)}'
        f'<div class="thumb-card-body">'
        f'<span class="entry-name">{html.escape(pg.post.title)}</span>'
        f'{sub}'
        f'</div></a></li>')


_TAG_RE = re.compile(r'<[^>]+>')
_WS_RE = re.compile(r'\s+')


def strip_html(html_str: str) -> str:
    """Strip tags + collapse whitespace + decode the few entities we care about."""
    text = _TAG_RE.sub(' ', html_str)
    for src, dst in (("&amp;", "&"), ("&lt;", "<"), ("&gt;", ">"),
                     ("&nbsp;", " "), ("&#39;", "'"), ("&quot;", '"'),
                     ("&laquo;", "«"), ("&raquo;", "»"), ("&rsquo;", "’")):
        text = text.replace(src, dst)
    return _WS_RE.sub(' ', text).strip()


_LEADING_HEADING = re.compile(r'^\s*<h[1-3][^>]*>.*?</h[1-3]>', re.DOTALL | re.IGNORECASE)


def og_description(html_body: str, limit: int = 200) -> str:
    """First ~`limit` chars of `html_body`, plain-text, cut on a word boundary.
    A leading <h1>/<h2>/<h3> is stripped since it would duplicate og:title."""
    body = _LEADING_HEADING.sub('', html_body, count=1)
    text = strip_html(body)
    if len(text) <= limit:
        return text
    cut = text[:limit]
    last_space = cut.rfind(" ")
    if last_space > limit * 0.7:
        cut = cut[:last_space]
    return cut.rstrip(",;:-–— ") + "…"


def normalise_for_search(s: str) -> str:
    """Accent-strip + lowercase. Same algorithm runs on the JS side."""
    s = unicodedata.normalize('NFD', s)
    s = ''.join(c for c in s if unicodedata.category(c) != 'Mn')
    return s.lower()


def extract_first_image(html: str) -> str | None:
    """Return the URL of the first <img> in a post body, or None."""
    m = _FIRST_IMG.search(html)
    return m.group(1) if m else None


# Blog convention for PJ/PNJ/Lieu pages: the portrait is followed by a
# centered <b>…</b> line giving the entity's role / title (e.g.
# "Responsable de la kommission du commerce" under Gotthard's portrait).
_SUBTITLE_RE = re.compile(
    r'<(?:div|p|center)[^>]*?text-align[^>]*?>\s*'
    r'<(?:b|strong)>([^<]{3,200})</(?:b|strong)>\s*'
    r'</(?:div|p|center)>',
    re.IGNORECASE | re.DOTALL,
)


def extract_subtitle(html_body: str) -> str | None:
    """Pull the centered-bold subtitle line that follows the portrait on
    PJ/PNJ/Lieu pages. Returns None if the post doesn't follow this layout.
    Only scans the head of the body — late centered-bold accents in the
    narrative shouldn't be picked up."""
    m = _SUBTITLE_RE.search(html_body[:3000])
    if not m:
        return None
    text = strip_html(m.group(1)).strip()
    return text or None


def strip_subtitle_paragraph(html_body: str) -> str:
    """Remove the first centered-bold subtitle paragraph from a body. Used
    when the subtitle is promoted to the variant-bio heading, so it doesn't
    appear twice on the page."""
    return _SUBTITLE_RE.sub('', html_body, count=1)


def slugify(stem: str) -> str:
    """'Boris Todbringer (2)' → 'boris-todbringer-2'."""
    s = unicodedata.normalize("NFKD", stem)
    s = "".join(c for c in s if not unicodedata.combining(c))
    s = s.lower()
    s = re.sub(r"[^a-z0-9]+", "-", s)
    return s.strip("-") or "untitled"


@dataclass
class Page:
    post: Post
    out_path: Path           # absolute path under _site/
    site_rel: Path           # relative path from _site/ root (used for links)
    slug: str
    session_num: int | None  # only set for Résumés
    thumbnail: str | None = None   # first image found in the post body
    subtitle: str | None = None    # centered-bold line under the portrait
    variant_group: str | None = None  # character label shared with variants
    is_main: bool = False    # True for the canonical page of a character group


def build_pages(posts: list[Post]) -> list[Page]:
    pages: list[Page] = []
    used_slugs: dict[str, int] = {}  # (out-folder, slug) → counter

    for p in posts:
        if p.folder is None or p.folder not in FOLDER_TO_OUT:
            continue
        out_folder = FOLDER_TO_OUT[p.folder]
        stem = p.filename[:-3] if p.filename and p.filename.endswith(".md") else (p.title or "untitled")

        # Résumés: name pages by session number ("01", ..., "62") instead of slug.
        if out_folder == "resumes":
            m = re.match(r"^\s*(\d{1,3})", stem)
            if m:
                num = int(m.group(1))
                slug = f"{num:02d}"
            else:
                slug = slugify(stem)
                num = None
        else:
            slug = slugify(stem)
            num = None

        # Ensure unique slug per output folder
        key = f"{out_folder}/{slug}"
        if key in used_slugs:
            used_slugs[key] += 1
            slug = f"{slug}-{used_slugs[key]}"
        else:
            used_slugs[key] = 1

        site_rel = Path(out_folder) / f"{slug}.html"
        out_path = OUT / site_rel
        # Subtitle only matters for entity pages (PJ/PNJ/Lieu) — sessions
        # and arc intros don't follow the centered-bold convention.
        sub = extract_subtitle(p.html) if p.folder in ENTITY_FOLDERS else None
        pages.append(Page(post=p, out_path=out_path, site_rel=site_rel,
                          slug=slug, session_num=num,
                          thumbnail=extract_first_image(p.html),
                          subtitle=sub))
    return pages


def build_search_index(
        pages: list[Page],
        siblings_idx: dict[tuple[str, str], list[Page]]) -> list[dict]:
    """Build a compact list of {title, cat, url, search, variant_titles?} entries.

    Variants are folded into their main's entry. The main carries its own
    title plus the normalised titles of all its variants, so a search for
    'batelière' still surfaces Elvira (her 'Elvira, batelière' variant
    title matches).
    """
    out: list[dict] = []
    for pg in pages:
        if pg.variant_group and not pg.is_main:
            continue

        excerpt_parts = [pg.post.title or "", strip_html(pg.post.html)[:500]]
        variant_titles_norm: list[str] = []
        # Display title for a variant group is the unifying label
        # (e.g. "Elvira" rather than "Elvira, grande prêtresse de Rhya"),
        # so the search-result chip shows the character's name, not their
        # most-recent role. The original main title is kept as a variant
        # title so direct queries for the role still match.
        display_title = pg.post.title or ""
        if pg.variant_group and pg.is_main:
            siblings = siblings_idx.get((pg.post.folder, pg.variant_group), [])
            if pg.variant_group and pg.variant_group != display_title:
                variant_titles_norm.append(normalise_for_search(display_title))
                display_title = pg.variant_group
            for sib in siblings:
                if sib.site_rel == pg.site_rel:
                    continue
                variant_titles_norm.append(normalise_for_search(sib.post.title or ""))
                excerpt_parts.append(sib.post.title or "")
                excerpt_parts.append(strip_html(sib.post.html)[:500])

        haystack = " ".join(excerpt_parts + [
            FOLDER_TO_LABEL.get(pg.post.folder, "") if pg.post.folder else "",
        ])
        entry: dict = {
            "t": display_title,
            "c": FOLDER_TO_LABEL.get(pg.post.folder, "") if pg.post.folder else "",
            "u": pg.site_rel.as_posix(),
            "s": normalise_for_search(haystack),
        }
        if variant_titles_norm:
            entry["vt"] = variant_titles_norm
        if pg.session_num is not None:
            entry["n"] = pg.session_num
        if pg.thumbnail:
            entry["i"] = pg.thumbnail
        out.append(entry)
    return out


_GROUPABLE_FOLDERS = {"PJ", "PNJ", "Lieux"}
_GENERIC_LABELS = {'*', '$', '^', ''}


def _candidate_labels(pg: 'Page') -> list[str]:
    """All labels that could plausibly be this page's *identity* —
    i.e. labels whose text matches the title in some way (exact, comma-prefix,
    first-word, or case-insensitive title-prefix). Returned in priority order.
    """
    title = pg.post.title.strip()
    labels = [s for s in (l.strip() for l in pg.post.labels)
              if s and not s.isdigit() and s not in _GENERIC_LABELS]
    if not labels or not title:
        return []
    cands: list[str] = []
    def add(x: str) -> None:
        if x and x not in cands:
            cands.append(x)
    if title in labels:
        add(title)
    prefix = title.split(',')[0].strip()
    if prefix in labels:
        add(prefix)
    first_word = title.split(maxsplit=1)[0].strip(" ,.()")
    if first_word in labels:
        add(first_word)
    title_lower = title.lower()
    for l in labels:
        if len(l) >= 3 and title_lower.startswith(l.lower()):
            add(l)
    return cands


def compute_variant_groups(pages: list[Page]) -> None:
    """Annotate each PJ/PNJ/Lieu page with its variant group + main flag.

    A "group" is a set of pages sharing the same character / place label
    (e.g. all posts tagged 'Elvira': her main bio + every version like
    'Elvira, batelière', 'Elvira, grande prêtresse de Rhya', etc.).
    Same-title posts that the GM republished over time (e.g. 5 separate
    'Middenheim' posts) form a group via the shared 'Middenheim' label.

    Rules:
      - The "main" is the *most recently published* page in the group — the
        date is the closest signal to the article's canonical state.
      - Each page belongs to at most one group; ties favour larger groups.
      - Pages outside `_GROUPABLE_FOLDERS` are ignored.
    """
    # Step 1: per page, list all candidate identity labels.
    page_cands: list[tuple[Page, list[str]]] = []
    for pg in pages:
        if pg.session_num is not None or pg.post.folder not in _GROUPABLE_FOLDERS:
            continue
        cands = _candidate_labels(pg)
        if cands:
            page_cands.append((pg, cands))

    # Step 2: count how many pages claim each (folder, label) pair as a
    # candidate — popular candidates indicate a real shared identity.
    freq: Counter[tuple[str, str]] = Counter()
    for pg, cands in page_cands:
        for c in cands:
            freq[(pg.post.folder, c)] += 1

    # Step 3: assign each page to the most-shared candidate within its folder.
    # Ties broken by shorter label (the unifying first-name beats the full name).
    by_label: dict[tuple[str, str], list[Page]] = {}
    for pg, cands in page_cands:
        scored = [(freq[(pg.post.folder, c)], -len(c), c) for c in cands]
        scored.sort(reverse=True)
        best = scored[0][2]
        by_label.setdefault((pg.post.folder, best), []).append(pg)

    # Step 4: real groups have 2+ members. The "main" is the canonical face of
    # the group, picked by:
    #   1. Character-sheet signal — a page with NO session-number labels is the
    #      GM's standalone character sheet, written once and not tied to any
    #      specific session (the convention for PJs: each player has one
    #      sheet, e.g. "Phineas" / "Elvira" / "Markward Skippy Jeronymus",
    #      published well after the role-variant snapshots). Wins outright.
    #   2. Bare-name match — title equals the group label exactly (e.g.
    #      "Boris Todbringer", "Karl-Heinz Wasmeier"). Useful for PNJs where
    #      every variant is a state snapshot with the same character name.
    #   3. Latest session-number label — among remaining ties, the variant
    #      tagged with the most recent session is the current narrative state.
    #   4. Publication date — final tie-breaker.
    for (folder, label), group_pages in by_label.items():
        if len(group_pages) < 2:
            continue

        def _main_rank(p: Page) -> tuple[int, int, int, str]:
            title = (p.post.title or "").strip()
            session_nums = [int(s.strip()) for s in p.post.labels
                            if s.strip().isdigit()]
            no_session_labels = 1 if not session_nums else 0
            is_canonical_title = 1 if title == label else 0
            latest_session = max(session_nums) if session_nums else 0
            return (no_session_labels, is_canonical_title,
                    latest_session, p.post.published or '')

        main = max(group_pages, key=_main_rank)
        main.variant_group = label
        main.is_main = True
        for p in group_pages:
            if p is not main:
                p.variant_group = label
                p.is_main = False


def variant_siblings_index(pages: list[Page]) -> dict[tuple[str, str], list[Page]]:
    """(folder, label) → all pages in that group (main + variants).

    Scoping by folder matters: the GM sometimes tags the same character in
    both PJ and PNJ, but each folder keeps its own group/main so siblings
    don't bleed across categories.
    """
    out: dict[tuple[str, str], list[Page]] = {}
    for pg in pages:
        if pg.variant_group:
            key = (pg.post.folder, pg.variant_group)
            out.setdefault(key, []).append(pg)
    for key, members in out.items():
        members.sort(key=lambda p: (0 if p.is_main else 1,
                                    p.post.published or ''))
    return out


def siblings_for(pg: Page,
                 siblings_idx: dict[tuple[str, str], list[Page]]) -> list[Page]:
    if not pg.variant_group:
        return []
    return siblings_idx.get((pg.post.folder, pg.variant_group), [])


def build_session_pages_by_num(pages: list[Page]) -> dict[int, Page]:
    """session_num → the résumé page for that session."""
    return {pg.session_num: pg for pg in pages if pg.session_num is not None}


def build_pages_by_session(pages: list[Page]) -> dict[int, dict[str, list[Page]]]:
    """Reverse index: for each session number, the PJ/PNJ/Lieu/Doc/Annexe
    pages tagged with that session (i.e. that appear in or are linked from it)."""
    out: dict[int, dict[str, list[Page]]] = {}
    for pg in pages:
        if pg.session_num is not None:
            continue
        if pg.post.folder not in ENTITY_FOLDERS:
            continue
        for lbl in pg.post.labels:
            s = lbl.strip()
            if not s.isdigit():
                continue
            snum = int(s)
            out.setdefault(snum, {}).setdefault(pg.post.folder, []).append(pg)
    return out


def build_site_url_map(pages: list[Page]) -> tuple[dict[str, Path], dict[str, Path]]:
    """
    Returns (url_to_sitepath, label_to_sitepath):
      - url_to_sitepath maps a blog post URL → its `_site/...` path
      - label_to_sitepath maps a blog tag name → the best-matching page path
        (bare-name pages win over (2)/(3) variants)
    """
    url_map: dict[str, Path] = {}
    for pg in pages:
        url_map[_normalise_url(pg.post.blog_url)] = pg.site_rel

    label_map: dict[str, Path] = {}
    def bareness(pg: Page) -> int:
        return 0 if "(" not in (pg.post.filename or "") else 1
    for pg in sorted(pages, key=bareness):
        stem = pg.post.filename[:-3] if pg.post.filename else None
        for key in filter(None, [pg.post.title.strip(), stem]):
            label_map.setdefault(key, pg.site_rel)
        # Résumés are also reachable by their session number — handles
        # Blogger tag-links like /search/label/62 (used e.g. in the préface
        # to point at "l'épisode 62"). Map both padded and unpadded forms.
        if pg.session_num is not None:
            label_map.setdefault(str(pg.session_num), pg.site_rel)
            label_map.setdefault(f"{pg.session_num:02d}", pg.site_rel)
    return url_map, label_map


# --------------------------------------------------------------------------- #
# Entity popovers — wrap PJ/PNJ/Lieu mentions in session bodies
# --------------------------------------------------------------------------- #


# Tags whose text contents we leave alone — already a link, a heading that
# already shows the title, or code/script blocks where wrapping would break
# syntax.
_POPOVER_SKIP_TAGS = {
    'a', 'h1', 'h2', 'h3', 'h4', 'h5', 'h6',
    'code', 'pre', 'script', 'style',
}


# Short, human-readable category label shown as the popover eyebrow —
# mirrors the "Session NN" snum pattern of session_card_html.
_ENTITY_POPOVER_CAT_LABEL = {"PJ": "PJ", "PNJ": "PNJ", "Lieux": "Lieu", "Documents": "Document"}


@dataclass
class EntityPopover:
    site_rel: Path         # navigation target — always the canonical main page
    anchor: str | None     # '#variant-…' fragment when the matched form is a variant
    title: str             # display headline (the variant's title when from a variant)
    cat: str               # short eyebrow label ("PJ" / "PNJ" / "Lieu")
    portrait: str | None   # absolute image URL (Blogger CDN) or None
    subtitle: str | None   # role / function line shown under the name


# Manual whitelist of entity aliases that resolve to a canonical title in
# the GLOBAL popover map (i.e. fire everywhere, not just inside their
# session-tagged résumé). Each entry is `alias_form → canonical_title`.
# Two flavours, same mechanism:
#   (a) short forms — first-name / surname-only mentions for entities cited
#       often enough that writing the full name every time would be tedious
#       ('Karl-Franz' → 'Karl-Franz Holswig-Schliestein').
#   (b) alternate full forms — Notes MJ uses one spelling, blog file is
#       titled differently ('Emmanuelle von Liebwitz' → 'Comtesse
#       Emmanuelle Von Liebwitz'). Maps Notes MJ canon to blog canon so
#       prose using either form pops the correct entity.
# Keep this list tight: every entry creates a popover trigger that fires
# on EVERY occurrence of the alias in any rendered page, including MJ
# overlay and source/Notes MJ extracts where ambiguity is more likely.
# For one-off disambiguations, prefer `[[Canonical Title|alias]]` wikilinks
# in the source markdown, or rely on `session_alias_popovers` for
# session-tagged résumé bodies.
_GLOBAL_ENTITY_ALIASES: dict[str, str] = {
    # Short forms — globally unambiguous first-name / surname.
    "Karl-Franz": "Karl-Franz Holswig-Schliestein",
    "Boris": "Boris Todbringer",
    "Helborg": "Kurt Helborg",
    "Yorri": "Yorri XV",
    "Volkmar": "Volkmar von Hindenstern",
    "Karl-Heinz": "Karl-Heinz Wasmeier",
    "Wasmeier": "Karl-Heinz Wasmeier",
    # Alternate full forms — Notes MJ canon ↔ blog title with title prefix,
    # translation, casing, or naming variation. Previously lived in
    # `_MJ_MANUAL_ALIASES` (alongside genuine OCR typos), moved here to
    # separate semantics: typos are blog-side errors to be corrected,
    # these are equally valid naming conventions to be reconciled.
    "Emmanuelle von Liebwitz":  "Comtesse Emmanuelle Von Liebwitz",
    "Etelka Toppenheimer":      "Comtesse Etelka Toppenheimer",
    "Bettie Greenhill":         "Bettie Vertebutte",
    "Schloss Grauenberg":       "Château Graunenberg",
    "Ewald von Laue":           "Ewald Von Laue",
    "Jendrick von Dabernick":   "Jendrick Dabernick",
}


def build_entity_popover_map(pages: list[Page]) -> dict[str, EntityPopover]:
    """Global map of canonical-name → popover payload for PJ/PNJ/Lieu pages.
    Used to wrap any occurrence of the name in résumé bodies and in MJ
    enrichment sections. Variants are collapsed onto their main.

    Two layers:
      1. Full titles ("Karl-Franz Holswig-Schliestein", "Emmanuelle von
         Liebwitz") — always indexed.
      2. Manually whitelisted aliases (`_GLOBAL_ENTITY_ALIASES`): short
         forms ('Karl-Franz') and alternate full forms ('Emmanuelle von
         Liebwitz' → 'Comtesse Emmanuelle Von Liebwitz'). Every other
         ambiguous alias must be either (a) written out as the full title
         in the source markdown, (b) handled per-session via
         `session_alias_popovers` for résumé bodies, or (c) explicitly
         linked with `[[Canonical Title|alias]]` wikilinks."""
    out: dict[str, EntityPopover] = {}
    by_title: dict[str, Page] = {}
    for pg in pages:
        if pg.variant_group and not pg.is_main:
            continue
        # Short names (1-3 chars) are usually too generic to match safely.
        if len(pg.post.title.strip()) < 4:
            continue
        payload = _entity_to_popover(pg)
        if payload is not None:
            out[payload.title] = payload
            by_title[payload.title] = pg

    # Whitelisted aliases — resolve each to its canonical entity.
    for alias, canonical in _GLOBAL_ENTITY_ALIASES.items():
        if alias in out:
            continue  # full title indexing already covers it
        pg = by_title.get(canonical)
        if pg is None:
            continue  # canonical entity not in the corpus (yet)
        payload = _entity_to_popover(pg)
        if payload is not None:
            out[alias] = payload
    return out


def _entity_to_popover(pg: Page, main: Page | None = None) -> EntityPopover | None:
    """Construct the popover payload for an entity. If `main` is provided
    and differs from `pg`, the popover is treated as a variant view —
    navigation goes to the main page with a `#variant-<slug>` anchor while
    the display info (title, subtitle, portrait) comes from `pg`."""
    cat = _ENTITY_POPOVER_CAT_LABEL.get(pg.post.folder)
    if cat is None:
        return None
    target = main if main is not None else pg
    is_variant = main is not None and main.site_rel != pg.site_rel
    return EntityPopover(
        site_rel=target.site_rel,
        anchor=(f"variant-{pg.slug}" if is_variant else None),
        title=pg.post.title.strip(),
        cat=cat,
        portrait=pg.thumbnail,
        subtitle=pg.subtitle,
    )


def _mirror_dash_space(aliases: set[str]) -> None:
    """In-place: for every multi-word alias, also add its dash↔space twin
    so 'Ar Ulric' matches even when the title uses 'Ar-Ulric'."""
    for a in list(aliases):
        if '-' in a:
            aliases.add(a.replace('-', ' '))
        if ' ' in a:
            aliases.add(a.replace(' ', '-'))


# Single-token aliases that would collide with common French/WHFB nouns,
# titles, ranks or creature types. Even when a single entity globally claims
# one of these tokens, the popover would fire on unrelated occurrences in
# prose (e.g. 'Grand-Duc Léopold' → 'Grand' would popover any 'Grand Maître',
# 'Grand Théogoniste', 'Grand Cathédrale'). Compared case-insensitively.
_ALIAS_BLOCKLIST: frozenset[str] = frozenset({
    # Common titles + ranks (FR + EN canon)
    'grand', 'duc', 'duke', 'duchesse', 'duchess', 'comte', 'count',
    'comtesse', 'countess', 'baron', 'baronne', 'baroness',
    'seigneur', 'lord', 'dame', 'lady', 'sir',
    'roi', 'king', 'reine', 'queen',
    'empereur', 'emperor', 'impératrice', 'empress',
    'prince', 'princesse', 'princess',
    'saint', 'sainte',
    'frère', 'soeur', 'sœur', 'père', 'mère',
    'brother', 'sister', 'father', 'mother',
    'capitaine', 'captain', 'sergent', 'sergeant',
    'colonel', 'major', 'général', 'general',
    'maître', 'master', 'chevalier', 'knight',
    'prêtre', 'priest', 'prêtresse', 'priestess',
    'mage', 'wizard', 'sorcier', 'sorcière',
    'reiksmarshall', 'reiksmarshal', 'reiksguard',
    'graf', 'gravin', 'gravinne', 'kurfürst', 'elector',
    'archilecteur', 'lecteur', 'capitulaire',
    # Creature types (WHFB)
    'griffon', 'gryphon', 'cheval', 'horse', 'loup', 'wolf',
    'aigle', 'eagle', 'corbeau', 'raven', 'dragon',
    'démon', 'demon', 'daemon', 'troll', 'orc', 'gobelin',
    'goblin', 'skaven', 'mutant', 'mutante',
    # Generic narrative nouns that occur as entity titles too
    'homme', 'femme', 'enfant', 'garçon', 'fille',
    'guerrier', 'voleur', 'marchand', 'paysan',
})


def _entity_aliases(title: str) -> list[str]:
    """Short forms of `title` that might appear in narrative.

    Yields:
      - the full title verbatim;
      - left-trimmed suffixes of the pre-comma portion, so 'Etelka
        Toppenheimer' surfaces from 'Comtesse Etelka Toppenheimer' as a
        single multi-word alias (longest-first wins in the regex, so 'Etelka
        Toppenheimer' in text becomes one popover instead of two);
      - each capitalised token of 4+ characters, for cases where the
        narrative drops to a single name ('Emmanuelle').

    Only the pre-comma portion is mined for aliases — a role line like
    'émissaire de Dietrich' often mentions OTHER characters and would
    produce false-positive aliases.

    Single-token candidates listed in `_ALIAS_BLOCKLIST` (common titles,
    ranks, creature types) are dropped even when globally unambiguous —
    they would fire on unrelated prose ('Grand Maître', 'Loup Blanc').

    Dash↔space mirroring is applied at the end so 'Ar Ulric' matches
    'Ar-Ulric' and vice versa."""
    aliases: set[str] = set()
    title = title.strip()
    if len(title) >= 4:
        aliases.add(title)
    head = title.split(',', 1)[0].strip()
    parts = head.split()
    # Multi-word suffixes — 'Etelka Toppenheimer' from 'Comtesse Etelka Toppenheimer'.
    for i in range(1, len(parts)):
        sub = ' '.join(parts[i:])
        if len(sub) >= 4 and sub[:1].isupper() and sub != title:
            aliases.add(sub)
    # Individual capitalised tokens (4+ chars). Also split each token on
    # internal dashes so 'Immanuel-Fernand' yields 'Immanuel' and 'Fernand'
    # as candidates — the per-session ambiguity check then drops anything
    # shared across multiple entities (e.g. 'Holswig' surfaces in both
    # 'Immanuel-Fernand Holswig-Schliestein' and 'Karl-Franz
    # Holswig-Schliestein' → ambiguous → not added).
    for token in parts:
        for piece in [token, *token.split('-')]:
            clean = piece.strip(',;.:()"\'')
            if len(clean) >= 4 and clean[:1].isupper() and clean != title:
                if clean.lower() in _ALIAS_BLOCKLIST:
                    continue
                aliases.add(clean)
    _mirror_dash_space(aliases)
    return [a for a in aliases if a]


def _aliases_from_subtitle(subtitle: str) -> list[str]:
    """Treat a short, mostly-title-cased subtitle as an alias candidate
    ('Ar Ulric' for Jarrick Valgeir's role line). Skips long descriptive
    sentences ('Responsable de la kommission du commerce' has only the
    first word capitalised so it's filtered out)."""
    s = subtitle.strip()
    if not (4 <= len(s) <= 25):
        return []
    words = [w for w in re.split(r'[\s\-]+', s) if w]
    if not words:
        return []
    cap = sum(1 for w in words if w[:1].isupper())
    # Tolerate one preposition / connector among proper-noun words.
    if cap < len(words) - 1:
        return []
    out = {s}
    _mirror_dash_space(out)
    return list(out)


def session_alias_popovers(session_num: int,
                           global_map: dict[str, EntityPopover],
                           pages_by_session: dict[int, dict[str, list[Page]]],
                           ) -> dict[str, EntityPopover]:
    """Per-session alias map. Returns alias → EntityPopover for name forms
    that identify an entity tagged with this session.

    Layered on top of the global popover map (via dict merge): both short
    aliases AND the canonical title may resolve to the session-specific
    variant, so a résumé tagged with S31 surfaces the 'Seigneur de loi'
    Wasmeier rather than the globally-canonical 'Cultiste' state."""
    if session_num not in pages_by_session:
        return {}

    # Per alias, track:
    #  - the FIRST source entity to produce it (its title/subtitle/portrait
    #    seed the popover display — covers variant-specific roles like
    #    "Mark, prêtre d'Ulric" / "Ar Ulric")
    #  - the set of mains that claim it (>1 main → ambiguous, drop alias)
    alias_source: dict[str, tuple[Page, Page]] = {}  # alias → (source, main)
    alias_mains: dict[str, set[Path]] = {}

    for folder, entities in pages_by_session[session_num].items():
        if folder not in ENTITY_FOLDERS:
            continue
        for ent in entities:
            if ent.variant_group and not ent.is_main:
                main = _MAIN_FOR_GROUP.get((ent.post.folder, ent.variant_group))
                if main is None:
                    continue
            else:
                main = ent
            forms = set(_entity_aliases(ent.post.title))
            if ent.subtitle:
                forms.update(_aliases_from_subtitle(ent.subtitle))
            for alias in forms:
                alias_mains.setdefault(alias, set()).add(main.site_rel)
                alias_source.setdefault(alias, (ent, main))

    out: dict[str, EntityPopover] = {}
    for alias, (source, main) in alias_source.items():
        if len(alias_mains[alias]) != 1:
            continue   # ambiguous across different mains
        payload = _entity_to_popover(source, main=main)
        if payload is not None:
            out[alias] = payload
    return out


def _render_entity_pop_anchor(matched_text: str, rel_url: str,
                              ent: EntityPopover) -> str:
    """Render a single <a class="entity-pop"> trigger. Attributes carry the
    data the popover JS uses to fill its hover card. Variant matches append
    a #variant-<slug> fragment so navigation lands on the canonical page
    scrolled to the variant block, exactly like vignette links do."""
    href = rel_url + (f"#{ent.anchor}" if ent.anchor else "")
    attrs: list[tuple[str, str]] = [
        ('class', 'entity-pop'),
        ('href', href),
        ('data-title', ent.title),
        ('data-cat', ent.cat),
    ]
    if ent.subtitle:
        attrs.append(('data-subtitle', ent.subtitle))
    if ent.portrait:
        attrs.append(('data-portrait', ent.portrait))
    attr_str = ' '.join(f'{k}="{html.escape(v)}"' for k, v in attrs)
    return f'<a {attr_str}>{html.escape(matched_text)}</a>'


def inject_entity_popovers(html_body: str,
                           entity_map: dict[str, EntityPopover],
                           current_dir: Path) -> str:
    """Wrap text-node occurrences of known entity names in
    `<a class="entity-pop">…</a>` triggers carrying data-* attributes for
    the client-side popover.

    Skips text already inside links, headings, code blocks etc. Matching is
    case-sensitive and uses word boundaries — narrative writing capitalises
    proper nouns, so this drops most false positives (e.g. PNJ "Mort"
    won't match the lowercase noun "mort")."""
    if not entity_map:
        return html_body

    # Longest names first so "Boris Todbringer" beats "Boris" on overlapping
    # matches.
    names = sorted(entity_map.keys(), key=len, reverse=True)
    name_re = re.compile(r'\b(?:' + '|'.join(re.escape(n) for n in names) + r')\b')

    soup = BeautifulSoup(html_body, 'html.parser')

    candidates = [
        t for t in soup.find_all(string=True)
        if not any(p.name in _POPOVER_SKIP_TAGS for p in t.parents)
        and name_re.search(str(t))
    ]

    for txt in candidates:
        original = str(txt)
        parts: list[str] = []
        idx = 0
        for m in name_re.finditer(original):
            parts.append(html.escape(original[idx:m.start()]))
            ent = entity_map[m.group()]
            rel = relative_url(current_dir, ent.site_rel)
            parts.append(_render_entity_pop_anchor(m.group(), rel, ent))
            idx = m.end()
        parts.append(html.escape(original[idx:]))

        # Replace the text node with the parsed fragment via the
        # wrap+unwrap idiom.
        wrapper = BeautifulSoup(f'<x>{"".join(parts)}</x>', 'html.parser').x
        txt.replace_with(wrapper)
        wrapper.unwrap()

    return str(soup)


# --------------------------------------------------------------------------- #
# Canon refs — MJ-only popovers on `<code>EiR Intro l.205-218</code>`         #
# --------------------------------------------------------------------------- #
# Notes MJ heavily reference canonical Cubicle 7 books with backtick refs
# such as `EiR Intro l.205-218` or `HR l.699`. In MJ mode we transform these
# into hover triggers that show the cited markdown lines from the converted
# Source/ tree. Public mode never sees these — refs only live inside
# `.mj-only` blocks (enrichment sections + autonomous overlay pages).

SOURCE_DIR = Path(__file__).parent / "Source"

# Mapping abbreviation → Source/ subfolder. The chapter file inside the folder
# is resolved at index-build time via the conventions of _convert_pdfs.py
# ("NN - Chapter K - Title.md", "NN - CHAPTER K Title.md", "01 - <monolith>.md").
_CANON_BOOK_DIRS: dict[str, str] = {
    "EiS":           "Enemy Within Campaign Volume 1 Enemy in Shadows",
    "DoR":           "Enemy Within Campaign Volume 2 Death on the Reik",
    "PBT":           "Enemy Within Campaign Volume 3 Power Behind the Throne",
    "HR":            "Enemy Within Campaign Volume 4 The Horned Rat",
    "EiR":           "Enemy Within Campaign Volume 5 Empire in Ruins",
    "EiS Companion": "Enemy in Shadows Companion",
    "DoR Companion": "Death on the Reik Companion",
    "PBT Companion": "Power Behind the Throne Companion",
    "HR Companion":  "The Horned Rat Companion",
    "EiR Companion": "Empire In Ruins Companion",
    "Altdorf":       "Altdorf - Crown of the Empire",
    "Altdorf-CotE-VF": "Warhammer v4 - Aldorf la Couronne de l'Empire",
    "Middenheim":    "Middenheim - City of the White Wolf",
    "Salzenmund":    "Salzenmund - City Of Salt & Silver",
    "Up in Arms":    "Up in Arms",
    "RN&HD":         "Rough Nights & Hard Days",
    "Archives Vol I":   "Archives of the Empire - Vol I",
    "Archives Vol II":  "Archives of the Empire - Vol II",
    "Archives Vol III": "Archives of the Empire - Volume III",
    "Winds of Magic":   "Winds of Magic",
    "Sea Wardens":      "Sea Wardens of Cothique",
    # — Éditions françaises (VF). Convention : abréviation VO + suffixe -VF —
    "EiS-VF":             "Warhammer v4 - 1.0 L'ennemi dans l'Ombre",
    "EiS Companion-VF":   "Warhammer v4 - 1.0 L'ennemi dans l'Ombre Compagnon",
    "DoR-VF":             "Warhammer v4 - 2.0 Mort sur le Reik",
    "DoR Companion-VF":   "Warhammer v4 - 2.0 Mort sur le Reik Compagnon",
    "PBT-VF":             "Warhammer v4 - 3.0 Le Pouvoir Derriere le Trone",
    "Middenheim-VF":      "Warhammer v4 - Middenheim la cité du Loup Blanc",
    "UA-VF":              "Warhammer v4 - Aventures a Ubersreik",
    "Archives Vol I-VF":  "Warhammer v4 - Les archives de l'Empire volume 1",
    "Archives Vol II-VF": "Warhammer v4 - Les archives de l'Empire volume 2",
    "LdB-VF":             "Warhammer v4 - Livre de base version corrigée",
    "RN&HD-VF":           "Warhammer v4 - Nuits agitees & dures journées",
    "BI Aventure":        "WH4_FR_BI_Livre_Aventure",
    "BI Ubersreik":       "WH4_FR_BI_Livre_Ubersreik",
}

# Canon ref pattern. Tolerates a single trailing whitespace before the line spec.
# Captures: 1=book abbrev (may contain a space "EiS Companion"), 2=chapter
# designator ("Intro" / "ch.N" / nothing if just book + line), 3=loc kind ("l"/"p"),
# 4=loc spec (digits, optional range with "-" or "+").
_CANON_BOOK_ALTS = sorted(_CANON_BOOK_DIRS.keys(), key=len, reverse=True)
_CANON_REF_RE = re.compile(
    r'^(?P<book>(?:' + '|'.join(re.escape(b) for b in _CANON_BOOK_ALTS) + r'))'
    r'(?:\s+(?P<chap>Intro|ch\.\d+|Appendix(?:\s+\w+)?))?'
    r'(?:\s+(?P<kind>[lp])\.(?P<spec>\d+(?:-\d+|(?:\+\d+)+)?))?\s*$'
)

# Module-level cache: (book_abbrev, chap_key) → Path. chap_key is "intro",
# "ch.1", "ch.13", "appendix" or "" (monolith). Built lazily by
# _build_canon_ref_index().
_CANON_REF_INDEX: dict[tuple[str, str], Path] | None = None


def _build_canon_ref_index() -> dict[tuple[str, str], Path]:
    """Walk Source/ once and build {(abbrev, chapter_key): markdown_path}.

    chapter_key forms:
      "intro"        — file named "Introduction"/"Front Matter"-ish
      "ch.N"         — file with explicit chapter number
      "appendix"     — first appendix-labelled file (rough heuristic)
      ""             — fallback for the volume / monolithic file
    """
    global _CANON_REF_INDEX
    if _CANON_REF_INDEX is not None:
        return _CANON_REF_INDEX
    idx: dict[tuple[str, str], Path] = {}
    if not SOURCE_DIR.exists():
        _CANON_REF_INDEX = idx
        return idx

    # Regex for parsed chapter from filename
    # "04 - Chapter 1 - Dirigible in Danger.md"  → ch.1
    # "04 - Chapter 1 BÖGENHAFEN TO ALTDORF.md"  → ch.1
    # "08 - CHAPTER 1- EASTER EGGS.md"           → ch.1
    # "03 - Introduction.md"                      → intro
    # "17 - An Introduction to the History..."   → also intro fallback (low-priority)
    chap_re = re.compile(
        r'^\s*\d+\s*-\s*(?:CHAPTER|Chapter|chapter)\s*(\d+)\b',
        re.IGNORECASE)
    intro_re = re.compile(
        r'^\s*\d+\s*-\s*(?:Introduction|INTRODUCTION|Front\s*Matter)',
        re.IGNORECASE)
    appendix_re = re.compile(
        r'^\s*\d+\s*-\s*(?:Appendix|APPENDIX)\b',
        re.IGNORECASE)
    # Fallback: file numbered "NN - <name>" that isn't Index/Credits/Contents/
    # Bibliography/Glossary — treat NN as the chapter key. Lets refs like
    # `EiR ch.17` resolve to "17 - An Introduction to the History of the Turmoil.md"
    # (EiR appendix-like section, not named "Chapter N").
    numbered_section_re = re.compile(
        r'^\s*(\d+)\s*-\s*(?!.*(?:Index|Credits|Contents|Bibliography|Glossary|Front\s*Matter|Foreword|Foreward))',
        re.IGNORECASE)

    for abbrev, subdir in _CANON_BOOK_DIRS.items():
        book_dir = SOURCE_DIR / subdir
        if not book_dir.exists():
            continue
        md_files = sorted(book_dir.glob("*.md"))
        # Skip the per-folder "00 - Index.md"
        md_files = [f for f in md_files if not f.name.lower().startswith("00 - index")]
        if not md_files:
            continue

        # Fallback monolith: empty chapter key points to the first non-index file.
        idx[(abbrev, "")] = md_files[0]

        for f in md_files:
            stem = f.stem
            m = chap_re.match(stem)
            if m:
                key = f"ch.{int(m.group(1))}"
                idx.setdefault((abbrev, key), f)
                continue
            if intro_re.match(stem):
                idx.setdefault((abbrev, "intro"), f)
                continue
            if appendix_re.match(stem):
                idx.setdefault((abbrev, "appendix"), f)
                continue
            m = numbered_section_re.match(stem)
            if m:
                key = f"ch.{int(m.group(1))}"
                idx.setdefault((abbrev, key), f)
                continue

    _CANON_REF_INDEX = idx
    return idx


def _canon_extract_lines(path: Path, kind: str, spec: str,
                         max_chars: int = 700) -> str:
    """Read the cited region from a Source/ markdown file. Returns the
    extracted snippet (truncated with ellipsis), or '' on failure.

    kind = "l" → line numbers, 1-indexed (Notes MJ convention matches the
                  PyCharm-style "Goto line" used to record refs).
    kind = "p" → page numbers; PDF→MD conversion lost pagination, so we
                  return an empty string (popover will show a note).
    """
    if kind == "p":
        return ""  # not resolvable post-conversion
    if kind != "l":
        return ""
    if not path.exists():
        return ""
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return ""
    n = len(lines)

    # Parse "205", "205-218" (range) or "215+217[+220…]" (non-contiguous lines).
    # Multi-plus: each cited line is treated as a separate citation; we widen
    # each to its paragraph and concatenate with a separator.
    if "+" in spec:
        parts = spec.split("+")
        try:
            line_nums = sorted(set(int(p) for p in parts))
        except ValueError:
            return ""
        snippets: list[str] = []
        for ln in line_nums:
            if 1 <= ln <= n:
                end_p = ln
                for i in range(ln, min(n, ln + 10)):
                    if not lines[i].strip():
                        break
                    end_p = i + 1
                snippets.append("\n".join(lines[ln - 1:end_p]))
        if not snippets:
            return ""
        snippet = "\n\n…\n\n".join(snippets)
        if len(snippet) > max_chars:
            snippet = snippet[:max_chars].rsplit(" ", 1)[0] + "…"
        return snippet
    if "-" in spec:
        a, b = spec.split("-", 1)
        try:
            start, end = int(a), int(b)
        except ValueError:
            return ""
    else:
        try:
            start = int(spec)
        except ValueError:
            return ""
        end = start

    # Bounds + small context fence: include the cited range as-is.
    start = max(1, start)
    end = min(n, max(start, end))
    if start > n:
        return ""
    snippet = "\n".join(lines[start - 1:end])
    # Single-line refs (l.NNN) often land on a heading or a paragraph start
    # which by itself is uninformative. Widen to the end of the paragraph
    # (until next blank line or +10 lines max). Also include a heading + its
    # first paragraph when start lands on a heading line.
    if start == end:
        # widen forward up to the next blank line (paragraph boundary) or +10
        forward_end = end
        for i in range(end, min(n, end + 10)):
            if not lines[i].strip():  # blank line stops the paragraph
                break
            forward_end = i + 1
        # if the cited line is a heading "## Title", continue past the
        # blank line that follows to include the first content paragraph
        if lines[end - 1].lstrip().startswith("#"):
            j = forward_end
            # skip blank lines
            while j < n and not lines[j].strip():
                j += 1
            # include next paragraph
            for i in range(j, min(n, j + 10)):
                if not lines[i].strip():
                    break
                forward_end = i + 1
        snippet = "\n".join(lines[start - 1:forward_end])
    # If the cited region is still empty (single-line ref on a blank line),
    # widen symmetrically as a last resort.
    if not snippet.strip():
        ctx_start = max(1, start - 2)
        ctx_end = min(n, end + 3)
        snippet = "\n".join(lines[ctx_start - 1:ctx_end])
    snippet = snippet.strip()
    if len(snippet) > max_chars:
        snippet = snippet[:max_chars].rstrip() + "…"
    return snippet


def _resolve_canon_ref(ref_text: str) -> tuple[str, str, str] | None:
    """Parse `ref_text` (e.g. "EiR Intro l.205-218") and return
    (display_label, source_label, extract). source_label is a short hint
    like "EiR · Introduction" shown in the popover header.
    Returns None if the ref doesn't match a known book/chapter.
    Kept for backwards compatibility; prefer _resolve_canon_ref_full()."""
    full = _resolve_canon_ref_full(ref_text)
    if full is None:
        return None
    return full[0], full[1], full[2]


def _resolve_canon_ref_full(ref_text: str) -> tuple[str, str, str, str, str, int] | None:
    """Like _resolve_canon_ref but also returns navigation metadata:
    (display, source_label, extract, book_abbrev, file_path, start_line).
    `file_path` is the absolute Path of the matched Source/ markdown file
    (as a string for hashability); `start_line` is 1-indexed (0 if the ref
    has no line spec — landing page only).
    Returns None if the ref doesn't match a known book/chapter."""
    m = _CANON_REF_RE.match(ref_text.strip())
    if not m:
        return None
    book = m.group("book")
    chap = (m.group("chap") or "").strip()
    kind = m.group("kind") or ""
    spec = m.group("spec") or ""

    idx = _build_canon_ref_index()

    # Determine chapter key for index lookup
    chap_lower = chap.lower()
    if chap_lower == "intro":
        chap_key = "intro"
    elif chap_lower.startswith("ch."):
        chap_key = chap_lower  # already "ch.13"
    elif chap_lower.startswith("appendix"):
        chap_key = "appendix"
    else:
        chap_key = ""  # monolith fallback

    path = idx.get((book, chap_key))
    if path is None:
        # Fallback to monolith if a specific chapter isn't indexed
        path = idx.get((book, ""))
    if path is None:
        return None

    # Pretty source label
    if chap_key == "intro":
        src_label = f"{book} · Introduction"
    elif chap_key == "appendix":
        src_label = f"{book} · Appendix"
    elif chap_key.startswith("ch."):
        src_label = f"{book} · Chapter {chap_key[3:]}"
    else:
        src_label = book

    extract = ""
    start_line = 0
    if kind and spec:
        extract = _canon_extract_lines(path, kind, spec)
        if not extract and kind == "p":
            extract = "(page reference — PDF pagination lost during markdown conversion)"
        if kind == "l":
            # Parse start line from spec ("205", "205-218", "205+207")
            for sep in ("-", "+"):
                if sep in spec:
                    head = spec.split(sep, 1)[0]
                    try:
                        start_line = int(head)
                    except ValueError:
                        start_line = 0
                    break
            else:
                try:
                    start_line = int(spec)
                except ValueError:
                    start_line = 0
    return ref_text, src_label, extract, book, str(path), start_line


def _canon_book_slug(abbrev: str) -> str:
    """Map a canon book abbreviation ("EiR", "EiS Companion") → URL slug.
    Slugs are stable: lowercase, ASCII, hyphens. Used for the source page
    output paths under mj-{TOKEN}/source/{slug}/…"""
    return _mj_slug(abbrev)


# Reverse map: file Path (str) → (book_slug, file_slug, book_abbrev).
# Populated lazily by _build_canon_source_route_map() on first access.
_CANON_SOURCE_ROUTE_MAP: dict[str, tuple[str, str, str]] | None = None


def _build_canon_source_route_map() -> dict[str, tuple[str, str, str]]:
    """Build {abs_path_str: (book_slug, file_slug, book_abbrev)} for every
    Source/ markdown file covered by _CANON_BOOK_DIRS. Used both to
    generate the MJ source pages and to compute canon-ref hrefs."""
    global _CANON_SOURCE_ROUTE_MAP
    if _CANON_SOURCE_ROUTE_MAP is not None:
        return _CANON_SOURCE_ROUTE_MAP
    out: dict[str, tuple[str, str, str]] = {}
    if not SOURCE_DIR.exists():
        _CANON_SOURCE_ROUTE_MAP = out
        return out
    for abbrev, subdir in _CANON_BOOK_DIRS.items():
        book_dir = SOURCE_DIR / subdir
        if not book_dir.exists():
            continue
        book_slug = _canon_book_slug(abbrev)
        for f in sorted(book_dir.glob("*.md")):
            if f.name.lower().startswith("00 - index"):
                continue
            file_slug = _mj_slug(f.stem)
            out[str(f)] = (book_slug, file_slug, abbrev)
    _CANON_SOURCE_ROUTE_MAP = out
    return out


def _canon_source_href(source_path: str, start_line: int,
                       current_dir: Path) -> str | None:
    """Return a relative URL from `current_dir` to the rendered Source page
    for `source_path` (an absolute path to a Source/*.md file), with #LN
    fragment if start_line > 0. None if MJ overlay isn't configured or the
    path isn't in the route map."""
    if not MJ_TOKEN:
        return None
    routes = _build_canon_source_route_map()
    info = routes.get(source_path)
    if info is None:
        return None
    book_slug, file_slug, _abbrev = info
    target = Path(f"mj-{MJ_TOKEN}") / "source" / book_slug / f"{file_slug}.html"
    url = relative_url(current_dir, target)
    if start_line > 0:
        url += f"#L{start_line}"
    return url


def inject_canon_refs(html_body: str, current_dir: Path | None = None) -> str:
    """Find <code>BOOK CHAP l.NNN</code> elements that match a known canon
    reference pattern and convert them to interactive `<a class="canon-ref"
    href="…" data-extract="…">` triggers. The popover (hover) shows the
    cited markdown lines; the link (click) navigates to the rendered Source
    page at the cited line anchor. No-op on text that doesn't match the
    pattern, so unrelated <code> blocks (e.g. statbloc abbreviations) are
    left alone.

    `current_dir` is the path of the page being rendered (relative to OUT)
    and is used to compute relative URLs. If omitted, the canon-ref is
    rendered as a non-clickable <span> (legacy behaviour).

    Should only be called on MJ-rendered HTML (autonomous overlay pages and
    the .mj-entity-enrichment section of public pages)."""
    if not html_body:
        return html_body
    if "<code>" not in html_body and "<code " not in html_body:
        return html_body

    soup = BeautifulSoup(html_body, 'html.parser')
    changed = False
    counter = 0
    for code_el in list(soup.find_all('code')):
        # Don't touch code inside <pre> (fenced blocks)
        if code_el.find_parent('pre') is not None:
            continue
        text = code_el.get_text("", strip=True)
        if not text:
            continue
        resolved = _resolve_canon_ref_full(text)
        if resolved is None:
            continue
        counter += 1
        display, src_label, extract, _book, src_path, start_line = resolved
        # Header shown in the popover combines the book/chapter label and the
        # exact line spec — gives the reader the full citation even though the
        # in-text marker is just a superscript number.
        header = f"{src_label} — {display}" if src_label else display
        attrs = {
            'class': 'canon-ref',
            'data-source': header,
            'data-extract': extract or '(extrait indisponible)',
            'title': display,  # native tooltip = full ref for accessibility
        }
        href = None
        if current_dir is not None:
            href = _canon_source_href(src_path, start_line, current_dir)
        if href:
            attrs['href'] = href
            new_el = soup.new_tag('a', attrs=attrs)
        else:
            new_el = soup.new_tag('span', attrs=attrs)
        # In-text marker: footnote-style superscript number
        sup = soup.new_tag('sup')
        sup.string = str(counter)
        new_el.append(sup)
        code_el.replace_with(new_el)
        changed = True

    if not changed:
        return html_body
    return str(soup)


# --------------------------------------------------------------------------- #
# HTML body rewriting — keep blog HTML, fix internal links only
# --------------------------------------------------------------------------- #


_HREF = re.compile(r'href\s*=\s*(["\'])([^"\']+)\1', re.IGNORECASE)


def rewrite_html_links(body_html: str, current: Page,
                       url_map: dict[str, Path],
                       label_map: dict[str, Path]) -> str:
    """Rewrite blog-internal hrefs in an HTML body to relative site paths."""
    current_dir = current.site_rel.parent  # e.g. Path("resumes")

    def repl(m: re.Match) -> str:
        quote, url = m.group(1), m.group(2)
        target = resolve_link(url, url_map, label_map)
        if target is None:
            return m.group(0)
        rel = link_for_path(target, current_dir)
        return f'href={quote}{rel}{quote}'

    return _HREF.sub(repl, body_html)


def resolve_link(url: str, url_map: dict[str, Path],
                 label_map: dict[str, Path]) -> Path | None:
    parsed = urlparse(url.strip())
    if parsed.netloc and parsed.netloc.lower() != BLOG_HOST:
        return None
    direct = url_map.get(_normalise_url(url))
    if direct is not None:
        return direct
    m = _SEARCH_LABEL.match(parsed.path)
    if m:
        name = unquote(m.group(1)).strip()
        return label_map.get(name)
    return None


_MAIN_FOR_GROUP: dict[tuple[str, str], 'Page'] = {}
_PATH_TO_PAGE: dict[Path, 'Page'] = {}


def link_for(target: 'Page', from_dir: Path) -> str:
    """URL from `from_dir` to `target`. Variants redirect to their group's
    main page + a `#variant-<slug>` anchor — so clicking 'Elvira, batelière'
    lands on `/pj/elvira.html#variant-elvira-bateliere`."""
    if target.variant_group and not target.is_main:
        main = _MAIN_FOR_GROUP.get((target.post.folder, target.variant_group))
        if main is not None:
            return f"{relative_url(from_dir, main.site_rel)}#variant-{target.slug}"
    return relative_url(from_dir, target.site_rel)


def link_for_path(target_path: Path, from_dir: Path) -> str:
    """Like link_for() but resolves a Path → Page first (variant redirection)."""
    pg = _PATH_TO_PAGE.get(target_path)
    if pg is not None:
        return link_for(pg, from_dir)
    return relative_url(from_dir, target_path)


# --------------------------------------------------------------------------- #
# HTML rendering — page templates
# --------------------------------------------------------------------------- #


SITE_TITLE = "Mon Ennemi Intérieur"
SITE_TAGLINE = "Chronique d'une campagne Warhammer Fantasy"

SEARCH_JS = r"""// Client-side search.  Loads search-index.json on first focus, then filters
// in-memory on every keystroke.  No deps.
(function () {
  var norm = function (s) {
    return s.normalize('NFD').replace(/[̀-ͯ]/g, '').toLowerCase();
  };
  var escapeHtml = function (s) {
    return String(s).replace(/[&<>"']/g, function (c) {
      return { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c];
    });
  };

  var index = null, indexPromise = null;
  function loadIndex(base) {
    if (indexPromise) return indexPromise;
    var promises = [fetch(base + 'search-index.json').then(function (r) { return r.json(); })];
    var mjToken = document.body && document.body.dataset.mjToken;
    if (mjToken) {
      promises.push(
        fetch(base + 'mj-' + mjToken + '/search-index.json')
          .then(function (r) { return r.ok ? r.json() : []; })
          .catch(function () { return []; })
      );
    }
    indexPromise = Promise.all(promises).then(function (results) {
      index = [].concat.apply([], results);
      return index;
    });
    return indexPromise;
  }

  function render(matches, base) {
    if (!matches.length) {
      return '<div class="search-empty">Aucun résultat.</div>';
    }
    return matches.slice(0, 14).map(function (m) {
      var e = m.entry;
      var thumb = e.i
        ? '<img class="search-thumb" src="' + escapeHtml(e.i) + '" alt="" loading="lazy">'
        : '<span class="search-thumb search-thumb-fallback">' +
          escapeHtml(e.n != null ? String(e.n).padStart(2, '0') : (e.t.charAt(0) || '·')) +
          '</span>';
      var cat = e.c ? '<span class="search-cat">' + escapeHtml(e.c) + '</span>' : '';
      var mjBadge = e.mj ? '<span class="search-mj-badge">MJ</span>' : '';
      var cls = 'search-result' + (e.mj ? ' search-result-mj' : '');
      return '<a class="' + cls + '" href="' + escapeHtml(base + e.u) + '">' +
             thumb +
             '<span class="search-meta"><span class="search-title">' + escapeHtml(e.t) + mjBadge + '</span>' + cat + '</span>' +
             '</a>';
    }).join('');
  }

  // --- Single source of truth for ranking & filtering -------------------
  // Change MIN_SCORE here (or the score thresholds below) and both the
  // dropdown and the /search/ results page pick it up.
  var MIN_SCORE = 50;
  function score(entry, qNorm) {
    var titleN = norm(entry.t);
    if (titleN === qNorm) return 100;             // exact title
    if (titleN.startsWith(qNorm)) return 80;      // title-prefix
    if (titleN.indexOf(qNorm) >= 0) return 60;    // title contains
    if (entry.vt) {                               // variant-title match
      for (var i = 0; i < entry.vt.length; i++) {
        if (entry.vt[i].indexOf(qNorm) >= 0) return 50;
      }
    }
    if (entry.s.indexOf(qNorm) >= 0) return 20;   // body / haystack
    return 0;
  }
  function findMatches(qNorm) {
    var out = [];
    for (var i = 0; i < index.length; i++) {
      var sc = score(index[i], qNorm);
      if (sc >= MIN_SCORE) out.push({ entry: index[i], score: sc });
    }
    out.sort(function (a, b) {
      if (b.score !== a.score) return b.score - a.score;
      return a.entry.t.localeCompare(b.entry.t, 'fr');
    });
    return out;
  }

  function attach(input, results, base) {
    var open = function () { results.classList.add('is-open'); };
    var close = function () { results.classList.remove('is-open'); };

    input.addEventListener('focus', function () { loadIndex(base); });

    input.addEventListener('input', function () {
      var q = input.value.trim();
      if (q.length < 2) { close(); results.innerHTML = ''; return; }
      var qN = norm(q);
      loadIndex(base).then(function () {
        var matches = findMatches(qN);
        results.innerHTML = render(matches, base);
        open();
      });
    });

    input.addEventListener('keydown', function (e) {
      if (e.key === 'Escape') { close(); input.blur(); }
      if (e.key === 'Enter') {
        var q = input.value.trim();
        if (q) {
          // Full results page rather than jumping to the top match.
          window.location.href = base + 'search/?q=' + encodeURIComponent(q);
        }
      }
    });

    document.addEventListener('click', function (e) {
      if (!input.contains(e.target) && !results.contains(e.target)) close();
    });
  }

  // ---------- Search results PAGE (`/search/?q=...`) -------------------

  var CATEGORY_ORDER = ['Résumés', 'PJ', 'PNJ', 'Lieux', 'Documents', 'Univers'];

  function renderSearchPage() {
    var container = document.getElementById('search-page-results');
    var titleEl   = document.getElementById('search-page-query');
    if (!container || !titleEl) return;

    var base = container.dataset.base || '';
    var params = new URLSearchParams(window.location.search);
    var q = (params.get('q') || '').trim();

    // Mirror the query in the page-top input so the user can refine it.
    var pageInput = document.querySelector('.search-input');
    if (pageInput) pageInput.value = q;

    if (!q) {
      titleEl.textContent = 'Tapez une requête dans la barre du haut.';
      return;
    }

    titleEl.innerHTML = 'Résultats pour « <em>' + escapeHtml(q) + '</em> »';

    var qN = norm(q);
    loadIndex(base).then(function () {
      var matches = findMatches(qN);
      if (!matches.length) {
        container.innerHTML = '<p class="search-empty">Aucun résultat.</p>';
        return;
      }

      var byCat = {};
      matches.forEach(function (m) {
        var c = m.entry.c || 'Autres';
        (byCat[c] = byCat[c] || []).push(m);
      });
      // categories not in CATEGORY_ORDER go at the end, alphabetical
      var order = CATEGORY_ORDER.slice();
      Object.keys(byCat).forEach(function (c) {
        if (order.indexOf(c) === -1) order.push(c);
      });

      var out = [];
      order.forEach(function (cat) {
        var list = byCat[cat];
        if (!list) return;
        out.push('<section class="search-group">');
        out.push('<h2 class="search-group-title">' + escapeHtml(cat) +
                 '<span class="count">' + list.length + '</span></h2>');
        out.push('<ul class="card-grid card-grid-entries">');
        list.forEach(function (m) {
          var e = m.entry;
          var thumb = e.i
            ? '<div class="thumb-wrap"><img class="thumb" loading="lazy" src="' +
              escapeHtml(e.i) + '" alt=""></div>'
            : '<div class="thumb-wrap thumb-fallback"><span>' +
              escapeHtml(e.n != null ? String(e.n).padStart(2, '0')
                                       : (e.t.charAt(0) || '·')) +
              '</span></div>';
          out.push('<li><a class="thumb-card entry-card" href="' +
                   escapeHtml(base + e.u) + '">' + thumb +
                   '<div class="thumb-card-body"><span class="entry-name">' +
                   escapeHtml(e.t) + '</span></div></a></li>');
        });
        out.push('</ul></section>');
      });
      container.innerHTML = out.join('');
    });
  }

  document.addEventListener('DOMContentLoaded', function () {
    var input = document.querySelector('.search-input');
    var results = document.querySelector('.search-results');
    if (input && results) attach(input, results, input.dataset.base || '');
    renderSearchPage();
  });

  // ---------- Image lightbox -------------------------------------------
  // Blogger wraps post images in <a href="...full-size.webp"><img></a>.
  // Without intervention, clicking navigates away from the site to a raw
  // image URL. Intercept the click and show the image in an in-page modal.

  var IMG_EXT = /\.(png|jpe?g|gif|webp|svg|avif)(\?.*)?$/i;

  function openLightbox(href, alt) {
    var overlay = document.createElement('div');
    overlay.className = 'lightbox-overlay';
    var img = document.createElement('img');
    img.className = 'lightbox-img';
    img.src = href;
    img.alt = alt || '';
    var btn = document.createElement('button');
    btn.className = 'lightbox-close';
    btn.setAttribute('aria-label', 'Fermer');
    btn.innerHTML = '&times;';
    overlay.appendChild(img);
    overlay.appendChild(btn);
    document.body.appendChild(overlay);
    document.body.classList.add('lightbox-open');

    function close() {
      overlay.remove();
      document.body.classList.remove('lightbox-open');
      document.removeEventListener('keydown', onKey);
    }
    function onKey(e) { if (e.key === 'Escape') close(); }

    overlay.addEventListener('click', function (e) {
      // Click on the image itself shouldn't close; clicking elsewhere does.
      if (e.target !== img) close();
    });
    document.addEventListener('keydown', onKey);
  }

  document.addEventListener('click', function (e) {
    // Catch clicks anywhere inside an <a> that wraps a post image — not just
    // clicks on the <img> itself. The anchor often has padding / line-height
    // that extends past the image edges, and clicking that border would
    // otherwise follow the link to the Blogger CDN and leave the site.
    var target = e.target;
    if (!target || !target.closest) return;
    var anchor = target.closest('a');
    if (!anchor) return;
    var href = anchor.getAttribute('href');
    if (!href || !IMG_EXT.test(href)) return;
    var img = anchor.querySelector('img');
    if (!img) return;
    e.preventDefault();
    openLightbox(href, img.getAttribute('alt'));
  });

  // ---------- Entity popovers -----------------------------------------
  // Triggered by hover on <a class="entity-pop" data-portrait="…">. Mobile
  // devices fall back to plain link navigation (CSS hides the popover via
  // `@media (hover: none)`).

  var popover = null, showTimer = null, hideTimer = null;

  function ensurePopover() {
    if (popover) return popover;
    popover = document.createElement('div');
    popover.className = 'entity-popover';
    popover.addEventListener('mouseenter', function () {
      if (hideTimer) { clearTimeout(hideTimer); hideTimer = null; }
    });
    popover.addEventListener('mouseleave', schedulePopoverHide);
    document.body.appendChild(popover);
    return popover;
  }

  function fillPopover(trigger) {
    var p = ensurePopover();
    var portrait = trigger.getAttribute('data-portrait');
    var title    = trigger.getAttribute('data-title')    || trigger.textContent.trim();
    var cat      = trigger.getAttribute('data-cat')      || '';
    var subtitle = trigger.getAttribute('data-subtitle') || '';
    p.innerHTML = '';
    if (portrait) {
      var img = document.createElement('img');
      img.src = portrait;
      img.alt = '';
      img.loading = 'lazy';
      p.appendChild(img);
    }
    // Eyebrow + name + subtitle, mirroring entry_card_html on the server.
    var body = document.createElement('span');
    body.className = 'entity-popover-body';
    function addLine(cls, text) {
      if (!text) return;
      var el = document.createElement('span');
      el.className = cls;
      el.textContent = text;
      body.appendChild(el);
    }
    addLine('entity-popover-cat',      cat);
    addLine('entity-popover-name',     title);
    addLine('entity-popover-subtitle', subtitle);
    p.appendChild(body);
    p.style.display = 'flex';
    positionPopover(p, trigger);
  }

  function positionPopover(p, trigger) {
    var rect = trigger.getBoundingClientRect();
    var pw = p.offsetWidth, ph = p.offsetHeight;
    var top  = rect.top - ph - 8;
    var left = rect.left + rect.width / 2 - pw / 2;
    if (top < 8) top = rect.bottom + 8;             // flip below if no room above
    if (left < 8) left = 8;
    var maxLeft = window.innerWidth - pw - 8;
    if (left > maxLeft) left = maxLeft;
    p.style.top = top + 'px';
    p.style.left = left + 'px';
  }

  function schedulePopoverShow(trigger) {
    if (hideTimer) { clearTimeout(hideTimer); hideTimer = null; }
    if (showTimer) clearTimeout(showTimer);
    showTimer = setTimeout(function () { fillPopover(trigger); }, 150);
  }
  function schedulePopoverHide() {
    if (showTimer) { clearTimeout(showTimer); showTimer = null; }
    if (hideTimer) clearTimeout(hideTimer);
    hideTimer = setTimeout(function () {
      if (popover) popover.style.display = 'none';
    }, 200);
  }

  document.addEventListener('mouseover', function (e) {
    var t = e.target.closest && e.target.closest('.entity-pop');
    if (t) schedulePopoverShow(t);
  });
  document.addEventListener('mouseout', function (e) {
    var t = e.target.closest && e.target.closest('.entity-pop');
    if (t) schedulePopoverHide();
  });
})();
"""


def _thumb_html(pg: Page) -> str:
    """Return an <img> tag or a placeholder for a card thumbnail."""
    if pg.thumbnail:
        return (f'<div class="thumb-wrap"><img class="thumb" loading="lazy" '
                f'src="{html.escape(pg.thumbnail)}" alt=""></div>')
    # No image — render a stylised letter/icon as fallback.
    if pg.session_num is not None:
        glyph = f"{pg.session_num:02d}"
    else:
        glyph = html.escape(pg.post.title[:1].upper()) or "·"
    return f'<div class="thumb-wrap thumb-fallback"><span>{glyph}</span></div>'


def to_roman(n: int) -> str:
    table = [(1000,'M'),(900,'CM'),(500,'D'),(400,'CD'),
             (100,'C'),(90,'XC'),(50,'L'),(40,'XL'),
             (10,'X'),(9,'IX'),(5,'V'),(4,'IV'),(1,'I')]
    out, n = '', max(0, n)
    for v, s in table:
        while n >= v:
            out += s; n -= v
    return out or 'O'


# Decorative SVG fleuron used between sections — vine-and-rosette ornament.
FLEURON_SVG = (
    '<svg class="fleuron" viewBox="0 0 220 30" aria-hidden="true">'
    # left vine — flourish curling toward center
    '<path class="vine" d="M2 15 C 30 15, 30 6, 60 8 C 75 9, 80 15, 90 15" />'
    '<path class="vine" d="M28 15 C 36 15, 38 21, 42 21" />'
    # left berry/leaf accent
    '<circle class="berry" cx="42" cy="21.5" r="1.4" />'
    '<circle class="berry" cx="56" cy="8.5" r="1.4" />'
    # right vine — mirror
    '<path class="vine" d="M218 15 C 190 15, 190 6, 160 8 C 145 9, 140 15, 130 15" />'
    '<path class="vine" d="M192 15 C 184 15, 182 21, 178 21" />'
    '<circle class="berry" cx="178" cy="21.5" r="1.4" />'
    '<circle class="berry" cx="164" cy="8.5" r="1.4" />'
    # central rosette (4-petal)
    '<g class="rosette" transform="translate(110 15)">'
    '  <path d="M0 -8 C 4 -4, 4 4, 0 8 C -4 4, -4 -4, 0 -8 Z" />'
    '  <path d="M-8 0 C -4 -4, 4 -4, 8 0 C 4 4, -4 4, -8 0 Z" />'
    '  <circle r="2.2" class="rosette-core" />'
    '</g>'
    '</svg>'
)

# A tiny vine bracket used at the corners of arc cards.
CORNER_SVG_TL = (
    '<svg class="corner corner-tl" viewBox="0 0 24 24" aria-hidden="true">'
    '<path d="M2 12 C 2 6, 6 2, 12 2" /></svg>'
)
CORNER_SVG_BR = (
    '<svg class="corner corner-br" viewBox="0 0 24 24" aria-hidden="true">'
    '<path d="M22 12 C 22 18, 18 22, 12 22" /></svg>'
)


def _render_sidebar(current_dir: Path,
                    buckets: dict[int, ArcBucket] | None,
                    active_arc: int | None) -> str:
    """Left rail with the 7 arcs. Sub-categories show only for the active arc."""
    parts = ['<nav class="sidebar" aria-label="Navigation par arc">',
             '<h2 class="sidebar-title">Les Sept Arcs</h2>',
             '<ol class="sidebar-arcs">']
    for arc in ARCS:
        href = relative_url(current_dir, Path(f"arc-{arc.num}.html"))
        is_active = active_arc == arc.num
        li_class = ' class="is-active"' if is_active else ''
        parts.append(f'<li{li_class}>')
        parts.append(
            f'<a class="sidebar-arc" href="{html.escape(href)}">'
            f'<span class="sidebar-num">{to_roman(arc.num)}</span>'
            f'<span class="sidebar-name">{html.escape(arc.title)}</span></a>')
        bucket = (buckets or {}).get(arc.num)
        if is_active and bucket:
            sub_items = []
            for out_folder, src_folder, label in CATEGORIES:
                items = bucket.by_folder.get(src_folder, [])
                if not items:
                    continue
                anchor = f"{href}#{out_folder}"
                sub_items.append(
                    f'<a href="{html.escape(anchor)}">'
                    f'<span>{html.escape(label)}</span>'
                    f'<span class="count">{len(items)}</span></a>')
            if sub_items:
                parts.append('<div class="sidebar-sub">')
                parts.extend(sub_items)
                parts.append('</div>')
        parts.append('</li>')
    parts.append('</ol>')
    parts.append('</nav>')
    return "\n".join(parts)


def _render_og_block(title: str, og: OgMeta | None) -> str:
    """Open Graph + Twitter card meta tags for a single page."""
    if og is None:
        return ""
    lines = [
        f'<meta property="og:site_name" content="{html.escape(SITE_TITLE)}">',
        f'<meta property="og:title" content="{html.escape(title)}">',
        f'<meta property="og:type" content="{html.escape(og.og_type)}">',
    ]
    if og.url:
        lines.append(f'<meta property="og:url" content="{html.escape(og.url)}">')
        lines.append(f'<link rel="canonical" href="{html.escape(og.url)}">')
    if og.description:
        lines.append(f'<meta property="og:description" content="{html.escape(og.description)}">')
        lines.append(f'<meta name="description" content="{html.escape(og.description)}">')
    if og.image:
        lines.append(f'<meta property="og:image" content="{html.escape(og.image)}">')
        lines.append('<meta name="twitter:card" content="summary_large_image">')
    else:
        lines.append('<meta name="twitter:card" content="summary">')
    return "\n".join(lines)


def layout(current_dir: Path, title: str, body: str,
           extra_class: str = "",
           buckets: dict[int, ArcBucket] | None = None,
           active_arc: int | None = None,
           og: OgMeta | None = None,
           sidebar_override: str | None = None) -> str:
    """Wrap a body fragment in the site shell with sidebar nav.

    `sidebar_override`: pre-rendered <nav class="sidebar"> HTML to use instead
    of the default 7-arcs rail (used for the per-scenario navigation)."""
    css = relative_url(current_dir, Path("style.css"))
    home = relative_url(current_dir, Path("index.html"))
    search_js = relative_url(current_dir, Path("search.js"))
    # Base path back to the site root, for the search JS's absolute-ish URLs.
    site_base = "../" * len(current_dir.parts)

    top_items = []
    for out_folder, _src_folder, label in CATEGORIES:
        href = relative_url(current_dir, Path(out_folder) / "index.html")
        top_items.append(f'<a href="{html.escape(href)}">{html.escape(label)}</a>')
    top_nav = " · ".join(top_items)

    # MJ-only nav entries — appended to the public nav, hidden by CSS until
    # MJ mode is toggled. Each separator+link is wrapped in .mj-only so it
    # disappears cleanly when not in MJ mode.
    mj_extras = ""
    if MJ_TOKEN:
        mj_root = Path(f"mj-{MJ_TOKEN}")
        mj_links = [
            ("Scénarios", mj_root / "scenarios" / "index.html"),
            ("Cartes", mj_root / "cartes" / "index.html"),
            ("Notes MJ", mj_root / "notes" / "index.html"),
        ]
        mj_extras_parts = []
        for label, path in mj_links:
            href = relative_url(current_dir, path)
            mj_extras_parts.append(
                f'<span class="mj-only"> · <a href="{html.escape(href)}">'
                f'{html.escape(label)}</a></span>')
        mj_extras = "".join(mj_extras_parts)

    sidebar = (sidebar_override if sidebar_override is not None
               else _render_sidebar(current_dir, buckets, active_arc))
    og_block = _render_og_block(title, og)

    return f"""<!doctype html>
<html lang="fr">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>{html.escape(title)} — {html.escape(SITE_TITLE)}</title>
{og_block}
<link rel="preconnect" href="https://fonts.googleapis.com">
<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link rel="stylesheet" href="https://fonts.googleapis.com/css2?family=IM+Fell+English+SC&family=IM+Fell+English:ital@0;1&family=EB+Garamond:ital,wght@0,400;0,600;1,400&display=swap">
<link rel="stylesheet" href="{html.escape(css)}">
<script src="{html.escape(relative_url(current_dir, Path('mj-mode.js')))}" defer></script>
</head>
<body class="{extra_class}">
<header class="site-header">
  <a class="site-title" href="{html.escape(home)}">
    <span class="wordmark">{html.escape(SITE_TITLE)}</span>
  </a>
  <div class="search" role="search">
    <input class="search-input" type="search" autocomplete="off" spellcheck="false"
           placeholder="Rechercher…" aria-label="Rechercher dans le site"
           data-base="{html.escape(site_base)}">
    <div class="search-results" role="listbox" aria-label="Résultats de recherche"></div>
  </div>
  <nav class="site-nav">{top_nav}{mj_extras}</nav>
  <button class="menu-toggle" type="button" aria-label="Ouvrir le menu" onclick="document.body.classList.toggle('menu-open')">☰</button>
</header>
<div class="layout">
{sidebar}
<main>
{body}
</main>
</div>
<footer class="site-footer">
  <div class="footer-flourish">{FLEURON_SVG}</div>
  <p>Campagne <em>Warhammer Fantasy Roleplay</em> 4ᵉ édition — Notes de partie.</p>
</footer>
<script src="{html.escape(search_js)}" defer></script>
</body>
</html>
"""


def render_home(buckets: dict[int, ArcBucket],
                pages_by_cat: dict[str, list[Page]],
                pages: list[Page],
                url_map: dict[str, Path],
                label_map: dict[str, Path]) -> str:
    body = [
        '<section class="hero">',
        '<p class="hero-eyebrow">Chronique de campagne</p>',
        '<h1 class="hero-title">Mon Ennemi Intérieur</h1>',
        f'<div class="hero-flourish">{FLEURON_SVG}</div>',
        '</section>',
    ]

    # Préface from the blog: original foreword text + entry-point links.
    preface_pg = next((p for p in pages
                       if p.post.title.strip() == "Préface"
                       and p.post.folder == "Tomes"), None)
    if preface_pg:
        # Step 1: rewrite the 3 archive-URL entry-points (these are Blogger
        # month archives, not individual posts, so url_map doesn't carry them).
        preface_html = preface_pg.post.html
        for blog_url, local_url in [
            ("https://monennemiinterieur.blogspot.com/2024/10/", "arc-1.html"),
            ("https://monennemiinterieur.blogspot.com/2023/",    "pj/index.html"),
            ("https://monennemiinterieur.blogspot.com/2018/",    "univers/index.html"),
        ]:
            preface_html = preface_html.replace(blog_url, local_url)

        # Step 2: run the standard link rewriter so /search/label/X URLs in
        # the preface (e.g. "Fin de l'épisode 62" → session 62) get mapped
        # to their local pages.
        home_proxy = Page(post=preface_pg.post, out_path=Path(),
                          site_rel=Path("index.html"),
                          slug="index", session_num=None)
        preface_html = rewrite_html_links(preface_html, home_proxy,
                                           url_map, label_map)

        body.append('<section class="preface">')
        body.append(f'<div class="preface-body">{preface_html}</div>')
        body.append('</section>')

    body.append('<section class="arcs-section">')
    body.append('<h2 class="section-title">Les Sept Arcs</h2>')
    body.append('<ol class="arc-cards">')
    for arc in ARCS:
        b = buckets.get(arc.num)
        s_count = len(b.by_folder.get("Résumés", [])) if b else 0
        is_current = arc.num == ARCS[-1].num
        href = f"arc-{arc.num}.html"
        body.append(f'<li><a class="arc-card" href="{href}">')
        body.append(f'  {CORNER_SVG_TL}{CORNER_SVG_BR}')
        body.append(f'  <div class="arc-roman">{to_roman(arc.num)}</div>')
        body.append(f'  <div class="arc-card-body">')
        body.append(f'    <div class="arc-card-eyebrow">Arc {to_roman(arc.num)}'
                    f'{"  ·  En cours" if is_current else ""}</div>')
        body.append(f'    <h3 class="arc-card-title">{html.escape(arc.title)}</h3>')
        body.append(f'    <div class="arc-card-meta">'
                    f'Sessions {arc.s_start}–{arc.s_end}  ·  {s_count} récits</div>')
        body.append('  </div>')
        body.append('</a></li>')
    body.append('</ol></section>')

    body.append(f'<div class="divider">{FLEURON_SVG}</div>')
    body.append('<section class="cats-section">')
    body.append('<h2 class="section-title">Le Compendium</h2>')
    body.append('<p class="section-lead">Toutes les fiches de la campagne — '
                'personnages, lieux, documents et univers.</p>')
    body.append('<ul class="cat-cards">')
    for out_folder, src_folder, lbl in CATEGORIES:
        count = len(pages_by_cat.get(src_folder, []))
        body.append(f'<li><a class="cat-card" href="{out_folder}/index.html">'
                    f'<span class="cat-name">{html.escape(lbl)}</span>'
                    f'<span class="cat-count">{count}</span></a></li>')
    body.append('</ul></section>')

    # OG preview: take the preface body if available, otherwise the tagline.
    if preface_pg:
        og_desc = og_description(preface_pg.post.html)
        og_image = preface_pg.thumbnail
    else:
        og_desc = SITE_TAGLINE
        og_image = None
    og = OgMeta(description=og_desc, image=og_image,
                og_type="website", url=absolute_url("index.html"))

    return layout(Path("."), SITE_TITLE, "\n".join(body),
                  extra_class="page-home", buckets=buckets, og=og)


def render_arc_page(bucket: ArcBucket, buckets: dict[int, ArcBucket],
                    url_map: dict[str, Path],
                    label_map: dict[str, Path]) -> str:
    arc = bucket.arc
    s_count = len(bucket.by_folder.get("Résumés", []))
    parts = [
        '<header class="arc-header">',
        f'  <div class="arc-roman-large">{to_roman(arc.num)}</div>',
        f'  <p class="arc-eyebrow">Arc {to_roman(arc.num)}</p>',
        f'  <h1 class="arc-title">{html.escape(arc.title)}</h1>',
        f'  <p class="arc-meta">Sessions {arc.s_start}–{arc.s_end}  ·  '
        f'{s_count} récits</p>',
        '</header>',
        f'<div class="divider">{FLEURON_SVG}</div>',
    ]

    # Intro post body (HTML preserved). The first letter gets a drop cap
    # via CSS targeting the leading <p> inside `.arc-intro-body`.
    if bucket.intro is not None:
        intro = bucket.intro
        body_html = rewrite_html_links(
            intro.post.html,
            Page(post=intro.post, out_path=Path(),
                 site_rel=Path(f"arc-{arc.num}.html"),
                 slug=intro.slug, session_num=None),
            url_map, label_map)
        parts.append('<section class="arc-intro">')
        parts.append(f'<div class="arc-intro-body">{body_html}</div>')
        parts.append('</section>')

    # Per-arc category sub-lists
    cat_blocks = []
    for out_folder, src_folder, label in CATEGORIES:
        items = bucket.by_folder.get(src_folder, [])
        if src_folder == "Résumés":
            items = sorted(items, key=lambda p: p.session_num or 0)
        else:
            items = sorted(items, key=lambda p: p.post.title.lower())
        if not items:
            continue
        block = [f'<section class="arc-cat" id="{out_folder}">']
        block.append(f'<h2 class="cat-heading">'
                     f'<a href="{out_folder}/index.html">{html.escape(label)}</a>'
                     f'<span class="cat-rule"></span>'
                     f'<span class="cat-tally">{len(items)}</span></h2>')
        if src_folder == "Résumés":
            block.append('<ol class="card-grid card-grid-sessions">')
            for pg in items:
                href = relative_url(Path("."), pg.site_rel)
                block.append(session_card_html(pg, href))
            block.append('</ol>')
        else:
            block.append('<ul class="card-grid card-grid-entries">')
            for pg in items:
                href = relative_url(Path("."), pg.site_rel)
                block.append(entry_card_html(pg, href))
            block.append('</ul>')
        block.append('</section>')
        cat_blocks.append("\n".join(block))

        # Inject MJ-only Scénarios section right after Résumés, before PJ
        if src_folder == "Résumés":
            mj_block = mj_scenarios_section_html(arc.num)
            if mj_block:
                cat_blocks.append(mj_block)

    if cat_blocks:
        parts.append('<div class="arc-cats">')
        parts.extend(cat_blocks)
        parts.append('</div>')

    intro = bucket.intro
    og = OgMeta(
        description=(og_description(intro.post.html) if intro
                     else f"Arc {to_roman(arc.num)} — sessions {arc.s_start}–{arc.s_end}, {s_count} récits."),
        image=intro.thumbnail if intro else None,
        og_type="article",
        url=absolute_url(f"arc-{arc.num}.html"))

    return layout(Path("."), f"Arc {to_roman(arc.num)} — {arc.title}", "\n".join(parts),
                  extra_class="page-arc", buckets=buckets,
                  active_arc=arc.num, og=og)


def render_category_index(out_folder: str, src_folder: str, label: str,
                          pages: list[Page],
                          buckets: dict[int, ArcBucket]) -> str:
    # For groupable categories (PJ/PNJ/Lieux) collapse variants under their
    # main page so the index isn't cluttered with every arc-specific version.
    if src_folder in _GROUPABLE_FOLDERS:
        canonical = [p for p in pages if p.variant_group is None or p.is_main]
        meta = f'{len(canonical)} entrées'
    else:
        canonical = pages
        meta = f'{len(pages)} entrées'

    body = [f'<h1>{html.escape(label)}</h1>',
            f'<p class="meta">{meta}</p>']

    if out_folder == "resumes":
        pages_by_arc = group_resumes_by_arc(pages)
        for arc in ARCS:
            arc_pages = pages_by_arc.get(arc.num, [])
            if not arc_pages:
                continue
            body.append(f'<h2 id="arc-{arc.num}"><a href="../arc-{arc.num}.html">'
                        f'Arc {to_roman(arc.num)} — {html.escape(arc.title)}</a>'
                        f'<span class="arc-range">S{arc.s_start}–S{arc.s_end}</span></h2>')
            body.append('<ol class="card-grid card-grid-sessions">')
            for s in sorted(arc_pages, key=lambda p: p.session_num or 0):
                body.append(session_card_html(s, f"{s.slug}.html"))
            body.append('</ol>')
    else:
        body.append('<ul class="card-grid card-grid-entries">')
        for pg in sorted(canonical, key=lambda p: p.post.title.lower()):
            body.append(entry_card_html(pg, f"{pg.slug}.html"))
        body.append('</ul>')

        # MJ-only entries (Phase 3): folded into the same index, .mj-only
        mj_only = _mj_only_entities_for_category(src_folder)
        if mj_only:
            body.append('<ul class="card-grid card-grid-entries mj-only mj-only-entries">')
            for e in sorted(mj_only, key=lambda x: x.title.lower()):
                rel = f"../mj-{MJ_TOKEN}/{e.out_url}"
                body.append(
                    f'<li><a class="entry-card" href="{html.escape(rel)}">'
                    f'<span class="entry-title">{html.escape(e.title)}'
                    f'<span class="mj-badge">MJ</span></span></a></li>')
            body.append('</ul>')

    og = OgMeta(
        description=f"{label} — {meta} de la campagne Warhammer.",
        og_type="website",
        url=absolute_url(f"{out_folder}/index.html"))

    return layout(Path(out_folder), label, "\n".join(body),
                  extra_class=f"page-cat page-cat-{out_folder}",
                  buckets=buckets, og=og)


@dataclass
class NavTarget:
    href: Path
    label: str
    is_arc: bool


def _render_post_nav(prev_target: NavTarget | None,
                     next_target: NavTarget | None,
                     current_dir: Path, position: str) -> str:
    """Prev/next nav block — `position` is 'top' or 'bottom'."""
    parts = [f'<nav class="post-nav post-nav-{position}">']
    for target, side, arrow_prefix, arrow_suffix, session_label in (
        (prev_target, "prev", "← ", "", "Session précédente"),
        (next_target, "next", "", " →", "Session suivante"),
    ):
        if target is None:
            parts.append(f'<span class="{side}"></span>')
            continue
        href = relative_url(current_dir, target.href)
        klass = f"{side} nav-to-arc" if target.is_arc else side
        eyebrow = (("Arc précédent" if side == "prev" else "Arc suivant")
                   if target.is_arc else session_label)
        parts.append(
            f'<a class="{klass}" href="{href}">'
            f'<span class="nav-eyebrow">{arrow_prefix}{eyebrow}{arrow_suffix}</span>'
            f'<span class="nav-label">{html.escape(target.label)}</span>'
            f'</a>')
    parts.append('</nav>')
    return "\n".join(parts)


def render_post_page(pg: Page, pages: list[Page],
                     url_map: dict[str, Path],
                     label_map: dict[str, Path],
                     buckets: dict[int, ArcBucket],
                     pages_by_session: dict[int, dict[str, list[Page]]],
                     siblings_idx: dict[tuple[str, str], list[Page]],
                     session_by_num_map: dict[int, Page],
                     entity_popover_map: dict[str, EntityPopover]) -> str:
    # Rewrite internal links in the HTML body
    body_html = rewrite_html_links(pg.post.html, pg, url_map, label_map)
    # Wrap PJ/PNJ/Lieu names in popover triggers — sessions only, where the
    # narrative is dense enough that hover previews help the reader. Adds
    # short-form aliases (first name / 'Ar-Ulric' style title) for entities
    # tagged with this session, but only when the alias is unambiguous in
    # the session's apparition list.
    if pg.session_num is not None:
        merged_pop_map = {
            **entity_popover_map,
            **session_alias_popovers(pg.session_num, entity_popover_map,
                                     pages_by_session),
        }
        body_html = inject_entity_popovers(body_html, merged_pop_map,
                                           pg.site_rel.parent)

    parts: list[str] = []

    # Header: post title + arc badge for résumés. Strip the leading 'NN) '
    # for sessions — the session number is already shown in the meta line.
    article_cls = "post post-session" if pg.session_num is not None else "post"
    display_title = (session_short_title(pg.post.title)
                     if pg.session_num is not None else pg.post.title)
    parts.append(f'<article class="{article_cls}">')
    parts.append(f'<h1>{html.escape(display_title)}</h1>')

    arc_obj: Arc | None = None
    if pg.session_num is not None:
        arc_obj = arc_for_page(pg)
        total_sessions = len(session_by_num_map)
        if arc_obj:
            arc_href = relative_url(pg.site_rel.parent, Path(f"arc-{arc_obj.num}.html"))
            parts.append(
                f'<p class="meta">'
                f'<a href="{arc_href}">Arc {to_roman(arc_obj.num)} — '
                f'{html.escape(arc_obj.title)}</a>'
                f'<span class="meta-sep">·</span>'
                f'<span class="meta-counter">Session {pg.session_num} / {total_sessions}</span>'
                f'</p>')

    # Prev / next nav for sessions. At arc boundaries the navigation jumps
    # to the neighbouring arc's landing page instead of the next session
    # (a convention inherited from the original blog).
    def _session_nav(other: Page) -> NavTarget:
        return NavTarget(
            href=other.site_rel,
            label=f"Session {other.session_num:02d} — "
                  + session_short_title(other.post.title),
            is_arc=False)

    def _arc_nav(a: Arc) -> NavTarget:
        return NavTarget(
            href=Path(f"arc-{a.num}.html"),
            label=f"Arc {to_roman(a.num)} — {a.title}",
            is_arc=True)

    prev_target: NavTarget | None = None
    next_target: NavTarget | None = None
    if pg.session_num is not None and arc_obj is not None:
        if pg.session_num == arc_obj.s_start:
            prev_arc = next((a for a in ARCS if a.num == arc_obj.num - 1), None)
            if prev_arc is not None:
                prev_target = _arc_nav(prev_arc)
        else:
            other = session_by_num_map.get(pg.session_num - 1)
            if other is not None:
                prev_target = _session_nav(other)
        if pg.session_num == arc_obj.s_end:
            next_arc = next((a for a in ARCS if a.num == arc_obj.num + 1), None)
            if next_arc is not None:
                next_target = _arc_nav(next_arc)
        else:
            other = session_by_num_map.get(pg.session_num + 1)
            if other is not None:
                next_target = _session_nav(other)
        if prev_target or next_target:
            parts.append(_render_post_nav(prev_target, next_target,
                                          pg.site_rel.parent, "top"))

    # Body: the original HTML, preserved
    parts.append(f'<div class="post-body">{body_html}</div>')

    def appearances_html(target: Page, current_dir: Path,
                         aggregate_variants: bool,
                         extra_labels: list[str] | None = None) -> str:
        """Render an 'Apparitions' section for `target` (PNJ/PJ/Lieu/Doc/Annexe).
        If `aggregate_variants` is True, union session labels across all
        siblings (so the canonical Boris page covers all his arcs in one
        sweep). For PJ — where each variant displays its own bio + its own
        apparitions just below — set to False so each block is self-contained.
        `extra_labels` carries labels absorbed from visually-identical siblings
        merged into this entry (see visual deduplication below).
        """
        if target.session_num is not None:
            return ""
        if target.post.folder not in ENTITY_FOLDERS:
            return ""

        label_sources: list[list[str]] = [target.post.labels]
        if extra_labels:
            label_sources.append(extra_labels)
        if aggregate_variants and target.variant_group:
            for sib in siblings_for(target, siblings_idx):
                if sib.site_rel != target.site_rel:
                    label_sources.append(sib.post.labels)
        session_nums = sorted({
            int(l.strip()) for lbls in label_sources for l in lbls
            if l.strip().isdigit()
        })
        if not session_nums:
            return ""

        arc_groups: dict[int, list[int]] = {arc.num: [] for arc in ARCS}
        for snum in session_nums:
            sp = session_by_num_map.get(snum)
            if not sp:
                continue
            arc = arc_for_page(sp)
            if arc:
                arc_groups[arc.num].append(snum)
        visible = [(num, snums) for num, snums in arc_groups.items() if snums]
        if not visible:
            return ""

        total = sum(len(s) for _, s in visible)
        out = [
            '<details class="appearances">',
            f'<summary class="appearances-summary">Apparitions'
            f'<span class="appearances-count">{total} session{"s" if total > 1 else ""}</span>'
            f'</summary>',
            '<div class="appearances-content">',
        ]
        for arc_num, snums in visible:
            arc = next(a for a in ARCS if a.num == arc_num)
            arc_href = relative_url(current_dir, Path(f"arc-{arc.num}.html"))
            out.append('<div class="appearances-arc">')
            out.append(
                f'<h3 class="appearances-arc-title">'
                f'<a href="{html.escape(arc_href)}">'
                f'{html.escape(arc.title)}</a></h3>')
            out.append('<ol class="appearances-sessions">')
            for snum in snums:
                sp = session_by_num_map[snum]
                out.append(session_link_html(
                    sp, relative_url(current_dir, sp.site_rel)))
            out.append('</ol></div>')
        out.append('</div></details>')
        return "\n".join(out)

    # Visual deduplication across the variant group: variants that share the
    # same title + subtitle + portrait represent the same narrative state
    # (e.g. Boris Todbringer reposted three times with identical visuals) and
    # collapse into a single inline bio. The absorbed siblings contribute
    # their session labels to the kept page's apparitions list.
    def _visual_key(p: Page) -> tuple[str, str, str]:
        return ((p.post.title or "").strip(),
                (p.subtitle or "").strip(),
                (p.thumbnail or "").strip())

    absorbed_labels: dict[Path, list[str]] = {pg.site_rel: []}
    siblings_to_render: list[Page] = []
    if pg.variant_group and pg.post.folder in _GROUPABLE_FOLDERS:
        all_in_group = siblings_for(pg, siblings_idx)
        others = [s for s in all_in_group if s.site_rel != pg.site_rel]
        key_owner: dict[tuple[str, str, str], Path] = {_visual_key(pg): pg.site_rel}
        for s in others:
            key = _visual_key(s)
            owner = key_owner.get(key)
            if owner is not None:
                absorbed_labels.setdefault(owner, []).extend(s.post.labels)
            else:
                key_owner[key] = s.site_rel
                absorbed_labels.setdefault(s.site_rel, [])
                siblings_to_render.append(s)

    # Main page apparitions: this page's own sessions, plus any labels merged
    # in from visually-identical siblings that were absorbed.
    main_app = appearances_html(pg, pg.site_rel.parent,
                                aggregate_variants=False,
                                extra_labels=absorbed_labels.get(pg.site_rel))
    if main_app:
        parts.append(main_app)

    # Render each visually-distinct variant as a full inline bio after the
    # apparitions section, for every groupable folder (PJ / PNJ / Lieux).
    # Lets a character/place show its full evolution on a single canonical
    # page.
    if siblings_to_render:
        pg_title = (pg.post.title or "").strip()
        parts.append('<div class="variants-bios">')
        for it in siblings_to_render:
            body_html_v = rewrite_html_links(it.post.html, it,
                                             url_map, label_map)
            # Heading text: when the variant shares the page's title (typical
            # PNJ case where all states have the same character name and only
            # the role/portrait differ), promote the subtitle to the heading
            # to avoid showing the same name twice. Strip the bold-subtitle
            # paragraph from the body in that case so it isn't duplicated.
            it_title = (it.post.title or "").strip()
            use_subtitle = (it_title == pg_title and bool(it.subtitle))
            heading_text = it.subtitle if use_subtitle else it.post.title
            if use_subtitle:
                body_html_v = strip_subtitle_paragraph(body_html_v)
            # Self-anchor: clicking the variant title updates the URL bar
            # without navigating elsewhere (useful for sharing).
            role = ('<span class="variant-flag">Version principale</span>'
                    if it.is_main else '')
            parts.append(f'<article class="variant-bio" id="variant-{html.escape(it.slug)}">')
            parts.append(f'<h3 class="variant-bio-title">'
                         f'<a href="#variant-{html.escape(it.slug)}">'
                         f'{html.escape(heading_text)}</a>'
                         f'{role}</h3>')
            parts.append(f'<div class="variant-bio-body">{body_html_v}</div>')
            variant_app = appearances_html(it, pg.site_rel.parent,
                                            aggregate_variants=False,
                                            extra_labels=absorbed_labels.get(it.site_rel))
            if variant_app:
                parts.append(variant_app)
            parts.append('</article>')
        parts.append('</div>')

    # For session pages: list every PJ/PNJ/Lieu/Document/Annexe tagged with
    # this session number, grouped by category — gives a quick lookup of
    # "everyone and everything that appeared in this session".
    if pg.session_num is not None:
        related = pages_by_session.get(pg.session_num, {})
        rel_blocks: list[str] = []
        for out_folder, src_folder, lbl in CATEGORIES:
            if src_folder == "Résumés":
                continue
            items = related.get(src_folder, [])
            if not items:
                continue
            rel_blocks.append('<section class="rel-cat">')
            rel_blocks.append(f'<h3 class="rel-cat-heading">{html.escape(lbl)}'
                              f'<span class="count">{len(items)}</span></h3>')
            rel_blocks.append('<ul class="card-grid card-grid-entries card-grid-compact">')
            for it in sorted(items, key=lambda p: p.post.title.lower()):
                href = link_for(it, pg.site_rel.parent)
                rel_blocks.append(entry_card_html(it, href))
            rel_blocks.append('</ul></section>')
        if rel_blocks:
            parts.append(f'<div class="divider">{FLEURON_SVG}</div>')
            parts.append('<section class="session-related">')
            parts.append('<h2 class="section-title">Dans cette session</h2>')
            parts.extend(rel_blocks)
            parts.append('</section>')


    # Prev/next bottom nav (same targets as the top one)
    if pg.session_num is not None and (prev_target or next_target):
        parts.append(_render_post_nav(prev_target, next_target,
                                      pg.site_rel.parent, "bottom"))

    parts.append('</article>')

    # Phase 3: MJ enrichment section (CSS-hidden until mj-mode toggled)
    mj_enrich = _mj_enrichment_html_for_page(pg)
    if mj_enrich:
        parts.append(mj_enrich)

    og = OgMeta(
        description=og_description(pg.post.html),
        image=pg.thumbnail,
        og_type="article",
        url=absolute_url(pg.site_rel))
    return layout(pg.site_rel.parent, display_title, "\n".join(parts),
                  extra_class="page-post", buckets=buckets,
                  active_arc=arc_obj.num if arc_obj else None, og=og)


def arc_for_page(pg: Page) -> Arc | None:
    """A post belongs to an arc if its <published> month matches one of the
    arc's listed pub_months (the blog's date-based convention)."""
    if not pg.post.published:
        return None
    month = pg.post.published[:7]
    for arc in ARCS:
        if month in arc.pub_months:
            return arc
    return None


def arc_for_session(num: int) -> Arc | None:
    """Backwards-compatible session-number lookup using the ranges computed
    by compute_arc_session_ranges()."""
    for arc in ARCS:
        if arc.s_start and arc.s_start <= num <= arc.s_end:
            return arc
    return None


def compute_arc_session_ranges(pages: list[Page]) -> None:
    """Fill Arc.s_start / Arc.s_end from the actual data."""
    for arc in ARCS:
        snums = sorted(
            pg.session_num for pg in pages
            if pg.session_num is not None and arc_for_page(pg) is arc
        )
        if snums:
            arc.s_start = snums[0]
            arc.s_end = snums[-1]


def group_resumes_by_arc(pages: list[Page]) -> dict[int, list[Page]]:
    out: dict[int, list[Page]] = {}
    for pg in pages:
        if pg.session_num is None:
            continue
        arc = arc_for_page(pg)
        if arc is not None:
            out.setdefault(arc.num, []).append(pg)
    return out


# Per-arc bucketing. Résumés go by session number; non-résumé pages
# (PJ/PNJ/Lieux/Documents) are bucketed via the `/search/label/X` links found
# inside each session's HTML — those are the actual semantic cross-references
# between a session and the characters / places / documents that appear in it.
@dataclass
class ArcBucket:
    arc: Arc
    intro: Page | None                       # the arc's intro post, if found
    by_folder: dict[str, list[Page]]         # src folder → pages in this arc


def find_intro_pages(pages: list[Page]) -> dict[int, Page]:
    """Find the intro post for each arc by matching its title."""
    by_title: dict[str, Page] = {}
    for pg in pages:
        t = pg.post.title.strip()
        by_title.setdefault(t, pg)
        by_title.setdefault(t.replace("’", "'"), pg)
    out: dict[int, Page] = {}
    for arc in ARCS:
        key = arc.intro_title.strip()
        match = by_title.get(key) or by_title.get(key.replace("’", "'"))
        if match is not None:
            out[arc.num] = match
    return out


def bucket_by_arc(pages: list[Page],
                  intros: dict[int, Page]) -> dict[int, ArcBucket]:
    """Group pages by arc.

    Résumés: by their published month (via `arc_for_page`).
    Non-résumés (PJ/PNJ/Lieu/Doc/Univers): by the session numbers each page
    carries in its Blogger labels — each session belongs to exactly one arc.
    Variants are collapsed to their group's main so an arc lists 'Elvira'
    once, not all her per-arc variants.
    """
    buckets: dict[int, ArcBucket] = {
        arc.num: ArcBucket(arc=arc, intro=intros.get(arc.num), by_folder={})
        for arc in ARCS
    }
    intro_ids = {id(p) for p in intros.values()}

    # 1. Bucket résumés directly (by their published month).
    for pg in pages:
        if pg.session_num is None:
            continue
        arc = arc_for_page(pg)
        if arc is not None:
            buckets[arc.num].by_folder.setdefault(pg.post.folder, []).append(pg)

    # 2. Non-résumé pages: each is bucketed into every arc whose sessions
    #    appear in its numeric Blogger labels (`'05'`, `'12'`, …). Variants
    #    are collapsed to their group's main (the per-session "Dans cette
    #    session" block keeps showing the specific variant).
    main_for_group: dict[tuple[str, str], Page] = {}
    for pg in pages:
        if pg.variant_group and pg.is_main:
            main_for_group[(pg.post.folder, pg.variant_group)] = pg

    # Each arc tracks (folder, variant_group_or_site_rel) it's already seen,
    # so the same character appears at most once per arc.
    seen_per_arc: dict[int, set] = {arc.num: set() for arc in ARCS}
    for pg in pages:
        if pg.session_num is not None:
            continue
        if id(pg) in intro_ids:
            continue
        if pg.post.folder not in ENTITY_FOLDERS:
            continue

        # Use THIS page's own labels to know which arcs it touches
        # (a variant carries the labels of its specific sessions).
        session_nums: set[int] = set()
        for lbl in pg.post.labels:
            s = lbl.strip()
            if s.isdigit():
                session_nums.add(int(s))
        if not session_nums:
            continue

        # …but add the main of its variant group (not the variant itself)
        # to each arc bucket — the per-session "Dans cette session" block
        # keeps showing the specific variant.
        if pg.variant_group:
            rep = main_for_group.get((pg.post.folder, pg.variant_group), pg)
            dedupe_key = (pg.post.folder, pg.variant_group)
        else:
            rep = pg
            dedupe_key = pg.site_rel

        for snum in session_nums:
            arc = arc_for_session(snum)
            if arc is None:
                continue
            if dedupe_key in seen_per_arc[arc.num]:
                continue
            seen_per_arc[arc.num].add(dedupe_key)
            buckets[arc.num].by_folder.setdefault(rep.post.folder, []).append(rep)

    return buckets


# --------------------------------------------------------------------------- #
# CSS
# --------------------------------------------------------------------------- #


CSS = """\
/* ==========================================================================
   Mon Ennemi Intérieur — old-grimoire / illuminated-manuscript theme
   ========================================================================== */

:root {
  /* Three tonal layers from darkest to lightest paper, so panels read
     clearly against each other:  vellum (sidebar) < parchment (body) < paper (cards) */
  --vellum:      #e1d4b3;
  --parchment:   #efe5cf;
  --paper:       #faf3e0;
  --paper-hi:    #fefaef;
  --ink:         #1f160d;
  --ink-soft:    #4a3a26;
  --muted:       #7a6b54;
  --oxblood:     #7a1f1f;
  --oxblood-hi:  #a52a2a;
  --gold:        #a37a2e;
  --gold-hi:     #c69a3e;
  --rule:        #c5b491;
  --rule-soft:   #d8c8a4;
  --shadow:      0 1px 0 rgba(0,0,0,0.04), 0 4px 14px -8px rgba(38, 28, 18, 0.18);
  --shadow-hi:   0 2px 4px rgba(0,0,0,0.06), 0 10px 28px -10px rgba(122, 31, 31, 0.28);

  /* Legacy aliases for code still referring to the old names */
  --parchment-2: var(--vellum);
  --card:        var(--paper);

  --serif-display:   "IM Fell English SC", "Cormorant SC", Georgia, serif;
  --serif-display-2: "IM Fell English", "EB Garamond", Georgia, serif;
  --serif-body:      "EB Garamond", "IM Fell English", Georgia, serif;
  --mono:            ui-monospace, "SFMono-Regular", "Menlo", monospace;

  --max:        820px;
  --sidebar-w:  280px;
  --header-h:   62px;
}
@media (prefers-color-scheme: dark) {
  :root {
    --vellum:      #14100a;
    --parchment:   #1b1610;
    --paper:       #24180f;
    --paper-hi:    #2c1e13;
    --ink:         #ece0bd;
    --ink-soft:    #c4b58f;
    --muted:       #948361;
    --oxblood:     #d18a8a;
    --oxblood-hi:  #ecb1b1;
    --gold:        #d6a648;
    --gold-hi:     #f0c46c;
    --rule:        #3d3122;
    --rule-soft:   #2a2218;
    --shadow:      0 1px 0 rgba(0,0,0,0.3), 0 4px 14px -8px rgba(0,0,0,0.5);
    --shadow-hi:   0 2px 4px rgba(0,0,0,0.4), 0 10px 28px -10px rgba(209,138,138,0.3);
  }
}

* { box-sizing: border-box; }
html { -webkit-text-size-adjust: 100%;
        scroll-padding-top: calc(var(--header-h) + 1rem); }
.variant-bio:target {
  animation: variant-highlight 1.6s ease-out;
}
@keyframes variant-highlight {
  0%   { background: rgba(163,122,46,0.18); }
  100% { background: transparent; }
}

body {
  margin: 0;
  padding: 0;
  color: var(--ink);
  font: 18.5px/1.78 var(--serif-body);
  font-variant-ligatures: common-ligatures historical-ligatures discretionary-ligatures;
  font-variant-numeric: oldstyle-nums proportional-nums;
  font-feature-settings: "liga" 1, "dlig" 1, "onum" 1, "kern" 1;
  background-color: var(--parchment);
  /* Subtle paper grain (an SVG noise data URI) layered over the parchment. */
  background-image:
    radial-gradient(ellipse at top, rgba(255, 240, 200, 0.22), transparent 60%),
    radial-gradient(ellipse at bottom right, rgba(122, 31, 31, 0.06), transparent 70%),
    url("data:image/svg+xml;utf8,<svg xmlns='http://www.w3.org/2000/svg' width='220' height='220'><filter id='n'><feTurbulence type='fractalNoise' baseFrequency='0.85' numOctaves='2' stitchTiles='stitch'/><feColorMatrix values='0 0 0 0 0.16 0 0 0 0 0.12 0 0 0 0 0.07 0 0 0 0.16 0'/></filter><rect width='100%25' height='100%25' filter='url(%23n)'/></svg>");
  background-attachment: fixed;
}

a { color: var(--oxblood); text-decoration: none;
    text-decoration-color: rgba(122, 31, 31, 0.35);
    text-decoration-thickness: 0.06em;
    text-underline-offset: 0.18em; }
a:hover { color: var(--oxblood-hi); text-decoration: underline;
          text-decoration-color: currentColor; }

img { max-width: 100%; height: auto; }
hr { border: 0; height: 1px; background: var(--rule); margin: 2rem 0; }
blockquote { border-left: 2px solid var(--gold);
             margin: 1rem 0; padding: 0.2rem 0 0.2rem 1rem;
             color: var(--ink-soft); font-style: italic; }
code { font-family: var(--mono); font-size: 0.92em;
       background: var(--card); padding: 0 0.25em; border-radius: 2px; }
em { color: var(--ink-soft); }

/* ---------- Header / wordmark / nav ----------------------------------- */

.site-header {
  position: sticky; top: 0; z-index: 10;
  display: flex; flex-wrap: wrap; align-items: center;
  justify-content: space-between; gap: 0.75rem 1.5rem;
  padding: 0.55rem 1.5rem;
  background:
    linear-gradient(180deg, rgba(247,239,218,0.92), rgba(247,239,218,0.82)),
    var(--parchment);
  -webkit-backdrop-filter: blur(6px); backdrop-filter: blur(6px);
  border-bottom: 1px solid var(--rule);
  box-shadow: 0 1px 0 rgba(0,0,0,0.04);
}
@media (prefers-color-scheme: dark) {
  .site-header {
    background:
      linear-gradient(180deg, rgba(36,24,15,0.92), rgba(36,24,15,0.78)),
      var(--parchment);
  }
}
.site-title { display: inline-flex; align-items: baseline; gap: 0.5rem; }
.site-title:hover { text-decoration: none; }
.wordmark {
  font-family: var(--serif-display);
  font-size: 1.05rem; letter-spacing: 0.14em;
  color: var(--ink);
}
.wordmark::after {
  content: "✦";
  margin-left: 0.5em; color: var(--gold);
  font-size: 0.7em; vertical-align: 0.15em;
}
.site-nav {
  font-family: var(--serif-display);
  font-size: 0.82rem; letter-spacing: 0.1em;
  color: var(--muted);
}
.site-nav a {
  color: var(--ink-soft); margin: 0 0.4em;
  border-bottom: 1px solid transparent; padding-bottom: 1px;
}
.site-nav a:hover { color: var(--oxblood); border-color: var(--gold);
                    text-decoration: none; }
.menu-toggle {
  display: none; cursor: pointer;
  background: transparent; border: 1px solid var(--rule);
  color: var(--ink); padding: 0.25rem 0.6rem;
  font-family: var(--serif-display); font-size: 1.1rem;
  border-radius: 0;
}

/* ---------- Search ----------------------------------------------------- */

.search { position: relative; flex: 1; min-width: 180px;
          max-width: 360px; margin: 0 auto; }
.search-input {
  -webkit-appearance: none; appearance: none;
  width: 100%;
  border: 0; border-radius: 0;
  border-bottom: 1px solid var(--rule);
  background: transparent;
  padding: 0.5rem 0.2rem 0.4rem 1.6rem;
  font: italic 1rem/1.3 var(--serif-body);
  color: var(--ink);
  outline: none;
  background-image:
    url("data:image/svg+xml;utf8,<svg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 16 16' fill='none' stroke='%23a37a2e' stroke-width='1.4' stroke-linecap='round'><circle cx='6.8' cy='6.8' r='4.6'/><line x1='10.2' y1='10.2' x2='14' y2='14'/></svg>");
  background-repeat: no-repeat;
  background-position: 0.1rem 50%;
  background-size: 16px 16px;
  transition: border-color 200ms ease;
}
.search-input::-webkit-search-decoration,
.search-input::-webkit-search-cancel-button,
.search-input::-webkit-search-results-button,
.search-input::-webkit-search-results-decoration { -webkit-appearance: none; display: none; }
.search-input:focus { border-bottom-color: var(--gold); border-bottom-width: 2px;
                       padding-bottom: calc(0.4rem - 1px); }
.search-input::placeholder { color: var(--muted); font-style: italic;
                              letter-spacing: 0.02em; }

.search-results {
  position: absolute; top: 100%; left: 0; right: 0;
  margin-top: 0.4rem;
  background: var(--card);
  border: 1px solid var(--rule);
  box-shadow: 0 8px 30px -10px rgba(0,0,0,0.18);
  max-height: 70vh; overflow-y: auto;
  z-index: 20;
  display: none;
}
.search-results.is-open { display: block; }
.search-result {
  display: grid; grid-template-columns: 44px 1fr;
  gap: 0.7rem; align-items: center;
  padding: 0.45rem 0.7rem;
  border-bottom: 1px solid var(--rule-soft);
  color: var(--ink); text-decoration: none;
}
.search-result:last-child { border-bottom: 0; }
.search-result:hover, .search-result:focus {
  background: rgba(163,122,46,0.08);
  color: var(--ink); text-decoration: none;
}
.search-thumb {
  width: 44px; height: 44px; object-fit: cover;
  border: 1px solid var(--rule-soft);
  filter: saturate(0.9);
}
.search-thumb-fallback {
  display: inline-flex; align-items: center; justify-content: center;
  background: linear-gradient(135deg, rgba(163,122,46,0.10), rgba(122,31,31,0.07));
  color: var(--gold);
  font-family: var(--serif-display);
  font-size: 1rem;
}
.search-meta { display: flex; flex-direction: column; gap: 0.1rem; min-width: 0; }
.search-title {
  font-family: var(--serif-body);
  font-size: 0.95rem; line-height: 1.25;
  overflow: hidden; text-overflow: ellipsis; white-space: nowrap;
}
.search-cat {
  font-family: var(--serif-display);
  font-size: 0.7rem; letter-spacing: 0.16em; text-transform: uppercase;
  color: var(--gold);
}
.search-empty {
  padding: 0.8rem 1rem; color: var(--muted);
  font-style: italic; font-family: var(--serif-body);
}

/* Full search results page (/search/?q=…) */
.search-page-heading {
  font-family: var(--serif-display);
  font-size: 0.8rem; letter-spacing: 0.28em;
  text-transform: uppercase;
  color: var(--gold);
  margin: 0 0 0.4rem;
}
.search-page-query {
  font-family: var(--serif-display-2);
  font-size: 1.7rem; line-height: 1.2;
  color: var(--ink);
  margin: 0 0 2rem;
}
.search-page-query em { color: var(--oxblood); font-style: italic; }
.search-group { margin-top: 2.4rem; }
.search-group-title {
  font-family: var(--serif-display);
  font-size: 1.05rem; letter-spacing: 0.16em;
  text-transform: uppercase;
  display: flex; align-items: baseline; gap: 0.7rem;
  margin: 0 0 0.8rem;
  padding-bottom: 0.4rem;
  border-bottom: 1px solid var(--rule);
}
.search-group-title .count {
  font-family: var(--mono); font-size: 0.78rem;
  letter-spacing: 0.04em; text-transform: none;
}

/* ---------- Image lightbox -------------------------------------------- */

body.lightbox-open { overflow: hidden; }
.lightbox-overlay {
  position: fixed; inset: 0; z-index: 1000;
  background: rgba(15, 10, 5, 0.92);
  -webkit-backdrop-filter: blur(6px); backdrop-filter: blur(6px);
  display: flex; align-items: center; justify-content: center;
  padding: 3rem 1.5rem;
  cursor: zoom-out;
  animation: lightbox-fade 160ms ease-out;
}
@keyframes lightbox-fade { from { opacity: 0; } to { opacity: 1; } }
.lightbox-img {
  max-width: 100%; max-height: 100%;
  width: auto; height: auto;
  object-fit: contain;
  cursor: default;
  box-shadow: 0 30px 80px -20px rgba(0,0,0,0.7);
  border: 1px solid rgba(163,122,46,0.4);
  animation: lightbox-zoom 220ms cubic-bezier(.16,1,.3,1);
}
@keyframes lightbox-zoom {
  from { opacity: 0; transform: scale(0.96); }
  to   { opacity: 1; transform: scale(1); }
}
.lightbox-close {
  position: absolute; top: 1.2rem; right: 1.2rem;
  width: 42px; height: 42px;
  background: transparent;
  color: #ece0bd;
  border: 1px solid rgba(236, 224, 189, 0.5);
  border-radius: 50%;
  font-size: 1.6rem; font-family: var(--serif-body);
  display: flex; align-items: center; justify-content: center;
  cursor: pointer; padding: 0;
  transition: background 160ms ease, border-color 160ms ease;
}
.lightbox-close:hover {
  background: rgba(236, 224, 189, 0.1);
  border-color: var(--gold);
  color: var(--gold-hi);
}

/* ---------- Two-column layout ----------------------------------------- */

.layout {
  display: grid;
  grid-template-columns: var(--sidebar-w) minmax(0, 1fr);
  max-width: calc(var(--max) + var(--sidebar-w) + 6rem);
  margin: 0 auto;
  padding: 0;
}
main {
  padding: 1.4rem 2.5rem 5rem;
  max-width: var(--max);
  min-width: 0;
}
@media (max-width: 1100px) {
  main { padding: 1.2rem 2rem 4rem; }
}

/* ---------- Sidebar (chapter index) ----------------------------------- */

.sidebar {
  position: sticky; top: var(--header-h);
  align-self: start;
  max-height: calc(100vh - var(--header-h));
  overflow-y: auto;
  padding: 2.4rem 1.6rem 3rem 1.8rem;
  font-size: 0.95rem;
  border-right: 1px solid var(--rule);
  background: var(--vellum);
  box-shadow: inset -8px 0 16px -16px rgba(38,28,18,0.18);
}
.sidebar::-webkit-scrollbar { width: 6px; }
.sidebar::-webkit-scrollbar-thumb { background: var(--rule); border-radius: 3px; }

.sidebar-eyebrow {
  font-family: var(--serif-display);
  font-size: 0.68rem; letter-spacing: 0.25em; text-transform: uppercase;
  color: var(--gold); margin-bottom: 0.1rem;
}
.sidebar-title {
  font-family: var(--serif-display);
  font-size: 1.1rem; letter-spacing: 0.05em;
  color: var(--ink); margin: 0 0 1.3rem;
  border-bottom: 1px double var(--rule); padding-bottom: 0.6rem;
}
.sidebar-arcs { list-style: none; padding: 0; margin: 0; }
.sidebar-arcs > li { margin: 0.15rem 0; padding: 0; position: relative; }

.sidebar-arc {
  display: grid;
  grid-template-columns: 2.1rem 1fr;
  align-items: baseline;
  gap: 0.55rem;
  padding: 0.45rem 0.4rem 0.45rem 0.2rem;
  color: var(--ink); line-height: 1.3;
  border-left: 2px solid transparent;
}
.sidebar-arc:hover { background: rgba(163,122,46,0.06);
                     color: var(--ink); text-decoration: none;
                     border-left-color: var(--gold); }
.sidebar-num {
  font-family: var(--serif-display);
  font-size: 0.95rem; color: var(--gold);
  letter-spacing: 0.06em; text-align: right;
}
.sidebar-name {
  font-family: var(--serif-body);
  font-size: 0.97rem;
  font-feature-settings: "smcp" 0;
}
.sidebar-arcs > li.is-active > .sidebar-arc {
  background: linear-gradient(90deg, rgba(122,31,31,0.10), transparent);
  border-left-color: var(--oxblood);
}
.sidebar-arcs > li.is-active .sidebar-num { color: var(--oxblood); }
.sidebar-arcs > li.is-active .sidebar-name { color: var(--oxblood); font-style: italic; }

.sidebar-sub {
  display: flex; flex-direction: column;
  margin: 0.1rem 0 0.4rem 2.6rem;
  font-family: var(--serif-display);
  font-size: 0.78rem; letter-spacing: 0.05em;
  border-left: 1px solid var(--rule-soft); padding-left: 0.6rem;
}
.sidebar-sub a {
  display: flex; justify-content: space-between;
  padding: 0.18rem 0.25rem; color: var(--muted);
}
.sidebar-sub a:hover { color: var(--oxblood); text-decoration: none; }
.sidebar-sub .count {
  color: var(--muted); font-family: var(--mono);
  font-size: 0.85em;
}

/* ---------- Scenario sidebar (per-scenario page rail) ----------------- */
.sidebar-scenario .scn-nav { display: flex; flex-direction: column; }
.sidebar-scenario .scn-group { margin: 0.55rem 0 0.2rem; }
.sidebar-scenario .scn-group-h {
  display: block;
  font-family: var(--serif-display);
  font-size: 0.72rem; letter-spacing: 0.08em; text-transform: uppercase;
  color: var(--muted);
  margin: 0.2rem 0 0.15rem;
  border-bottom: 1px solid var(--rule);
  padding-bottom: 0.12rem;
}
.sidebar-scenario .scn-link {
  display: block;
  font-family: var(--serif-display);
  font-size: 0.92rem; line-height: 1.25;
  color: var(--ink);
  text-decoration: none;
  padding: 0.18rem 0.4rem 0.18rem 0.6rem;
  border-left: 2px solid transparent;
  border-radius: 2px;
}
.sidebar-scenario .scn-link:hover {
  background: rgba(163,122,46,0.06);
  border-left-color: var(--gold);
  text-decoration: none;
}
.sidebar-scenario .scn-link.is-active {
  background: linear-gradient(90deg, rgba(122,31,31,0.10), transparent);
  border-left-color: var(--oxblood);
  color: var(--oxblood); font-style: italic;
}

/* ---------- Footer ----------------------------------------------------- */

.site-footer {
  border-top: 1px solid var(--rule);
  margin-top: 4rem;
  padding: 2rem 1.5rem 3rem;
  text-align: center;
  color: var(--muted); font-size: 0.92rem;
  font-style: italic;
}
.footer-flourish { margin-bottom: 1rem; }

/* ---------- Decorative fleuron + corners ------------------------------ */

.fleuron {
  display: block; margin: 0 auto;
  width: 260px; height: 36px;
  overflow: visible;
}
.fleuron .vine { fill: none; stroke: var(--gold); stroke-width: 1.1;
                  stroke-linecap: round; }
.fleuron .berry { fill: var(--gold); }
.fleuron .rosette > path { fill: none; stroke: var(--gold); stroke-width: 1.1; }
.fleuron .rosette-core { fill: var(--gold); }
.divider { margin: 3rem 0; }

.corner {
  position: absolute; width: 18px; height: 18px;
  pointer-events: none;
}
.corner path { fill: none; stroke: var(--gold);
                stroke-width: 1.1; stroke-linecap: round; }
.corner-tl { top: 6px; left: 6px; }
.corner-br { bottom: 6px; right: 6px; }

/* ---------- Headings --------------------------------------------------- */

h1, h2, h3, h4 { font-family: var(--serif-display); font-weight: normal;
                  color: var(--ink); letter-spacing: 0.02em; }
h1 { font-size: 2.1rem; margin: 0.5rem 0 0.5rem; line-height: 1.15; }
h2 { font-size: 1.35rem; margin: 2.5rem 0 1rem;
     padding-bottom: 0.4rem;
     border-bottom: 1px solid var(--rule); }
h3 { font-size: 1.15rem; margin: 1.6rem 0 0.6rem; }
.lead, .meta, .section-lead { font-family: var(--serif-body); }
.meta { color: var(--muted); font-style: italic; margin: 0 0 1.4rem; }
.meta-sep { display: inline-block; margin: 0 0.55em; color: var(--gold);
            font-style: normal; opacity: 0.7; }
.meta-counter { font-family: var(--serif-display);
                font-style: normal; font-size: 0.78em;
                letter-spacing: 0.14em; text-transform: uppercase;
                color: var(--muted); }
.lead { color: var(--ink-soft); font-style: italic; font-size: 1.05rem; }
.count { color: var(--muted); font-family: var(--mono); font-size: 0.82em; }
.arc-range { color: var(--muted); font-family: var(--mono);
              font-size: 0.78rem; letter-spacing: 0.04em;
              white-space: nowrap; margin-left: 0.9em;
              text-transform: none; }

/* ---------- HOME PAGE -------------------------------------------------- */

/* On the home page the sidebar would just duplicate the arc-cards grid, so
   hide it and let main span the full layout. */
.page-home .sidebar { display: none; }
.page-home .layout { grid-template-columns: 1fr; max-width: 880px; }
.page-home main { padding-left: 1.5rem; padding-right: 1.5rem; max-width: 880px; }

.hero { text-align: center; padding: 2rem 0 1rem; }
.hero-eyebrow {
  font-family: var(--serif-display);
  font-size: 0.78rem; letter-spacing: 0.32em;
  text-transform: uppercase;
  color: var(--gold);
  margin: 0 0 0.6rem;
}
.hero-title {
  font-family: var(--serif-display);
  font-size: clamp(2.4rem, 5vw, 3.4rem);
  line-height: 1.05; margin: 0 auto 1rem;
  max-width: 14ch;
  color: var(--ink);
  letter-spacing: 0.02em;
}
.hero-sub {
  font-family: var(--serif-body);
  font-size: 1.15rem; line-height: 1.55;
  max-width: 38em; margin: 0 auto;
  color: var(--ink-soft);
}
.hero-flourish { margin-top: 2.2rem; }

/* Preface block on the home page — the campaign foreword. */
.preface { max-width: 38em; margin: 0 auto 2.5rem;
           font-size: 1.05rem; line-height: 1.75; color: var(--ink-soft); }
.preface-body > h1 { display: none; }  /* hide blogger's inner Préface heading */
.preface-body p { margin: 0.9rem 0; text-align: justify; }
.preface-body a {
  display: inline-block; margin: 0.15rem 0;
  font-family: var(--serif-display);
  font-size: 0.82rem; letter-spacing: 0.14em;
  text-transform: uppercase;
  color: var(--oxblood);
  border-bottom: 1px solid rgba(122,31,31,0.25);
}
.preface-body a:hover { color: var(--oxblood-hi);
                          border-bottom-color: currentColor;
                          text-decoration: none; }
.preface-body em { color: var(--muted); font-size: 0.92em; }

.section-title {
  font-family: var(--serif-display);
  font-size: 1.05rem; letter-spacing: 0.22em;
  text-transform: uppercase;
  text-align: center;
  color: var(--ink);
  border: 0; padding: 0;
  margin: 2.5rem 0 0.5rem;
}
.section-lead { text-align: center; color: var(--muted);
                margin: 0 0 1.5rem; font-style: italic; }

.arc-cards {
  list-style: none; padding: 0;
  margin: 1.5rem 0 0;
  display: grid; grid-template-columns: 1fr; gap: 0.8rem;
}
.arc-card {
  position: relative;
  display: grid; grid-template-columns: 4rem 1fr;
  align-items: center; gap: 1.2rem;
  padding: 1.4rem 1.8rem;
  background: var(--paper);
  border: 1px solid var(--rule-soft);
  color: var(--ink);
  box-shadow: var(--shadow);
  transition: background 180ms ease, border-color 180ms ease,
              transform 180ms ease, box-shadow 220ms ease;
}
.arc-card:hover {
  text-decoration: none;
  background: var(--paper-hi);
  border-color: var(--gold);
  transform: translateY(-2px);
  box-shadow: var(--shadow-hi);
}
.arc-card:hover .arc-roman { color: var(--oxblood); }
.arc-card:hover .corner path { stroke: var(--oxblood); }
.arc-roman {
  font-family: var(--serif-display);
  font-size: 2.4rem; color: var(--gold);
  text-align: center; line-height: 1;
  letter-spacing: 0.05em;
}
.arc-card-eyebrow {
  font-family: var(--serif-display);
  font-size: 0.7rem; letter-spacing: 0.22em;
  text-transform: uppercase; color: var(--muted);
  margin-bottom: 0.2rem;
}
.arc-card-title {
  font-family: var(--serif-display-2);
  font-size: 1.3rem; line-height: 1.2;
  color: var(--ink); margin: 0;
}
.arc-card-meta {
  font-family: var(--serif-body);
  font-size: 0.9rem; color: var(--muted); margin-top: 0.25rem;
  font-style: italic;
}

.cat-cards {
  list-style: none; padding: 0; margin: 1.5rem 0;
  display: grid; gap: 0.5rem;
  grid-template-columns: repeat(auto-fit, minmax(140px, 1fr));
}
.cat-card {
  display: flex; flex-direction: column;
  align-items: center; gap: 0.3rem;
  padding: 1.1rem 0.5rem 0.9rem;
  background: var(--paper);
  border: 1px solid var(--rule-soft);
  color: var(--ink);
  box-shadow: var(--shadow);
  transition: all 180ms ease;
}
.cat-card:hover { background: var(--paper-hi); border-color: var(--gold);
                   transform: translateY(-2px); box-shadow: var(--shadow-hi);
                   text-decoration: none; }
.cat-name { font-family: var(--serif-display);
            font-size: 0.95rem; letter-spacing: 0.12em; }
.cat-count { font-family: var(--mono); color: var(--muted);
             font-size: 0.85em; }

/* ---------- ARC PAGE --------------------------------------------------- */

.arc-header { text-align: center; padding: 1rem 0 0.5rem; }
.arc-roman-large {
  font-family: var(--serif-display);
  font-size: clamp(3rem, 7vw, 4.2rem);
  color: var(--gold);
  line-height: 1; letter-spacing: 0.07em;
  text-shadow: 0 1px 0 rgba(0,0,0,0.04);
}
.arc-eyebrow {
  font-family: var(--serif-display);
  font-size: 0.78rem; letter-spacing: 0.3em;
  text-transform: uppercase; color: var(--muted);
  margin: 0.6rem 0 0.3rem;
}
.arc-title {
  font-family: var(--serif-display-2);
  font-size: clamp(1.7rem, 3.5vw, 2.3rem);
  line-height: 1.15;
  margin: 0 auto 0.4rem;
  max-width: 20ch;
}
.arc-meta {
  font-family: var(--serif-body); font-style: italic;
  color: var(--muted); margin: 0;
}

.arc-intro { margin: 0 0 2rem; padding: 1rem 0 0; }
.arc-intro-body { font-size: 1.05rem; line-height: 1.7;
                   color: var(--ink); }
.arc-intro-body > h1 { display: none; }  /* hide blogger's inner H1 */
.arc-intro-body p { margin: 0.8rem 0; }
.arc-intro-body p:first-of-type::first-letter {
  font-family: var(--serif-display);
  font-size: 3.6rem; line-height: 0.85;
  float: left; padding: 0.2rem 0.4rem 0 0;
  color: var(--oxblood);
}
.arc-intro-body img { display: block; margin: 1rem auto;
                       max-height: 340px; border: 1px solid var(--rule); }

.arc-cats { display: grid; grid-template-columns: 1fr; gap: 1.2rem; }
.arc-cat { padding: 0; }

.cat-heading {
  display: grid;
  grid-template-columns: auto 1fr auto;
  align-items: baseline; gap: 0.7rem;
  font-size: 1.15rem;
  border: 0; padding: 0; margin: 1.4rem 0 0.6rem;
}
.cat-heading a { color: var(--ink); }
.cat-heading a:hover { color: var(--oxblood); text-decoration: none; }
.cat-rule { height: 1px; background: var(--rule); }
.cat-tally {
  font-family: var(--mono); font-size: 0.8em;
  color: var(--muted); letter-spacing: 0.05em;
}

/* ---------- Card grids with thumbnails -------------------------------- */

.card-grid {
  list-style: none; padding: 0; margin: 1rem 0 0;
  display: grid; gap: 0.9rem;
}
.card-grid-sessions {
  grid-template-columns: repeat(auto-fill, minmax(220px, 1fr));
}
.card-grid-entries {
  grid-template-columns: repeat(auto-fill, minmax(140px, 1fr));
  gap: 0.7rem;
}
.card-grid-compact {
  grid-template-columns: repeat(auto-fill, minmax(120px, 1fr));
  gap: 0.5rem;
}

.card-grid > li { display: flex; }
.thumb-card {
  display: flex; flex-direction: column;
  flex: 1;
  background: var(--paper);
  border: 1px solid var(--rule-soft);
  color: var(--ink);
  overflow: hidden;
  box-shadow: var(--shadow);
  transition: border-color 180ms ease, transform 180ms ease,
              box-shadow 220ms ease, background 180ms ease;
}
.thumb-card:hover {
  text-decoration: none;
  background: var(--paper-hi);
  border-color: var(--gold);
  transform: translateY(-2px);
  box-shadow: var(--shadow-hi);
}

.thumb-wrap {
  position: relative;
  aspect-ratio: 4 / 3;
  width: 100%;
  background: linear-gradient(135deg, var(--parchment-2), var(--card));
  overflow: hidden;
  border-bottom: 1px solid var(--rule-soft);
  flex-shrink: 0;
}
.thumb {
  width: 100%; height: 100%;
  object-fit: cover; object-position: center center;
  display: block;
  max-width: 100%; max-height: 100%;
  /* gently desaturated for visual cohesion */
  filter: saturate(0.92) contrast(0.96);
  transition: filter 220ms ease, transform 280ms ease;
}
/* Entry cards (PJ/PNJ/Lieux) usually have square portraits (round medallion
   in a square frame) — use 1:1 so the whole portrait fits without cropping. */
.entry-card .thumb-wrap { aspect-ratio: 1 / 1; }
.entry-card .thumb { object-fit: contain; background: var(--card); }
.thumb-card:hover .thumb { filter: none; transform: scale(1.02); }
.thumb-fallback {
  display: flex; align-items: center; justify-content: center;
  color: var(--gold);
  font-family: var(--serif-display);
  font-size: 2.2rem; letter-spacing: 0.06em;
  background: linear-gradient(135deg, rgba(163,122,46,0.10), rgba(122,31,31,0.07));
}
.thumb-card-body {
  padding: 0.55rem 0.7rem 0.7rem;
  display: flex; flex-direction: column; gap: 0.15rem;
  flex: 1;
  min-height: 3.4rem;
}
.session-card .snum {
  font-family: var(--serif-display);
  font-size: 0.7rem; letter-spacing: 0.18em;
  text-transform: uppercase; color: var(--gold);
}
.session-card .stitle {
  font-family: var(--serif-body);
  font-size: 0.98rem; line-height: 1.3;
  color: var(--ink);
  display: -webkit-box;
  -webkit-line-clamp: 2;
  line-clamp: 2;
  -webkit-box-orient: vertical;
  overflow: hidden;
}
.entry-card .entry-name {
  font-family: var(--serif-body);
  font-size: 0.92rem; line-height: 1.3;
  color: var(--ink);
  display: -webkit-box;
  -webkit-line-clamp: 2;
  line-clamp: 2;
  -webkit-box-orient: vertical;
  overflow: hidden;
}
.entry-card .entry-sub {
  font-family: var(--serif-body);
  font-size: 0.78rem; line-height: 1.3;
  color: var(--muted);
  font-style: italic;
  margin-top: 0.1rem;
  display: -webkit-box;
  -webkit-line-clamp: 2;
  line-clamp: 2;
  -webkit-box-orient: vertical;
  overflow: hidden;
}
.card-grid-compact .thumb-wrap { aspect-ratio: 1 / 1; }
.card-grid-compact .entry-name { font-size: 0.85rem; }
.card-grid-compact .entry-sub  { font-size: 0.72rem; -webkit-line-clamp: 1; line-clamp: 1; }
.card-grid-compact .thumb-card-body { min-height: 2.8rem; }

/* ---------- Per-session related cross-refs ---------------------------- */

.session-related { margin: 3rem 0 1.5rem; }
.session-related .section-title { text-align: left; }
.rel-cat { margin: 1.4rem 0; }
.rel-cat-heading {
  font-family: var(--serif-display);
  font-size: 0.85rem; letter-spacing: 0.18em;
  text-transform: uppercase;
  color: var(--muted);
  border: 0; padding: 0;
  margin: 0 0 0.5rem;
  display: flex; align-items: baseline; gap: 0.6rem;
}
.rel-cat-heading .count { color: var(--muted); }

/* Variant badge on category-index cards: "+N versions" indicator */
.variant-badge {
  display: inline-block;
  margin-top: 0.25rem;
  padding: 0.05rem 0.45rem;
  font-family: var(--serif-display);
  font-size: 0.7rem; letter-spacing: 0.12em;
  background: rgba(163,122,46,0.12);
  color: var(--gold);
  border: 1px solid rgba(163,122,46,0.25);
  border-radius: 0;
  align-self: flex-start;
}
.variant-badge::before { content: "+ "; opacity: 0.7; }

/* PJ index: full bios stacked vertically, the way the blog presents them */
.bio-list { display: flex; flex-direction: column; gap: 3rem;
            margin-top: 2rem; }
.bio { border-top: 1px solid var(--rule); padding-top: 2rem; }
.bio:first-child { border-top: 0; padding-top: 0; }
.bio-title { font-family: var(--serif-display);
             font-size: 1.7rem; margin: 0 0 0.6rem;
             border-bottom: 0; padding: 0; }
.bio-title a { color: var(--ink); }
.bio-title a:hover { color: var(--oxblood); text-decoration: none; }
.bio-body { font-size: 1rem; line-height: 1.65; color: var(--ink); }
.bio-body p { margin: 0.7rem 0; }
.bio-body img { display: block; margin: 1rem auto; max-height: 360px;
                 border: 1px solid var(--rule); }
.bio-body a { color: var(--oxblood); }
.bio-more { margin-top: 1rem; font-family: var(--serif-display);
            font-size: 0.78rem; letter-spacing: 0.14em; }
.bio-more a { color: var(--gold); }
.bio-more a:hover { color: var(--oxblood); }

/* Appearances: sessions where a PNJ/PJ/Lieu/Doc shows up, grouped by arc.
   Native <details>: collapsible, no JS. */
.appearances { margin: 2rem 0 1.2rem; }
.appearances-summary {
  list-style: none;        /* hide default disclosure marker */
  cursor: pointer;
  font-family: var(--serif-display);
  font-size: 1.05rem; letter-spacing: 0.22em;
  text-transform: uppercase;
  color: var(--ink);
  padding: 0.5rem 0;
  border-bottom: 1px solid var(--rule);
  display: flex; align-items: baseline; gap: 0.8rem;
  user-select: none;
  transition: color 150ms;
}
.appearances-summary:hover { color: var(--oxblood); }
.appearances-summary::-webkit-details-marker { display: none; }
.appearances-summary::before {
  content: "▸";
  font-size: 0.85em; color: var(--gold);
  display: inline-block;
  transition: transform 180ms ease;
  width: 0.8em;
}
.appearances[open] > .appearances-summary::before {
  transform: rotate(90deg);
}
.appearances-count {
  font-family: var(--serif-body);
  font-size: 0.78rem; font-style: italic;
  text-transform: none; letter-spacing: 0;
  color: var(--muted);
  margin-left: auto;
}
.appearances-content {
  padding: 0.5rem 0 0;
  animation: appearances-fade 200ms ease-out;
}
@keyframes appearances-fade {
  from { opacity: 0; transform: translateY(-3px); }
  to   { opacity: 1; transform: translateY(0); }
}
.appearances-arc { margin: 1.2rem 0 1.5rem; }
.appearances-arc-title {
  font-family: var(--serif-display);
  font-size: 0.85rem; letter-spacing: 0.18em;
  text-transform: uppercase;
  margin: 0 0 0.4rem;
  border: 0; padding: 0;
}
.appearances-arc-title a { color: var(--oxblood); }
.appearances-arc-title a:hover { color: var(--oxblood-hi); text-decoration: none; }
.appearances-sessions { list-style: none; padding: 0; margin: 0; }
.appearances-sessions li { margin: 0; }
.appearances-sessions a {
  display: grid; grid-template-columns: 6rem 1fr;
  gap: 0.6rem; align-items: baseline;
  padding: 0.35rem 0.3rem;
  border-bottom: 1px dotted var(--rule-soft);
  color: var(--ink);
}
.appearances-sessions a:hover { background: rgba(163,122,46,0.06);
                                 text-decoration: none; }
.appearances-sessions .snum {
  font-family: var(--serif-display);
  font-size: 0.78rem; letter-spacing: 0.14em;
  text-transform: uppercase; color: var(--gold);
}
.appearances-sessions .stitle { font-family: var(--serif-body); }

/* Variants section on individual pages */
.variants-section { margin: 2.5rem 0 1.5rem; }
.variants-section .section-title { text-align: left; margin-top: 0; }
.variant-card .variant-role {
  font-family: var(--serif-display);
  font-size: 0.65rem; letter-spacing: 0.16em;
  text-transform: uppercase; color: var(--oxblood);
  margin-top: 0.2rem;
}

/* PJ pages: variants rendered as stacked inline bios with floating portrait */
.variants-bios { display: flex; flex-direction: column; gap: 2.5rem;
                  margin-top: 1.5rem; }
.variant-bio { border-top: 1px solid var(--rule); padding-top: 1.8rem; }
.variant-bio:first-child { border-top: 0; padding-top: 0; }
.variant-bio > .appearances { clear: both; margin-top: 1.4rem; }
.variant-bio-title {
  font-family: var(--serif-display-2);
  font-size: 1.35rem; line-height: 1.2;
  margin: 0 0 1rem;
  border-bottom: 0; padding-bottom: 0;
  display: flex; flex-wrap: wrap; align-items: baseline; gap: 0.7rem;
}
.variant-bio-title a { color: var(--ink); }
.variant-bio-title a:hover { color: var(--oxblood); text-decoration: none; }
.variant-flag {
  font-family: var(--serif-display);
  font-size: 0.65rem; letter-spacing: 0.18em;
  text-transform: uppercase; color: var(--oxblood);
  padding: 0.1rem 0.5rem; border: 1px solid var(--oxblood);
  font-weight: normal;
}
.variant-bio-body { font-size: 0.98rem; line-height: 1.65;
                     color: var(--ink-soft);
                     overflow: hidden; /* contain the floated portrait so
                                          the apparitions block starts on
                                          a clean line below */ }
.variant-bio-body > h1,
.variant-bio-body > h2 { display: none; /* hide blogger's inner heading */ }
.variant-bio-body p { margin: 0.7rem 0; }
.variant-bio-body img { float: left;
                         margin: 0.2rem 1.2rem 0.6rem 0;
                         max-width: 170px; max-height: 170px;
                         border-radius: 50%; border: 0; }
@media (max-width: 540px) {
  .variant-bio-body img { float: none; display: block;
                           margin: 0.5rem auto 1rem; max-width: 200px; }
}

/* ---------- POST PAGE -------------------------------------------------- */

.post h1 { font-size: 2.1rem; margin-bottom: 0.5rem; }
.post-body { margin: 1.4rem 0; font-size: 1.05rem; line-height: 1.8; }
.post-body p { margin: 1rem 0; }
/* Drop cap only on session recaps — bio pages (PJ/PNJ/Lieux/Doc/Univers)
   often have a short bold subtitle as their first paragraph, which the
   ::first-letter float would mangle. */
.post-session .post-body p:first-of-type::first-letter {
  font-family: var(--serif-display);
  font-size: 3.9rem; line-height: 0.85;
  float: left; padding: 0.2rem 0.5rem 0 0;
  color: var(--oxblood);
}
.post-body img { display: block; margin: 1.8rem auto;
                  max-height: 520px;
                  border: 1px solid var(--rule);
                  box-shadow: var(--shadow); }
.post-body a img { box-shadow: var(--shadow-hi); }
.post-body em, .post-body i { color: var(--ink-soft); }
.post-body strong, .post-body b { color: var(--ink); }

.post-nav {
  display: grid; grid-template-columns: 1fr 1fr;
  gap: 0.6rem; margin: 1.2rem 0;
  font-family: var(--serif-display);
  font-size: 0.82rem; letter-spacing: 0.08em;
}
.post-nav a, .post-nav span {
  display: inline; padding: 0.35rem 0.1rem;
  color: var(--ink); background: transparent;
  border: 0;
}
.post-nav span { color: transparent; }
.post-nav a:hover { color: var(--oxblood); text-decoration: none; }
.post-nav .prev { text-align: left; }
.post-nav .next { text-align: right; }
.post-nav .nav-eyebrow {
  display: block;
  font-size: 0.7rem; letter-spacing: 0.18em;
  color: var(--muted); text-transform: uppercase;
  font-family: var(--serif-display);
  margin-bottom: 0.1rem;
}
.post-nav .nav-label {
  display: block;
  font-family: var(--serif-body);
  font-size: 0.92rem; line-height: 1.3;
  color: var(--ink);
}
.post-nav .nav-to-arc .nav-eyebrow { color: var(--gold); }
.post-nav .nav-to-arc .nav-label { color: var(--oxblood); font-style: italic; }
.post-nav .nav-to-arc:hover .nav-label { color: var(--oxblood-hi); }
.post-nav-top { margin-bottom: 2rem; }
.post-nav-bottom { margin-top: 3rem; }

.source { color: var(--muted); font-size: 0.85rem; margin-top: 2rem;
          font-style: italic;
          padding-top: 1rem; border-top: 1px solid var(--rule);
          word-break: break-all; }
.source a { color: var(--muted); }

/* ---------- Category index pages -------------------------------------- */

.page-cat .session-list a { padding: 0.5rem 0.4rem; }

/* ---------- Entity popovers (résumé bodies) --------------------------- */

.entity-pop {
  color: var(--ink);
  border-bottom: 1px dotted var(--gold);
  text-decoration: none;
  cursor: help;
  transition: color 120ms ease, border-color 120ms ease;
}
.entity-pop:hover { color: var(--oxblood); border-bottom-color: currentColor; }

.entity-popover {
  position: fixed; z-index: 100;
  display: none;
  align-items: center; gap: 0.7rem;
  padding: 0.55rem 0.85rem 0.55rem 0.55rem;
  background: var(--paper);
  border: 1px solid var(--rule);
  box-shadow: var(--shadow-hi);
  max-width: 260px;
  animation: entity-pop-fade 140ms ease-out;
  pointer-events: auto;
}
@keyframes entity-pop-fade {
  from { opacity: 0; transform: translateY(2px); }
  to   { opacity: 1; transform: translateY(0); }
}
.entity-popover img {
  width: 60px; height: 60px;
  object-fit: cover;
  border: 1px solid var(--rule-soft);
  flex-shrink: 0;
}
.entity-popover-body {
  display: flex; flex-direction: column; gap: 0.15rem;
  min-width: 0;
}
.entity-popover-cat {
  font-family: var(--serif-display);
  font-size: 0.7rem; letter-spacing: 0.18em;
  text-transform: uppercase; color: var(--gold);
}
.entity-popover-name {
  font-family: var(--serif-body);
  font-size: 0.98rem; line-height: 1.25;
  color: var(--ink);
}
.entity-popover-subtitle {
  font-family: var(--serif-body);
  font-size: 0.82rem; line-height: 1.3;
  color: var(--ink-soft);
  font-style: italic;
}

/* Hide hover popover entirely on touch devices — tap navigates instead. */
@media (hover: none) {
  .entity-popover { display: none !important; }
}

/* ---------- Responsive ------------------------------------------------- */

@media (max-width: 920px) {
  :root { --sidebar-w: 220px; }
  main { padding: 2rem 1.5rem 3rem; }
}
@media (max-width: 760px) {
  .layout { grid-template-columns: 1fr; }
  .menu-toggle { display: inline-flex; }
  .sidebar {
    display: none;
    position: static; max-height: none;
    border-right: 0; border-bottom: 1px solid var(--rule);
    background: var(--card);
  }
  body.menu-open .sidebar { display: block; }
  main { padding: 1.5rem 1.2rem 3rem; }
  .hero-title { font-size: clamp(2rem, 8vw, 2.6rem); }
  .arc-card { grid-template-columns: 2.5rem 1fr; padding: 0.8rem; }
  .arc-roman { font-size: 1.8rem; }
}
"""


# --------------------------------------------------------------------------- #
# Build
# --------------------------------------------------------------------------- #


ROBOTS_TXT = f"""\
User-agent: *
Allow: /

Sitemap: {SITE_URL}/sitemap.xml
"""


def build_sitemap(pages: list[Page]) -> str:
    """Sitemap covering home, arc landings, category indexes, and every
    canonical post page (variants are skipped — they redirect to their main)."""
    entries: list[tuple[str, str | None]] = [(f"{SITE_URL}/", None)]
    for arc in ARCS:
        entries.append((f"{SITE_URL}/arc-{arc.num}.html", None))
    for out_folder, _src, _lbl in CATEGORIES:
        entries.append((f"{SITE_URL}/{out_folder}/index.html", None))
    for pg in pages:
        if pg.variant_group and not pg.is_main:
            continue
        loc = f"{SITE_URL}/{pg.site_rel.as_posix()}"
        lastmod = (pg.post.updated or pg.post.published or "")[:10] or None
        entries.append((loc, lastmod))

    lines = ['<?xml version="1.0" encoding="UTF-8"?>',
             '<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">']
    for loc, lastmod in entries:
        if lastmod:
            lines.append(f'  <url><loc>{html.escape(loc)}</loc>'
                         f'<lastmod>{lastmod}</lastmod></url>')
        else:
            lines.append(f'  <url><loc>{html.escape(loc)}</loc></url>')
    lines.append('</urlset>')
    return "\n".join(lines) + "\n"


def write(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")


# --------------------------------------------------------------------------- #
# MJ overlay — Phase 1 implementation
# --------------------------------------------------------------------------- #

_MJ_WIKILINK_RE = re.compile(r"\[\[(?P<target>[^|\]\n]+?)(?:\|(?P<alias>[^\]\n]+?))?\]\]")


@dataclass
class MJOverlayPage:
    src_path: Path
    out_rel_path: Path          # path under MJ_OUT_DIR
    title: str
    body_md: str
    category: str               # scenario_hub | scenario_scene | scenario_ref | mj_note
    scenario: str | None = None
    arc: int | None = None


def _mj_slug(s: str) -> str:
    s = unicodedata.normalize("NFKD", s)
    s = "".join(c for c in s if not unicodedata.combining(c))
    s = re.sub(r"[^\w\s-]", "", s).strip().lower()
    s = re.sub(r"[\s_]+", "-", s)
    s = re.sub(r"-+", "-", s)
    return s


def _md_to_html(text: str) -> str:
    """Markdown → HTML. Uses markdown-it-py."""
    try:
        from markdown_it import MarkdownIt
    except ImportError:
        return f"<pre>{html.escape(text)}</pre>"
    md = (MarkdownIt("commonmark", {"linkify": False, "html": True, "breaks": False})
          .enable(["table", "strikethrough"]))
    return md.render(text)


def _parse_carte_block(text: str) -> dict[str, str]:
    """Extract the nested `carte:` block from a fiche's YAML frontmatter.
    Returns a flat dict of its inner keys (map/kind/type/importance/section/
    quarter/x/y/desc/legend). Empty dict if no frontmatter or no carte block.
    The whole frontmatter is stripped from the body by `_parse_frontmatter`,
    so adding this block to a Lieux fiche does not affect its rendered content."""
    if not text.startswith("---"):
        return {}
    lines = text.splitlines()
    if not lines or lines[0].strip() != "---":
        return {}
    end = None
    for i in range(1, len(lines)):
        if lines[i].strip() == "---":
            end = i
            break
    if end is None:
        return {}
    out: dict[str, str] = {}
    in_block = False
    for ln in lines[1:end]:
        if re.match(r"^\s*carte\s*:\s*$", ln):
            in_block = True
            continue
        if in_block:
            m = re.match(r"^(\s+)([A-Za-z_][\w-]*)\s*:\s*(.*)$", ln)
            if m:
                out[m.group(2)] = m.group(3).strip().strip('"').strip("'")
            elif ln.strip():
                break  # dedented non-empty line → end of carte block
    return out


def _parse_frontmatter(text: str) -> tuple[dict[str, str], str]:
    """Extract minimal YAML-style frontmatter at the top of a .md file.
    Returns (frontmatter dict, remaining body). Supports only `key: value`
    pairs (no nested structures, no lists)."""
    if not text.startswith("---"):
        return {}, text
    lines = text.splitlines()
    if not lines or lines[0].strip() != "---":
        return {}, text
    fm: dict[str, str] = {}
    end_idx = None
    for i in range(1, len(lines)):
        if lines[i].strip() == "---":
            end_idx = i
            break
        m = re.match(r"^\s*([A-Za-z_][A-Za-z_0-9-]*)\s*:\s*(.+?)\s*$", lines[i])
        if m:
            fm[m.group(1)] = m.group(2).strip().strip('"').strip("'")
    if end_idx is None:
        return {}, text
    return fm, "\n".join(lines[end_idx + 1:])


def _scenario_arc_from_hub(hub_path: Path) -> int | None:
    """Read the `arc:` field from the YAML frontmatter of a scenario Hub.md."""
    if not hub_path.exists():
        return None
    fm, _ = _parse_frontmatter(hub_path.read_text(encoding="utf-8"))
    arc_str = fm.get("arc")
    if arc_str is None:
        return None
    try:
        return int(arc_str)
    except (ValueError, TypeError):
        return None


def _walk_notes_mj_overlay() -> list[MJOverlayPage]:
    """Walk Notes MJ/ and produce MJOverlayPage objects for Phase 1 categories.
    Scenario→arc mapping is discovered from YAML frontmatter in each Hub.md."""
    if not NOTES_MJ_DIR.exists():
        return []
    pages: list[MJOverlayPage] = []

    # Scénarios
    sc_root = NOTES_MJ_DIR / "Scénarios"
    if sc_root.exists():
        for scenario_dir in sorted(sc_root.iterdir()):
            if not scenario_dir.is_dir():
                continue
            sc_name = scenario_dir.name
            sc_slug = _mj_slug(sc_name)
            arc_num = _scenario_arc_from_hub(scenario_dir / "Hub.md")

            for f in sorted(scenario_dir.glob("*.md")):
                stem = f.stem
                if stem == "Hub":
                    category = "scenario_hub"
                    out_rel = Path("scenarios") / sc_slug / "index.html"
                elif re.match(r"^\d", stem):
                    category = "scenario_scene"
                    out_rel = Path("scenarios") / sc_slug / f"{_mj_slug(stem)}.html"
                else:
                    category = "scenario_ref"
                    out_rel = Path("scenarios") / sc_slug / f"{_mj_slug(stem)}.html"
                pages.append(MJOverlayPage(
                    src_path=f, out_rel_path=out_rel, title=stem,
                    body_md=_parse_frontmatter(f.read_text(encoding="utf-8"))[1],
                    category=category, scenario=sc_name, arc=arc_num))

    # Notes MJ/Documents/, PNJ/, Lieux/, Factions/ are handled in Phase 3.

    # Notes MJ thématiques — root NN-prefixed files
    for f in sorted(NOTES_MJ_DIR.glob("[0-9][0-9] - *.md")):
        pages.append(MJOverlayPage(
            src_path=f,
            out_rel_path=Path("notes") / f"{_mj_slug(f.stem)}.html",
            title=f.stem, body_md=_parse_frontmatter(f.read_text(encoding="utf-8"))[1],
            category="mj_note"))

    # Turmoil/
    turmoil = NOTES_MJ_DIR / "Turmoil"
    if turmoil.exists():
        for f in sorted(turmoil.glob("*.md")):
            pages.append(MJOverlayPage(
                src_path=f,
                out_rel_path=Path("notes") / "turmoil" / f"{_mj_slug(f.stem)}.html",
                title=f.stem, body_md=_parse_frontmatter(f.read_text(encoding="utf-8"))[1],
                category="mj_note"))

    # Arcs/
    arcs_dir = NOTES_MJ_DIR / "Arcs"
    if arcs_dir.exists():
        for f in sorted(arcs_dir.glob("*.md")):
            pages.append(MJOverlayPage(
                src_path=f,
                out_rel_path=Path("notes") / "arcs" / f"{_mj_slug(f.stem)}.html",
                title=f.stem, body_md=_parse_frontmatter(f.read_text(encoding="utf-8"))[1],
                category="mj_note"))

    return pages


def _build_mj_wikilink_index(mj_pages: list[MJOverlayPage]) -> dict[str, Path]:
    """Map normalized stem → out_rel_path. Used to resolve [[wikilinks]]."""
    idx: dict[str, Path] = {}
    for p in mj_pages:
        keys = {p.title.lower()}
        m = re.match(r"^\d+\s*-\s*(.+)$", p.title)
        if m:
            keys.add(m.group(1).strip().lower())
        if p.scenario:
            keys.add(f"{p.scenario}/{p.title}".lower())
            if p.title == "Hub":
                keys.add(p.scenario.lower())
        for k in keys:
            idx.setdefault(k, p.out_rel_path)
    return idx


def _relpath_within_mj(from_dir: Path, to_path: Path) -> str:
    """Compute a relative URL from a dir to a path, both relative to MJ_OUT_DIR."""
    from_parts = list(from_dir.parts)
    to_parts = list(to_path.parts)
    common = 0
    while (common < min(len(from_parts), len(to_parts))
           and from_parts[common] == to_parts[common]):
        common += 1
    ups = len(from_parts) - common
    return "../" * ups + "/".join(to_parts[common:])


def _resolve_mj_wikilinks(text: str, current_out_rel: Path,
                          mj_idx: dict[str, Path],
                          url_map: dict[str, Path] | None = None) -> str:
    """Replace [[wikilinks]] in markdown.
    - If target matches an MJ overlay page (scene, hub, note) → produce a link.
    - Otherwise → strip the brackets and return plain text. The entity popover
      system (`inject_entity_popovers`) detects entity names in prose and
      handles linking + tooltips for PNJ / Lieux / Factions / Documents.
    Public link resolution by name is no longer the job of [[…]]."""
    def repl(m: re.Match) -> str:
        raw_target = m.group("target").strip()
        alias = (m.group("alias") or raw_target).strip()
        anchor = ""
        if "#" in raw_target:
            raw_target, anchor_text = raw_target.split("#", 1)
            anchor = "#" + _mj_slug(anchor_text.strip())
            raw_target = raw_target.strip()

        keys = _norm_keys_for_match(raw_target)
        keys.add(raw_target.lower())
        for k in keys:
            if k in mj_idx:
                target_out = mj_idx[k]
                rel = _relpath_within_mj(current_out_rel.parent, target_out)
                return f"[{alias}]({quote(rel.replace(chr(92), '/'), safe='/#')}{anchor})"

        # No MJ overlay match → fall through to plain text. The popover system
        # will tooltipize entity names in the resulting HTML.
        return alias

    return _MJ_WIKILINK_RE.sub(repl, text)


_SCENARIO_THREAD_LABELS = {
    0: "Ouverture", 1: "Le marteau", 2: "Officiel", 3: "Empereur",
    4: "Ville & perso", 5: "Déplacement", 6: "Départ",
}
_SCENARIO_SIDEBAR_RELABEL = {
    "Ambiance": "⚡ Écran live (ville)",
}


def _render_scenario_sidebar(scenario_name: str,
                             siblings: list[MJOverlayPage],
                             current_out_rel: Path) -> str:
    """Left rail listing every page of one scenario, grouped by thread, with
    the Hub pinned on top and the current page highlighted."""
    hub = None
    scenes_by_thread: dict[int, list[tuple[int, MJOverlayPage]]] = {}
    refs: list[MJOverlayPage] = []
    for p in siblings:
        if p.category == "scenario_hub":
            hub = p
            continue
        m = re.match(r"\s*(\d+)", p.title)
        if m:
            num = int(m.group(1))
            scenes_by_thread.setdefault(num // 10, []).append((num, p))
        else:
            refs.append(p)

    def link(p: MJOverlayPage, pinned: bool = False) -> str:
        href = _relpath_within_mj(current_out_rel.parent, p.out_rel_path)
        label = _SCENARIO_SIDEBAR_RELABEL.get(p.title, p.title)
        is_active = (p.out_rel_path == current_out_rel)
        cls = "scn-link is-active" if is_active else "scn-link"
        cur = ' aria-current="page"' if is_active else ''
        star = '★ ' if pinned else ''
        return (f'<a class="{cls}" href="{html.escape(href)}"{cur}>'
                f'{star}{html.escape(label)}</a>')

    parts = ['<nav class="sidebar sidebar-scenario" aria-label="Navigation du scénario">',
             f'<h2 class="sidebar-title">{html.escape(scenario_name)}</h2>',
             '<div class="scn-nav">']
    if hub is not None:
        parts.append(link(hub, pinned=True))
    for tens in sorted(scenes_by_thread):
        label = _SCENARIO_THREAD_LABELS.get(tens, f"{tens}x")
        parts.append(f'<div class="scn-group"><span class="scn-group-h">{html.escape(label)}</span>')
        for _num, p in sorted(scenes_by_thread[tens], key=lambda t: t[0]):
            parts.append(link(p))
        parts.append('</div>')
    if refs:
        refs_sorted = sorted(refs, key=lambda p: (p.title != "Ambiance", p.title.lower()))
        parts.append('<div class="scn-group"><span class="scn-group-h">Référence</span>')
        for p in refs_sorted:
            parts.append(link(p))
        parts.append('</div>')
    parts.append('</div>')
    parts.append('</nav>')
    return "\n".join(parts)


def _render_mj_overlay_page(mj_page: MJOverlayPage,
                            mj_idx: dict[str, Path],
                            url_map: dict[str, Path],
                            buckets: dict[int, ArcBucket],
                            entity_popover_map: dict[str, EntityPopover] | None = None,
                            scenario_siblings: list[MJOverlayPage] | None = None) -> str:
    """Render an MJ overlay page using the standard site layout."""
    current_out_rel = mj_page.out_rel_path
    md_text = _resolve_mj_wikilinks(
        mj_page.body_md, current_out_rel, mj_idx, url_map)
    html_body = _md_to_html(md_text)

    # Phase 4: apply entity popover system to scenario / handout / note bodies.
    # Use _MJ_POPOVER_MAP (alias-resolved + MJ-only entities) instead of the
    # raw blog popover map, so canonical Notes MJ spellings fire popovers.
    popover_map = _MJ_POPOVER_MAP if _MJ_POPOVER_MAP else entity_popover_map
    if popover_map:
        current_dir_rel = (MJ_OUT_DIR / current_out_rel).parent.relative_to(OUT)
        html_body = inject_entity_popovers(html_body, popover_map, current_dir_rel)

    # MJ-only canon refs: <code>EiR Intro l.205-218</code> → hover popover + click navigation
    current_dir_from_out = (MJ_OUT_DIR / current_out_rel).parent.relative_to(OUT)
    html_body = inject_canon_refs(html_body, current_dir_from_out)

    # Breadcrumb
    crumbs = ['<a href="' + _relpath_within_mj(current_out_rel.parent, Path("index.html")) + '">Notes MJ</a>']
    if mj_page.category in ("scenario_hub", "scenario_scene", "scenario_ref"):
        sc_slug = _mj_slug(mj_page.scenario) if mj_page.scenario else ""
        crumbs.append('<a href="' + _relpath_within_mj(
            current_out_rel.parent, Path("scenarios") / "index.html") + '">Scénarios</a>')
        if mj_page.category != "scenario_hub":
            crumbs.append('<a href="' + _relpath_within_mj(
                current_out_rel.parent, Path("scenarios") / sc_slug / "index.html")
                + f'">{html.escape(mj_page.scenario or "")}</a>')
        else:
            crumbs.append(f'<span>{html.escape(mj_page.scenario or "")}</span>')
    elif mj_page.category == "mj_note":
        crumbs.append('<a href="' + _relpath_within_mj(
            current_out_rel.parent, Path("notes") / "index.html") + '">Notes thématiques</a>')

    breadcrumb_html = '<nav class="mj-breadcrumb">' + " · ".join(crumbs) + '</nav>'

    body = f"""
{breadcrumb_html}
<article class="post mj-content">
<div class="post-body">
{html_body}
</div>
</article>
"""
    sidebar_override = None
    if (mj_page.category in ("scenario_hub", "scenario_scene", "scenario_ref")
            and scenario_siblings):
        sidebar_override = _render_scenario_sidebar(
            mj_page.scenario or "", scenario_siblings, current_out_rel)

    return layout(current_dir_from_out, mj_page.title, body,
                  extra_class="page-mj-overlay", buckets=buckets,
                  sidebar_override=sidebar_override)


def _render_mj_index_pages(mj_pages: list[MJOverlayPage],
                           buckets: dict[int, ArcBucket]) -> dict[Path, str]:
    """Render the MJ navigation index pages (root index, scenarios, notes)."""
    out: dict[Path, str] = {}

    # Group by category (Phase 1: scenarios + thematic MJ notes only)
    scenarios_by_slug: dict[str, list[MJOverlayPage]] = {}
    notes: list[MJOverlayPage] = []
    for p in mj_pages:
        if p.category in ("scenario_hub", "scenario_scene", "scenario_ref"):
            scenarios_by_slug.setdefault(_mj_slug(p.scenario or ""), []).append(p)
        elif p.category == "mj_note":
            notes.append(p)

    # Root MJ index
    root_body = ['<h1>Notes MJ</h1>',
                 '<p>Espace privé MJ — non visible côté joueurs.</p>',
                 '<div class="mj-cat-list">']
    if scenarios_by_slug:
        root_body.append('<section class="arc-cat"><h2 class="cat-heading">'
                         '<a href="scenarios/index.html">Scénarios</a>'
                         '<span class="cat-rule"></span>'
                         f'<span class="cat-tally">{len(scenarios_by_slug)}</span></h2></section>')
    if notes:
        root_body.append('<section class="arc-cat"><h2 class="cat-heading">'
                         '<a href="notes/index.html">Notes thématiques</a>'
                         '<span class="cat-rule"></span>'
                         f'<span class="cat-tally">{len(notes)}</span></h2></section>')
    root_body.append('</div>')
    mj_root = Path(f"mj-{MJ_TOKEN}")
    out[Path("index.html")] = layout(mj_root, "Notes MJ — Index",
                                     "\n".join(root_body),
                                     extra_class="page-mj-overlay", buckets=buckets)

    # Scenarios index (grouped by arc)
    sc_body = ['<h1>Scénarios</h1>',
               '<p>Homebrew scenarios groupés par arc.</p>']
    by_arc: dict[int | None, list[tuple[str, str, MJOverlayPage]]] = {}
    for sc_slug, scenes in scenarios_by_slug.items():
        hub_candidates = [s for s in scenes if s.category == "scenario_hub"]
        hub = hub_candidates[0] if hub_candidates else scenes[0]
        by_arc.setdefault(hub.arc, []).append((sc_slug, hub.scenario or sc_slug, hub))
    for arc_num in sorted(by_arc.keys(), key=lambda x: (x is None, x or 0)):
        if arc_num is None:
            sc_body.append('<section class="arc-cat"><h2 class="cat-heading">Sans arc<span class="cat-rule"></span></h2><ul class="card-grid card-grid-entries">')
        else:
            sc_body.append(f'<section class="arc-cat"><h2 class="cat-heading">Arc {to_roman(arc_num)}<span class="cat-rule"></span></h2><ul class="card-grid card-grid-entries">')
        for sc_slug, sc_name, hub in sorted(by_arc[arc_num], key=lambda x: x[1].lower()):
            sc_body.append(f'<li><a class="entry-card" href="../scenarios/{sc_slug}/index.html"><span class="entry-title">{html.escape(sc_name)}</span></a></li>')
        sc_body.append('</ul></section>')
    out[Path("scenarios") / "index.html"] = layout(
        mj_root / "scenarios", "Scénarios — MJ", "\n".join(sc_body),
        extra_class="page-mj-overlay", buckets=buckets)

    # Per-scenario index (the Hub becomes this, but we also add a navigation page if no Hub)
    # The Hub is already rendered as scenarios/<slug>/index.html via _render_mj_overlay_page.
    # Nothing to add here.

    # Notes index
    if notes:
        nt_body = ['<h1>Notes thématiques</h1>', '<ul class="card-grid card-grid-entries">']
        for p in sorted(notes, key=lambda x: x.title.lower()):
            rel = _relpath_within_mj(Path("notes"), p.out_rel_path)
            nt_body.append(f'<li><a class="entry-card" href="{quote(rel)}">'
                           f'<span class="entry-title">{html.escape(p.title)}</span></a></li>')
        nt_body.append('</ul>')
        out[Path("notes") / "index.html"] = layout(
            mj_root / "notes", "Notes thématiques — MJ", "\n".join(nt_body),
            extra_class="page-mj-overlay", buckets=buckets)

    return out


# --------------------------------------------------------------------------- #
# Phase 3.5 — canon Source pages (MJ-only, target of canon-ref click)
# --------------------------------------------------------------------------- #


_FENCE_RE = re.compile(r'^\s*(?:```|~~~)')


def _md_with_line_anchors(md_text: str) -> str:
    """Inject `<a class="line-anchor" id="L{N}"></a>` markers into each
    non-empty raw markdown line. The anchor goes AFTER any leading block
    construct markers (`#`, `##`, list bullets, blockquote `>`) so that
    markdown-it still recognises the construct.

    Skips lines inside fenced code blocks (``` or ~~~) since prepending HTML
    there would corrupt the fence content. Empty lines are left untouched
    (they act as paragraph breaks).

    The output is still valid markdown — markdown-it-py with html: true
    passes the inline anchor through, so it ends up as the first child of
    the produced HTML element (heading first-child, paragraph first-child,
    list-item first-child, …)."""
    lines = md_text.splitlines()
    out: list[str] = []
    in_fence = False
    # Match leading: indentation, then ATX heading hashes / list bullet /
    # ordered list / blockquote markers (possibly nested), with required
    # trailing whitespace before the content.
    prefix_re = re.compile(
        r'^(\s*'
        r'(?:'
        r'#{1,6}\s+'                    # ATX heading
        r'|[-*+]\s+'                    # bullet list
        r'|\d+[.)]\s+'                  # ordered list
        r'|>\s*'                        # blockquote
        r')*'
        r')'
    )
    pending_blank_anchors: list[str] = []
    for i, line in enumerate(lines, start=1):
        if _FENCE_RE.match(line):
            in_fence = not in_fence
            out.append(line)
            continue
        if in_fence:
            out.append(line)
            continue
        if not line.strip():
            # Record the blank line's anchor; we'll attach it to the next
            # non-blank line so that markdown still sees a paragraph break
            # AND `#L{i}` for a blank line resolves to a real DOM target.
            pending_blank_anchors.append(f'<a class="line-anchor" id="L{i}"></a>')
            out.append(line)  # preserve the blank line for markdown parsing
            continue
        # Skip lines that look like table rows (start with `|`): prepending
        # HTML there breaks markdown-it's table parser. Table rows lose
        # their anchors but the table renders; ref jumps into the middle of
        # a table still land on the nearest non-table line.
        if line.lstrip().startswith("|"):
            # Carry blank-line anchors through to the next anchorable line.
            out.append(line)
            continue
        m = prefix_re.match(line)
        prefix = m.group(1) if m else ""
        rest = line[len(prefix):]
        anchor = f'<a class="line-anchor" id="L{i}"></a>'
        # Carry over any anchors from immediately preceding blank lines so
        # `#L{blank-line}` is still scrollable. They render as zero-width
        # spans inside the same block — invisible, but valid targets.
        prepended = "".join(pending_blank_anchors)
        pending_blank_anchors = []
        out.append(f'{prefix}{prepended}{anchor}{rest}')
    return "\n".join(out)


def _render_canon_source_pages(buckets: dict[int, ArcBucket]) -> dict[Path, str]:
    """Render every Source/*.md file covered by _CANON_BOOK_DIRS into a
    standalone MJ HTML page under mj-{TOKEN}/source/{book-slug}/{file-slug}.html.

    Each page renders the source markdown with per-line anchor markers
    (#L1, #L2, …) so canon-ref links can deep-link to the cited line.

    Also produces:
    - mj-{TOKEN}/source/index.html       (book index)
    - mj-{TOKEN}/source/{book}/index.html (per-book chapter list)
    """
    out: dict[Path, str] = {}
    if not MJ_TOKEN or not SOURCE_DIR.exists():
        return out

    routes = _build_canon_source_route_map()
    # Group files by book_slug for the per-book index
    files_by_book: dict[str, list[tuple[str, Path, str]]] = {}
    for abbrev, subdir in _CANON_BOOK_DIRS.items():
        book_dir = SOURCE_DIR / subdir
        if not book_dir.exists():
            continue
        book_slug = _canon_book_slug(abbrev)
        for f in sorted(book_dir.glob("*.md")):
            if f.name.lower().startswith("00 - index"):
                continue
            file_slug = _mj_slug(f.stem)
            files_by_book.setdefault(book_slug, []).append((abbrev, f, file_slug))

    if not files_by_book:
        return out

    # Render each file
    for book_slug, file_entries in files_by_book.items():
        for abbrev, f, file_slug in file_entries:
            try:
                md_text = f.read_text(encoding="utf-8")
            except OSError:
                continue
            # Strip optional YAML frontmatter (rare in Source/ but safe)
            _, md_body = _parse_frontmatter(md_text)
            md_anchored = _md_with_line_anchors(md_body)
            body_html = _md_to_html(md_anchored)

            out_rel = Path("source") / book_slug / f"{file_slug}.html"
            current_dir_from_out = (MJ_OUT_DIR / out_rel).parent.relative_to(OUT)

            # Breadcrumb
            book_index_href = _relpath_within_mj(
                out_rel.parent, Path("source") / book_slug / "index.html")
            source_index_href = _relpath_within_mj(
                out_rel.parent, Path("source") / "index.html")
            mj_index_href = _relpath_within_mj(
                out_rel.parent, Path("index.html"))
            crumbs = (
                f'<a href="{mj_index_href}">Notes MJ</a> · '
                f'<a href="{source_index_href}">Source</a> · '
                f'<a href="{book_index_href}">{html.escape(abbrev)}</a> · '
                f'<span>{html.escape(f.stem)}</span>'
            )
            page_body = (
                f'<nav class="mj-breadcrumb">{crumbs}</nav>'
                f'<article class="post mj-content canon-source-page">'
                f'<header class="post-header"><h1 class="post-title">'
                f'{html.escape(abbrev)} — {html.escape(f.stem)} '
                f'<span class="mj-badge">Source</span></h1></header>'
                f'<div class="post-body">{body_html}</div>'
                f'</article>'
            )
            out[out_rel] = layout(current_dir_from_out, f"{abbrev} — {f.stem}",
                                  page_body,
                                  extra_class="page-mj-overlay canon-source-page",
                                  buckets=buckets)

    # Per-book chapter index
    for book_slug, file_entries in files_by_book.items():
        abbrev = file_entries[0][0]
        items = []
        for _abbrev, f, file_slug in file_entries:
            items.append(
                f'<li><a class="entry-card" href="{quote(file_slug)}.html">'
                f'<span class="entry-title">{html.escape(f.stem)}</span></a></li>')
        body = (
            f'<nav class="mj-breadcrumb">'
            f'<a href="../../index.html">Notes MJ</a> · '
            f'<a href="../index.html">Source</a> · '
            f'<span>{html.escape(abbrev)}</span>'
            f'</nav>'
            f'<h1>{html.escape(abbrev)}</h1>'
            f'<p>{len(file_entries)} fichier(s) source.</p>'
            f'<ul class="card-grid card-grid-entries">{"".join(items)}</ul>'
        )
        out_rel = Path("source") / book_slug / "index.html"
        current_dir_from_out = (MJ_OUT_DIR / out_rel).parent.relative_to(OUT)
        out[out_rel] = layout(current_dir_from_out, f"{abbrev} — Source",
                              body,
                              extra_class="page-mj-overlay",
                              buckets=buckets)

    # Source root index (list all books)
    book_items = []
    for book_slug in sorted(files_by_book.keys()):
        abbrev = files_by_book[book_slug][0][0]
        n = len(files_by_book[book_slug])
        book_items.append(
            f'<li><a class="entry-card" href="{quote(book_slug)}/index.html">'
            f'<span class="entry-title">{html.escape(abbrev)}</span>'
            f'<span class="entry-meta">{n} fichier(s)</span></a></li>')
    src_body = (
        f'<nav class="mj-breadcrumb">'
        f'<a href="../index.html">Notes MJ</a> · '
        f'<span>Source</span>'
        f'</nav>'
        f'<h1>Source (canon Cubicle 7)</h1>'
        f'<p>Index des livres canon convertis en markdown. Pages générées en mode MJ.</p>'
        f'<ul class="card-grid card-grid-entries">{"".join(book_items)}</ul>'
    )
    src_index_rel = Path("source") / "index.html"
    src_index_dir = (MJ_OUT_DIR / src_index_rel).parent.relative_to(OUT)
    out[src_index_rel] = layout(src_index_dir, "Source — MJ",
                                src_body, extra_class="page-mj-overlay",
                                buckets=buckets)

    return out


# --------------------------------------------------------------------------- #
# Phase 3 — entity enrichment (PNJ / Lieux / Factions / Documents)
# --------------------------------------------------------------------------- #


@dataclass
class MJEntity:
    src_path: Path
    title: str               # stem of the Notes MJ file
    body_md: str
    src_folder: str          # public folder this entity targets ("PNJ", "Lieux", "Documents")
    out_url: str             # relative to MJ_OUT_DIR, e.g. "pnj/wasmeier.html"
    norm_key: str            # normalized stem for matching


# Notes MJ folder → public src_folder mapping
_MJ_ENTITY_FOLDERS = {
    "PNJ":       "PNJ",
    "Lieux":     "Lieux",
    "Factions":  "Lieux",     # factions live under "Lieux & Organisations"
    "Documents": "Documents",
}


# Manual aliases for MJ popovers: canonical (Notes MJ) → blog title.
# Used when the blog post title has an orthographic variant of the canonical
# Lexicanum/Fandom form that the fuzzy matcher doesn't auto-resolve (e.g. blog
# typo "Fernand" vs canon "Ferrand"). Each entry adds the canonical form as
# an alias that resolves to the same popover as the blog title.
#
# Mismatches sourced from `Notes MJ/Orthographe canon - corrections à
# appliquer.md` § 2. Workaround in effect until the blog posts are edited
# on Blogger (and the local blog mirror re-synced).
# OCR/spelling typos on the blog side, keyed by the canonical Notes MJ
# form. Used as a fallback when norm-key enrichment matching fails because
# the blog file is mis-spelled. Pure typos only — alternate naming
# conventions (title prefixes, translations, casing) live in
# `_GLOBAL_ENTITY_ALIASES` and feed the popover map via
# `build_entity_popover_map`.
_MJ_MANUAL_ALIASES = {
    "Immanuel-Ferrand Holswig-Schliestein": "Immanuel-Fernand Holswig-Schliestein",
    "Volkmar von Hindenstern":               "Votkmar von Hindenstern",
    "Detlef Sierck":                         "Detlef Sierek",
    "Wolfgang Holswig-Abenauer":             "Wolfgang Holswig-Abenhauer",
    "Yabo Chao":                             "Yobo Chao",
    "Altdorf":                               "Aldorf",
}


# Optional template metadata lines in Notes MJ entity bodies:
#   **Sous-titre** : short role line (5-7 words) shown under the title in popovers
#   **Portrait** : URL or path of a portrait image shown in popovers
# Both are extracted by `_extract_mj_entity_metadata` and fed to EntityPopover
# for MJ-only autonomous fiches.
_MJ_META_SUBTITLE_RE = re.compile(r'^\s*\*\*Sous-titre\*\*\s*:\s*(.+?)\s*$', re.MULTILINE)
_MJ_META_PORTRAIT_RE = re.compile(r'^\s*\*\*Portrait\*\*\s*:\s*(\S+)', re.MULTILINE)


def _extract_mj_entity_metadata(body_md: str) -> tuple[str | None, str | None]:
    """Pull optional **Sous-titre** / **Portrait** lines from a Notes MJ
    entity body. Returns (subtitle, portrait), both possibly None."""
    subtitle = None
    portrait = None
    m = _MJ_META_SUBTITLE_RE.search(body_md)
    if m:
        subtitle = m.group(1).strip()
    m = _MJ_META_PORTRAIT_RE.search(body_md)
    if m:
        portrait = m.group(1).strip()
    return subtitle, portrait


def _norm_entity_key(s: str) -> str:
    s = unicodedata.normalize("NFKD", s)
    s = "".join(c for c in s if not unicodedata.combining(c))
    s = re.sub(r"[^\w\s-]", "", s).strip().lower()
    s = re.sub(r"[\s_-]+", " ", s)
    return s


# Honorifics / titles to strip when computing match keys
_TITLE_WORDS = {
    "lieutenant", "capitaine", "captain", "colonel", "general", "general",
    "sergeant", "sergent", "father", "pere", "frere", "frère", "brother",
    "sister", "soeur", "sœur", "baron", "baronne", "comte", "comtesse",
    "count", "countess", "grand", "grande", "duc", "duchesse", "duke",
    "lord", "lady", "sir", "dame", "abbe", "abbé", "maitre", "maître",
    "master", "mistress", "miss", "monsieur", "madame", "doctor", "docteur",
    "professeur", "professor", "doktor",
}


def _norm_keys_for_match(s: str) -> set[str]:
    """Return possible normalized keys for fuzzy entity matching.
    Covers full normalized form + last/first word + variants without honorifics."""
    full = _norm_entity_key(s)
    keys = {full}
    words = full.split()
    if len(words) > 1:
        # Strip leading honorifics: "Lieutenant Erica Hauser" → "Erica Hauser"
        stripped = words
        while stripped and stripped[0] in _TITLE_WORDS:
            stripped = stripped[1:]
        if stripped:
            keys.add(" ".join(stripped))
            keys.add(stripped[-1])           # last word (likely surname)
            keys.add(stripped[0])            # first word (likely given name)
        keys.add(words[-1])
        keys.add(words[0])
    return {k for k in keys if k}


def _norm_match_keys_strict(s: str) -> set[str]:
    """Like `_norm_keys_for_match` but only returns multi-word keys (≥ 2 words).
    Used for entity enrichment matching where single-word keys ("wolfgang",
    "kellermann") generate false positives between unrelated PNJ that happen
    to share a given name or surname."""
    full = _norm_entity_key(s)
    keys = set()
    words = full.split()
    if len(words) > 1:
        keys.add(full)
        stripped = words
        while stripped and stripped[0] in _TITLE_WORDS:
            stripped = stripped[1:]
        if len(stripped) > 1:
            keys.add(" ".join(stripped))
    return {k for k in keys if k}


def _walk_mj_entities() -> list[MJEntity]:
    if not NOTES_MJ_DIR.exists() or not MJ_TOKEN:
        return []
    out: list[MJEntity] = []
    for src_folder, pub_folder in _MJ_ENTITY_FOLDERS.items():
        folder = NOTES_MJ_DIR / src_folder
        if not folder.exists():
            continue
        out_prefix = FOLDER_TO_OUT.get(pub_folder, pub_folder.lower())
        for f in sorted(folder.glob("*.md")):
            fm, body = _parse_frontmatter(f.read_text(encoding="utf-8"))
            out.append(MJEntity(
                src_path=f,
                title=f.stem,
                body_md=body,
                src_folder=pub_folder,
                out_url=f"{out_prefix}/{_mj_slug(f.stem)}.html",
                norm_key=_norm_entity_key(f.stem),
            ))
    return out


# Module-level caches, populated at start of build()
_MJ_ENTITIES_CACHE: list[MJEntity] | None = None
_MJ_ENRICHMENT_BY_PAGE: dict[Path, MJEntity] | None = None  # site_rel → entity (match)
_MJ_ONLY_ENTITIES: dict[str, list[MJEntity]] | None = None   # src_folder → MJ-only entities
_MJ_OVERLAY_IDX: dict[str, Path] | None = None              # wikilink → mj_out_rel path
_MJ_URL_MAP: dict[str, Path] | None = None                  # normalized entity name → public path
_MJ_LABEL_MAP: dict[str, Path] | None = None                # original label → public path (case-insensitive)
_MJ_POPOVER_MAP: dict[str, EntityPopover] | None = None     # entity name → popover for inject_entity_popovers


def _populate_mj_entity_caches(pages: list[Page],
                               url_map: dict[str, Path],
                               label_map: dict[str, Path],
                               popover_map: dict[str, EntityPopover] | None = None) -> None:
    """Match MJ entities to blog pages; cache results for use during render."""
    global _MJ_ENTITIES_CACHE, _MJ_ENRICHMENT_BY_PAGE, _MJ_ONLY_ENTITIES
    global _MJ_OVERLAY_IDX, _MJ_URL_MAP, _MJ_LABEL_MAP, _MJ_POPOVER_MAP
    if not MJ_TOKEN:
        _MJ_ENTITIES_CACHE = []
        _MJ_ENRICHMENT_BY_PAGE = {}
        _MJ_ONLY_ENTITIES = {}
        _MJ_OVERLAY_IDX = {}
        _MJ_URL_MAP = {}
        _MJ_LABEL_MAP = {}
        _MJ_POPOVER_MAP = {}
        return
    _MJ_POPOVER_MAP = dict(popover_map) if popover_map else {}
    # Build wikilink index from scenarios + thematic notes (Phase 1 overlay)
    mj_pages = _walk_notes_mj_overlay()
    _MJ_OVERLAY_IDX = _build_mj_wikilink_index(mj_pages)
    # Build a normalized name → public path index for wikilink resolution
    # (label_map is case-sensitive; we need permissive lookup for [[Gideon]],
    # [[gideon]], [[Karl-Heinz Wasmeier]], etc.)
    name_idx: dict[str, Path] = {}
    for pg in pages:
        if pg.variant_group and not pg.is_main:
            continue
        if not pg.post.folder:
            continue
        for k in _norm_keys_for_match(pg.post.title or ""):
            name_idx.setdefault(k, pg.site_rel)
        if pg.slug:
            for k in _norm_keys_for_match(pg.slug.replace("-", " ")):
                name_idx.setdefault(k, pg.site_rel)
    _MJ_URL_MAP = name_idx
    # Also keep label_map lowercased for fallback
    _MJ_LABEL_MAP = {k.lower(): v for k, v in label_map.items()}

    entities = _walk_mj_entities()
    _MJ_ENTITIES_CACHE = entities

    # Build {(src_folder, norm_key): list of pages} — multi-page bucket so all
    # variants of an entity (same title, different slugs) get enriched.
    # Strict keys (multi-word only) avoid false-positive matches where two
    # unrelated PNJ share a given name ("Wolfgang Kellermann" vs "Wolfgang
    # Holswig-Abenhauer") or a surname.
    page_idx: dict[tuple[str, str], list[Page]] = {}
    for pg in pages:
        if not pg.post.folder:
            continue
        keys = _norm_match_keys_strict(pg.post.title or "")
        if pg.slug:
            keys |= _norm_match_keys_strict(pg.slug.replace("-", " "))
        for k in keys:
            bucket = page_idx.setdefault((pg.post.folder, k), [])
            if pg not in bucket:
                bucket.append(pg)

    # Lookup table from canonical Notes MJ title → blog title (typo variant).
    # Used both for popover aliases (later) and to force enrichment matching
    # when the orthographic difference is too small for strict keys to bridge.
    blog_title_by_norm: dict[str, Page] = {}
    for pg in pages:
        if not pg.post.folder:
            continue
        norm = _norm_entity_key(pg.post.title or "")
        if norm:
            blog_title_by_norm[norm] = pg

    enrichment: dict[Path, MJEntity] = {}
    mj_only: dict[str, list[MJEntity]] = {}
    for e in entities:
        matches: list[Page] = []
        for k in _norm_match_keys_strict(e.title):
            for pg in page_idx.get((e.src_folder, k), []):
                if pg not in matches:
                    matches.append(pg)
        # Fallback chain when strict norm-key matching misses:
        #   1. `_MJ_MANUAL_ALIASES` covers OCR/spelling typos blog-side.
        #   2. `_GLOBAL_ENTITY_ALIASES` covers alternate naming conventions
        #      (title prefix, translation, casing). Same enrichment intent
        #      either way: route Notes MJ entity X onto blog page Y when
        #      titles diverge.
        if not matches:
            alias_target = (
                _MJ_MANUAL_ALIASES.get(e.title)
                or _GLOBAL_ENTITY_ALIASES.get(e.title)
            )
            if alias_target:
                pg = blog_title_by_norm.get(_norm_entity_key(alias_target))
                if pg is not None and pg.post.folder == e.src_folder:
                    matches.append(pg)
        if matches:
            for pg in matches:
                enrichment[pg.site_rel] = e
        else:
            mj_only.setdefault(e.src_folder, []).append(e)

    _MJ_ENRICHMENT_BY_PAGE = enrichment
    _MJ_ONLY_ENTITIES = mj_only

    # MJ-only entities (no public counterpart) also need popovers so that
    # mentions of "Henrik Kappelmuller", "Hermann von Feilbach" etc. light up
    # in MJ mode just like public entities do. The popover navigates to the
    # autonomous page under mj-{TOKEN}/<category>/<slug>.html.
    # Optional **Sous-titre** and **Portrait** metadata lines in the entity
    # body feed the popover's subtitle and image (template convention).
    for src_folder, ents in mj_only.items():
        cat = _ENTITY_POPOVER_CAT_LABEL.get(src_folder)
        if cat is None:
            continue
        for e in ents:
            if len(e.title) < 4:
                continue
            subtitle, portrait = _extract_mj_entity_metadata(e.body_md)
            popover = EntityPopover(
                site_rel=Path(f"mj-{MJ_TOKEN}") / e.out_url,
                anchor=None,
                title=e.title,
                cat=cat,
                portrait=portrait,
                subtitle=subtitle,
            )
            # Only register the FULL title — sub-tokens (first-name, surname)
            # generate false-positive matches on common words like "Chambre",
            # "Noire", "Ordo", "Grand" pulled from MJ-only titles like
            # "Chambre Noire", "Inner Council Ordo Septenarius", etc.
            # Mirror the public-side `build_entity_popover_map` behaviour.
            _MJ_POPOVER_MAP.setdefault(e.title, popover)

    # Apply manual aliases: canonical Notes MJ form → blog popover (typo fix).
    for canon_form, blog_title in _MJ_MANUAL_ALIASES.items():
        if blog_title in _MJ_POPOVER_MAP:
            _MJ_POPOVER_MAP.setdefault(canon_form, _MJ_POPOVER_MAP[blog_title])


def _render_mj_entity_body(e: MJEntity, current_out_rel: Path,
                           mj_idx: dict[str, Path] | None = None,
                           url_map: dict[str, Path] | None = None) -> str:
    """Render an MJ entity's markdown body to HTML, with wikilink resolution."""
    md_text = e.body_md
    if mj_idx is not None and url_map is not None:
        md_text = _resolve_mj_wikilinks(md_text, current_out_rel, mj_idx, url_map)
    return _md_to_html(md_text)


def _mj_enrichment_html_for_page(pg: Page) -> str:
    """Return the HTML block to inject at the bottom of a public entity page.
    Empty string if no MJ enrichment exists for this page."""
    if not MJ_TOKEN or _MJ_ENRICHMENT_BY_PAGE is None:
        return ""
    e = _MJ_ENRICHMENT_BY_PAGE.get(pg.site_rel)
    if e is None:
        return ""
    body_html = _render_mj_entity_body(
        e, Path(e.out_url),
        mj_idx=_MJ_OVERLAY_IDX,
        url_map=_MJ_URL_MAP)
    # Apply entity popover system to text-node entity mentions
    if _MJ_POPOVER_MAP:
        body_html = inject_entity_popovers(body_html, _MJ_POPOVER_MAP, pg.site_rel.parent)
    # MJ-only canon refs: <code>EiR Intro l.205-218</code> → hover popover + click navigation
    body_html = inject_canon_refs(body_html, pg.site_rel.parent)
    return (
        '<section class="mj-only mj-entity-enrichment">'
        '<hr class="mj-separator">'
        '<div class="mj-label">Notes MJ</div>'
        f'<div class="mj-content">{body_html}</div>'
        '</section>'
    )


def _mj_only_entities_for_category(src_folder: str) -> list[MJEntity]:
    """Return MJ-only entities (no public counterpart) for a given category."""
    if not MJ_TOKEN or _MJ_ONLY_ENTITIES is None:
        return []
    return _MJ_ONLY_ENTITIES.get(src_folder, [])


def _build_mj_search_index(mj_pages: list[MJOverlayPage]) -> list[dict]:
    out: list[dict] = []
    for p in mj_pages:
        if p.category in ("scenario_hub", "scenario_scene", "scenario_ref"):
            cat = f"Scénario · {p.scenario or ''}"
        elif p.category == "mj_note":
            cat = "Notes MJ"
        else:
            cat = "MJ"
        body_text = re.sub(r"<[^>]+>", " ", _md_to_html(p.body_md))
        body_text = re.sub(r"\s+", " ", body_text).strip()[:1000]
        haystack = (p.title or "") + " " + body_text + " " + cat
        out.append({
            "t": p.title,
            "c": cat,
            "u": f"mj-{MJ_TOKEN}/" + p.out_rel_path.as_posix(),
            "s": normalise_for_search(haystack),
            "mj": True,
        })

    # Phase 3: MJ-only entities (no public counterpart) — add to search
    if _MJ_ONLY_ENTITIES:
        for src_folder, entities in _MJ_ONLY_ENTITIES.items():
            cat_label = FOLDER_TO_LABEL.get(src_folder, src_folder)
            for e in entities:
                body_text = re.sub(r"<[^>]+>", " ", _md_to_html(e.body_md))
                body_text = re.sub(r"\s+", " ", body_text).strip()[:1000]
                haystack = e.title + " " + body_text + " " + cat_label
                out.append({
                    "t": e.title,
                    "c": cat_label,
                    "u": f"mj-{MJ_TOKEN}/" + e.out_url,
                    "s": normalise_for_search(haystack),
                    "mj": True,
                })
    return out


# ---------------------------------------------------------------------------
# Cartes interactives (MJ overlay) — schéma-graphe SVG cliquable.
# Data lives in Notes MJ/Cartes/<name>.json (GM-editable, version-controlled).
# Stage 1: zones + POI cliquables + sélecteur scénario + highlight + recherche.
# ---------------------------------------------------------------------------

CARTES_DIR = NOTES_MJ_DIR / "Cartes"


CARTE_CSS = """\
/* Cartes interactives — schéma-graphe SVG. Réutilise les variables du thème. */
/* Carte page = pleine largeur, sans la barre latérale des arcs (inutile ici). */
.page-carte .sidebar { display: none; }
.page-carte .layout { grid-template-columns: 1fr; max-width: 1500px; }
.page-carte main { max-width: none; padding: 1.4rem 2.2rem 4rem; }

.carte-app { margin: 1.2rem 0 2rem; }
.carte-toolbar {
  display: flex; flex-wrap: wrap; align-items: flex-end; gap: 0.9rem 1.2rem;
  padding: 0.7rem 0.9rem; margin-bottom: 0.9rem;
  background: var(--parchment); border: 1px solid var(--rule);
  border-radius: 8px;
}
.carte-field { display: flex; flex-direction: column; gap: 0.2rem;
  font-family: var(--serif-display); font-size: 0.78rem;
  letter-spacing: 0.04em; text-transform: uppercase; color: var(--muted); }
.carte-field select, .carte-field input {
  font-family: var(--serif-body); font-size: 0.98rem; color: var(--ink);
  background: var(--paper); border: 1px solid var(--rule);
  border-radius: 5px; padding: 0.3rem 0.5rem; min-width: 13rem;
}
.carte-field select:focus, .carte-field input:focus {
  outline: none; border-color: var(--gold); }
.carte-search-field input { min-width: 11rem; }
.carte-toggle { display: flex; align-items: center; gap: 0.35rem;
  font-family: var(--serif-body); font-size: 0.9rem; color: var(--ink-soft);
  cursor: pointer; padding-bottom: 0.3rem; }
.carte-btn { font-family: var(--serif-display); font-size: 0.85rem;
  background: var(--paper); color: var(--ink); border: 1px solid var(--rule);
  border-radius: 5px; padding: 0.3rem 0.7rem; cursor: pointer; }
.carte-btn:hover { border-color: var(--gold); color: var(--oxblood); }
.carte-btn.active { background: var(--oxblood); color: var(--paper-hi); border-color: var(--oxblood); }
.carte-route { fill: none; stroke: var(--oxblood); stroke-width: 3.5; opacity: 0.85;
  stroke-dasharray: 9 6; stroke-linecap: round; stroke-linejoin: round; }
.carte-route-pt { fill: var(--oxblood); stroke: var(--paper-hi); stroke-width: 2; }
#carte-svg.route-mode { cursor: crosshair; }
.carte-hub-link { margin-left: auto; align-self: center;
  font-family: var(--serif-display); color: var(--oxblood);
  text-decoration: none; border-bottom: 1px solid var(--gold);
  padding-bottom: 1px; }
.carte-hub-link:hover { color: var(--oxblood-hi); }

.carte-filters { display: flex; flex-wrap: wrap; align-items: center; gap: 0.3rem 0.4rem;
  margin: 0 0 0.8rem; }
.carte-live { display: flex; flex-wrap: wrap; align-items: center; gap: 0.3rem 0.5rem;
  margin: 0 0 0.8rem; padding: 0.4rem 0.6rem; border: 1px solid var(--gold);
  border-radius: 8px; background: var(--parchment); }
.carte-live .carte-live-hour-field select { min-width: 12rem; }
.carte-filter-lbl { font-family: var(--serif-display); font-size: 0.74rem;
  text-transform: uppercase; letter-spacing: 0.05em; color: var(--muted);
  margin: 0 0.2rem 0 0.6rem; }
.carte-chip { font-family: var(--serif-body); font-size: 0.82rem; cursor: pointer;
  background: var(--paper); color: var(--muted); border: 1px solid var(--rule);
  border-radius: 999px; padding: 0.12rem 0.6rem; opacity: 0.55; }
.carte-chip.active { opacity: 1; color: var(--ink); border-color: var(--gold);
  background: var(--parchment); }
.carte-chip:hover { border-color: var(--oxblood); }
/* single source of truth for the 10 location-type colours (used by both the
   map markers and the filter-chip swatches) */
:root {
  --t-religieux: #eae3cf; --t-magie: #7c5aa6; --t-gouvernement: #3f6fa8;
  --t-militaire: #9c3434; --t-noble: #c9a227; --t-commerce: #d2762a;
  --t-taverne: #8a5a2b; --t-crime: #2f2a27; --t-mort: #61716a; --t-autre: #9a8f7a;
}
/* type chips carry a colour swatch matching their map markers */
.carte-chip[data-type]::before { content: ''; display: inline-block; width: 9px; height: 9px;
  border-radius: 50%; margin-right: 5px; vertical-align: middle;
  border: 1px solid rgba(40,28,18,0.45); background: var(--t-autre); }
.carte-chip[data-type="religieux"]::before   { background: var(--t-religieux); }
.carte-chip[data-type="magie"]::before        { background: var(--t-magie); }
.carte-chip[data-type="gouvernement"]::before { background: var(--t-gouvernement); }
.carte-chip[data-type="militaire"]::before    { background: var(--t-militaire); }
.carte-chip[data-type="noble"]::before        { background: var(--t-noble); }
.carte-chip[data-type="commerce"]::before     { background: var(--t-commerce); }
.carte-chip[data-type="taverne"]::before      { background: var(--t-taverne); }
.carte-chip[data-type="crime"]::before        { background: var(--t-crime); }
.carte-chip[data-type="mort"]::before         { background: var(--t-mort); }
.carte-chip[data-type="autre"]::before        { background: var(--t-autre); }
.carte-poi.filtered-out { display: none; }
.carte-stage { display: grid; grid-template-columns: 1fr 290px; gap: 1rem;
  align-items: start; }
.carte-svg-wrap { border: 1px solid var(--rule); border-radius: 8px;
  background: var(--paper-hi); overflow: hidden;
  box-shadow: var(--shadow); }
#carte-svg { display: block; width: 100%; height: auto; }

.carte-panel { border: 1px solid var(--rule); border-radius: 8px;
  background: var(--parchment); padding: 0.9rem 1rem; min-height: 8rem;
  font-family: var(--serif-body); position: sticky; top: calc(var(--header-h) + 12px); }
.carte-panel-empty { color: var(--muted); font-style: italic; margin: 0; }
.carte-panel h3 { font-family: var(--serif-display); margin: 0 0 0.15rem;
  color: var(--ink); font-size: 1.25rem; }
.carte-panel .carte-zone-tag { display: inline-block; font-size: 0.72rem;
  text-transform: uppercase; letter-spacing: 0.05em; color: var(--muted);
  margin-bottom: 0.5rem; }
.carte-panel .carte-quarter-tag { font-family: var(--serif-display); font-size: 0.82rem;
  color: var(--gold); margin: 0.15rem 0 0.2rem; }
.carte-panel .carte-desc { color: var(--ink-soft); margin: 0.3rem 0 0.7rem;
  font-size: 0.96rem; line-height: 1.45; }
.carte-panel .carte-fiche-link { font-family: var(--serif-display);
  color: var(--oxblood); text-decoration: none;
  border-bottom: 1px solid var(--gold); }
.carte-panel .carte-fiche-link:hover { color: var(--oxblood-hi); }
.carte-panel h4 { font-family: var(--serif-display); font-size: 0.82rem;
  text-transform: uppercase; letter-spacing: 0.05em; color: var(--muted);
  margin: 1rem 0 0.3rem; border-top: 1px solid var(--rule-soft);
  padding-top: 0.6rem; }
.carte-panel ul.carte-scenes { list-style: none; margin: 0; padding: 0; }
.carte-panel ul.carte-scenes li { margin: 0.25rem 0; }
.carte-panel ul.carte-scenes a { color: var(--ink); text-decoration: none;
  border-bottom: 1px dotted var(--rule); font-size: 0.93rem; }
.carte-panel ul.carte-scenes a:hover { color: var(--oxblood);
  border-bottom-color: var(--gold); }
.carte-panel .carte-scen-name { color: var(--gold); font-style: italic;
  font-size: 0.8rem; }
.carte-panel .carte-poi-list { list-style: none; margin: 0; padding: 0; }
.carte-panel .carte-poi-list li a { color: var(--ink); cursor: pointer;
  text-decoration: none; border-bottom: 1px dotted var(--rule); }

/* SVG map styling (poster underlay + canon-positioned markers) */
#carte-svg { cursor: grab; touch-action: none; background: var(--paper-hi);
  -webkit-user-select: none; user-select: none; }
#carte-svg.grabbing { cursor: grabbing; }
.carte-dist-label { font-family: var(--serif-display); fill: var(--ink-soft);
  letter-spacing: 0.04em; pointer-events: auto; cursor: pointer; opacity: 0.8;
  font-style: italic; paint-order: stroke; stroke: var(--paper-hi);
  stroke-width: 3px; stroke-linejoin: round; }
.carte-dist-label:hover { fill: var(--oxblood); opacity: 1; }
.carte-zone { opacity: 0.30; cursor: pointer; stroke: rgba(40,28,18,0.28);
  stroke-width: 0.6; transition: opacity 0.12s; }
.carte-zone:hover { opacity: 0.48; }
/* "Couleur" off = tint hidden but cells stay clickable (invisible, hit-testable) */
#carte-svg.hide-zones .carte-zone { opacity: 0; stroke: none; }
#carte-svg.hide-zones .carte-zone:hover { opacity: 0.18; }
.carte-river-band { fill: #6f93a6; opacity: 0.55; pointer-events: none; }
#carte-svg.hide-zones .carte-river-band { opacity: 0; }
.carte-seclegend { display: flex; flex-wrap: wrap; align-items: center; gap: 0.35rem;
  margin: 0.15rem 0 0.35rem; }
.carte-seclegend-lbl { font-size: 0.8rem; opacity: 0.7; margin-right: 0.15rem; }
.carte-seclegend-chip { display: inline-flex; align-items: center; gap: 0.35rem;
  cursor: pointer; border: 1px solid rgba(163,122,46,0.45); border-radius: 0.5rem;
  padding: 0.12rem 0.55rem; font-size: 0.84rem; background: rgba(255,255,255,0.04);
  color: inherit; font-family: inherit; }
.carte-seclegend-chip:hover { background: rgba(163,122,46,0.22); }
.carte-seclegend-sw { width: 13px; height: 13px; border-radius: 3px;
  border: 1px solid rgba(0,0,0,0.35); display: inline-block; }
.carte-zone.sec-flash { stroke: #fff; stroke-width: 2.5; }
.carte-legend-zone { opacity: 0.30; cursor: pointer; transition: opacity 0.12s; }
.carte-legend-zone:hover { opacity: 0.5; }
#carte-svg.hide-zones .carte-legend-zone { opacity: 0; }
#carte-svg.hide-zones .carte-legend-zone:hover { opacity: 0.18; }
.carte-legend-hit { fill: transparent; cursor: pointer; }
.carte-legend-hit:hover { fill: rgba(163,122,46,0.34); }
.carte-poi { cursor: pointer; }
.carte-poi-dot { fill: var(--paper); stroke: var(--oxblood);
  transition: fill 0.12s, stroke 0.12s; }
/* marker fill by location type (colours single-sourced from :root --t-*; interaction states below override) */
.carte-poi[data-type="religieux"]   .carte-poi-dot { fill: var(--t-religieux); }
.carte-poi[data-type="magie"]       .carte-poi-dot { fill: var(--t-magie); }
.carte-poi[data-type="gouvernement"] .carte-poi-dot { fill: var(--t-gouvernement); }
.carte-poi[data-type="militaire"]   .carte-poi-dot { fill: var(--t-militaire); }
.carte-poi[data-type="noble"]       .carte-poi-dot { fill: var(--t-noble); }
.carte-poi[data-type="commerce"]    .carte-poi-dot { fill: var(--t-commerce); }
.carte-poi[data-type="taverne"]     .carte-poi-dot { fill: var(--t-taverne); }
.carte-poi[data-type="crime"]       .carte-poi-dot { fill: var(--t-crime); }
.carte-poi[data-type="mort"]        .carte-poi-dot { fill: var(--t-mort); }
.carte-poi[data-type="autre"]       .carte-poi-dot { fill: var(--t-autre); }
/* per-type icon glyph drawn over the coloured pill (white + ink halo) */
.carte-poi-glyph { fill: #fbf7ec; stroke: rgba(28,18,10,0.9); stroke-linejoin: round;
  paint-order: stroke; pointer-events: none; }
.carte-poi-label { font-family: var(--serif-body); fill: var(--ink);
  paint-order: stroke; stroke: var(--paper-hi); stroke-linejoin: round;
  pointer-events: none; opacity: 0; transition: opacity 0.1s; }
.carte-poi:hover .carte-poi-dot { fill: var(--gold-hi); }
.carte-poi:hover .carte-poi-label,
.carte-poi.selected .carte-poi-label,
.carte-poi.has-scenario .carte-poi-label,
.carte-poi.search-hit .carte-poi-label,
#carte-svg.show-names .carte-poi.lbl-on .carte-poi-label { opacity: 1; }
.carte-poi.selected .carte-poi-dot { fill: var(--oxblood); stroke: var(--ink); }
.carte-poi.has-scenario .carte-poi-dot { fill: var(--gold); stroke: var(--oxblood); }
.carte-poi.search-hit .carte-poi-dot { fill: var(--oxblood-hi); stroke: var(--ink); }
.carte-poi.dim { opacity: 0.16; }
.carte-poi.dim .carte-poi-label { opacity: 0 !important; }
/* scenario-linked POIs (a scene is tied to them): pulsing gold ring + bold label */
.carte-poi-ring { fill: none; stroke: var(--gold-hi); opacity: 0; pointer-events: none; }
.carte-poi.has-scenario .carte-poi-ring { opacity: 0.95; animation: carte-pulse 1.8s ease-in-out infinite; }
.carte-poi.has-scenario .carte-poi-label { fill: var(--oxblood); font-weight: 700; }
@keyframes carte-pulse { 0%,100% { opacity: 0.95; } 50% { opacity: 0.3; } }
@media (prefers-reduced-motion: reduce) { .carte-poi.has-scenario .carte-poi-ring { animation: none; } }

/* in-poster legend panel (over a blank cartouche) */
.carte-legendbox-bg { fill: rgba(248,242,228,0.88); stroke: var(--oxblood); stroke-width: 1.3; }
.carte-legendbox-title { font-family: var(--serif-body); fill: var(--oxblood);
  font-size: 17px; font-weight: 700; letter-spacing: 0.02em; }
.carte-legendbox-h { font-family: var(--serif-body); fill: var(--ink); font-size: 11px;
  font-weight: 700; letter-spacing: 0.08em; opacity: 0.72; text-transform: uppercase; }
.carte-legendbox-t { font-family: var(--serif-body); fill: var(--ink); font-size: 12.5px; }
.carte-legendbox-glyph { fill: #fbf7ec; stroke: rgba(28,18,10,0.9); stroke-width: 1.2;
  stroke-linejoin: round; pointer-events: none; }
g.carte-legendbox-row[tabindex] { cursor: pointer; }
g.carte-legendbox-row[tabindex]:hover .carte-legendbox-t { fill: var(--oxblood); font-weight: 700; }

@media (max-width: 720px) {
  .carte-stage { grid-template-columns: 1fr; }
  .carte-panel { position: static; }
}
"""


CARTE_JS = """\
// Carte interactive (Étape A): poster canon en fond + zoom/pan,
// marqueurs aux coordonnées canon, clic POI -> panneau, scénario -> highlight, recherche.
(function () {
  var dataEl = document.getElementById('carte-data');
  var svg = document.getElementById('carte-svg');
  if (!dataEl || !svg) return;
  var M = JSON.parse(dataEl.textContent);
  if (M.title) svg.setAttribute('aria-label', 'Carte schématique ' + (/^[aeiouhàâéèêAEIOUH]/.test(M.title) ? "d'" : 'de ') + M.title);
  var SVGNS = 'http://www.w3.org/2000/svg';
  var poiById = {}, poiNodes = {};
  M.pois.forEach(function (p) { poiById[p.id] = p; });
  var routeMode = false, route = [];   // pathfinding (#6): trajet + quartiers traversés

  function el(name, attrs) {
    var n = document.createElementNS(SVGNS, name);
    for (var k in attrs) { if (attrs[k] != null) n.setAttribute(k, attrs[k]); }
    return n;
  }
  function esc(s) { var d = document.createElement('div'); d.textContent = s == null ? '' : s; return d.innerHTML; }
  function norm(s) { return (s || '').toLowerCase().normalize('NFD').replace(/[\\u0300-\\u036f]/g, ''); }

  // --- coordinate space = canon PDF points ---
  var VB = (M.viewBox || '0 0 1247 794').split(/\\s+/).map(Number);
  var W0 = VB[2], H0 = VB[3];
  var view = { x: 0, y: 0, w: W0, h: H0 };   // current viewBox (pan/zoom)
  var lastR = null;
  function applyView() {
    svg.setAttribute('viewBox', view.x + ' ' + view.y + ' ' + view.w + ' ' + view.h);
    var r = view.w / W0;                       // <1 when zoomed in
    if (r === lastR) return;                   // pan: viewBox moved but scale unchanged → skip resize/declutter
    lastR = r;
    svg.style.setProperty('--mk', r);          // marker scale ratio
    updateSizes(r);                            // (calls declutter) — only on real zoom
  }

  // layers (drawn in canon coordinates; the viewBox does the zoom/pan)
  // order = paint/hit order: poster < zones < district labels < pois < legend hotspots
  var gPoster = el('g'), gZones = el('g'), gRivers = el('g'), gRoute = el('g'), gDist = el('g'),
      gPois = el('g'), gLegendZones = el('g'), gLegend = el('g'), gLegendBox = el('g');
  svg.appendChild(gPoster); svg.appendChild(gZones); svg.appendChild(gRivers); svg.appendChild(gRoute);
  svg.appendChild(gDist); svg.appendChild(gPois); svg.appendChild(gLegendZones); svg.appendChild(gLegend);
  svg.appendChild(gLegendBox);
  // river band drawn OVER the zones → the colored sections are visibly split by the water
  (M.rivers || []).forEach(function (poly) {
    var pts = poly.map(function (p) { return p[0] + ',' + p[1]; }).join(' ');
    gRivers.appendChild(el('polygon', { points: pts, 'class': 'carte-river-band' }));
  });
  svg.setAttribute('viewBox', '0 0 ' + W0 + ' ' + H0);

  // poster underlay
  if (M.posterUrl) {
    var img = el('image', { x: 0, y: 0, width: W0, height: H0, href: M.posterUrl,
      preserveAspectRatio: 'none', opacity: (M.posterOpacity != null ? M.posterOpacity : 0.55) });
    img.setAttributeNS('http://www.w3.org/1999/xlink', 'href', M.posterUrl);
    gPoster.appendChild(img);
  }

  // --- quarters (Voronoï seeds) = district anchors + quarter-POIs ---
  var seeds = [];
  (M.districts || []).forEach(function (dz) {
    seeds.push({ x: dz.x, y: dz.y, kind: 'district', ref: dz, name: dz.name }); });
  M.pois.forEach(function (p) {
    if (p.seed) seeds.push({ x: p.x, y: p.y, kind: 'poi', ref: p, name: p.name }); });
  var seedByName = {}; seeds.forEach(function (s) { seedByName[s.name] = s; });
  // Membership is CANON (p.section / quarter tags), never distance-based.
  // The Voronoï below is used ONLY to draw the section regions.

  // district orientation labels (clickable → show quarter)
  var distLabels = [];
  (M.districts || []).forEach(function (dz) {
    var t = el('text', { x: dz.x, y: dz.y, 'class': 'carte-dist-label',
      'text-anchor': 'middle', tabindex: 0 });
    t.textContent = dz.name;
    t.addEventListener('click', function () { showQuarter(seedByName[dz.name]); });
    t.addEventListener('keydown', function (ev) {
      if (ev.key === 'Enter' || ev.key === ' ') { ev.preventDefault(); showQuarter(seedByName[dz.name]); } });
    gDist.appendChild(t); distLabels.push(t);
  });

  // --- zones: Voronoï cells (geometry only) coloured by CANON section ---
  // Clip box excludes the printed legend panel on the left.
  var ZX = 236;
  // Section colours are data-driven: explicit `color` from each section fiche,
  // else auto-assigned from a palette (so a new map's sections are coloured too).
  var SECTION_COLOR = {};
  (M.sections || []).forEach(function (s) { if (s.color) SECTION_COLOR[s.key] = s.color; });
  var SECTION_PALETTE = ['#9a7a2e', '#a8483f', '#4f7a55', '#3f6fa8', '#7c5aa6', '#8a5a2b', '#5b6b78'];
  var palIdx = 0;
  (M.quarterPolygons || []).forEach(function (q) {
    if (!SECTION_COLOR[q.section]) SECTION_COLOR[q.section] = SECTION_PALETTE[palIdx++ % SECTION_PALETTE.length];
  });
  function clipHP(poly, A, B) {  // keep the half-plane of points closer to A than B
    var dx = B.x - A.x, dy = B.y - A.y, mx = (A.x + B.x) / 2, my = (A.y + B.y) / 2;
    function f(p) { return (p[0] - mx) * dx + (p[1] - my) * dy; }
    function inter(p, q) { var a = f(p), b = f(q), t = a / (a - b);
      return [p[0] + t * (q[0] - p[0]), p[1] + t * (q[1] - p[1])]; }
    var out = [];
    for (var i = 0; i < poly.length; i++) {
      var cur = poly[i], prv = poly[(i + poly.length - 1) % poly.length];
      var ci = f(cur) <= 0, pi = f(prv) <= 0;
      if (ci) { if (!pi) out.push(inter(prv, cur)); out.push(cur); }
      else if (pi) { out.push(inter(prv, cur)); }
    }
    return out;
  }
  // Zones are clipped to the canon city outline (inside the walls) so they don't
  // spill into the countryside. Starting the Voronoï subject as the (concave)
  // city polygon is correct because each clipHP is a convex half-plane cut.
  function qnorm(s) { return norm(s).replace(/ß/g, 'ss').replace(/[^a-z0-9]/g, ''); }
  var seedByNorm = {}; seeds.forEach(function (s) { seedByNorm[qnorm(s.name)] = s; });
  // Click a quarter by canon name → its fiche if one exists, else a minimal panel.
  function showQuarterByName(name) {
    var s = seedByNorm[qnorm(name)];
    if (s) { showQuarter(s); return; }
    var qp = (M.quarterPolygons || []).find(function (q) { return qnorm(q.name) === qnorm(name); });
    showQuarter({ kind: 'district', ref: { name: name, section: qp ? qp.section : '' } });
  }

  if (M.quarterPolygons && M.quarterPolygons.length) {
    // Real quarter contours traced from the canon boundary map (dashed-line
    // watershed, barriers = walls + water). Coloured by canon section.
    var NQ = M.quarterPolygons.length;
    // Distinct per-quarter hue (golden-angle spread) for the "par quartier" mode.
    function quarterColor(i) { return 'hsl(' + ((i * 137.508) % 360).toFixed(1) + ', 42%, 52%)'; }
    M.quarterPolygons.forEach(function (q, i) {
      var pts = q.poly.map(function (p) { return p[0] + ',' + p[1]; }).join(' ');
      var secColor = SECTION_COLOR[q.section] || '#8a7a5a';
      var pg = el('polygon', { points: pts, 'class': 'carte-zone', 'data-section': q.section,
        fill: secColor });
      pg.setAttribute('data-sec-color', secColor);
      pg.setAttribute('data-quart-color', quarterColor(i));
      var ti = el('title'); ti.textContent = q.name; pg.appendChild(ti);
      pg.addEventListener('click', function () { showQuarterByName(q.name); });
      gZones.appendChild(pg);
    });
  } else {
    // Fallback: Voronoï cells clipped to the city outline (geometry only).
    var CITY = (M.cityPolygon && M.cityPolygon.length >= 3)
      ? M.cityPolygon.map(function (p) { return [p[0], p[1]]; })
      : [[ZX, 0], [W0, 0], [W0, H0], [ZX, H0]];
    seeds.forEach(function (s, i) {
      var poly = CITY.map(function (p) { return [p[0], p[1]]; });
      for (var j = 0; j < seeds.length && poly.length >= 3; j++) {
        if (j !== i) poly = clipHP(poly, { x: s.x, y: s.y }, { x: seeds[j].x, y: seeds[j].y });
      }
      if (poly.length < 3) return;
      var pts = poly.map(function (p) { return p[0].toFixed(1) + ',' + p[1].toFixed(1); }).join(' ');
      var sec = s.ref.section;
      var pg = el('polygon', { points: pts, 'class': 'carte-zone', 'data-section': sec,
        fill: SECTION_COLOR[sec] || '#8a7a5a' });
      var ti = el('title'); ti.textContent = s.name; pg.appendChild(ti);
      pg.addEventListener('click', function () { showQuarter(s); });
      gZones.appendChild(pg);
    });
  }

  // --- colour the printed legend by section + make each section block clickable ---
  (M.legendSections || []).forEach(function (ls) {
    var r = el('rect', { x: ls.x, y: ls.y, width: ls.w, height: ls.h,
      'class': 'carte-legend-zone', 'data-section': ls.key,
      fill: SECTION_COLOR[ls.key] || '#8a7a5a' });
    var ti = el('title'); ti.textContent = ls.label; r.appendChild(ti);
    r.addEventListener('click', function () { showSection(ls.key); });
    gLegendZones.appendChild(r);
  });

  // --- clickable hotspots over the poster's printed legend (1-25) ---
  (M.legend || []).forEach(function (e) {
    if (!e.poi) return;
    var r = el('rect', { x: e.x, y: e.y, width: e.w, height: Math.min(e.h || 12, 12),
      'class': 'carte-legend-hit' });
    var ti = el('title'); ti.textContent = (poiById[e.poi] || {}).name || ''; r.appendChild(ti);
    r.addEventListener('click', function (ev) {
      ev.stopPropagation();
      var p = poiById[e.poi]; if (!p) return;
      selectPoi(e.poi); focusOn(p.x, p.y, W0 * 0.32);
    });
    gLegend.appendChild(r);
  });

  // per-type icon glyphs (centred at origin in a ±6 box, scaled with zoom)
  var TYPE_GLYPH = {
    religieux:    'M-1.3,-6 H1.3 V-2 H5 V0.6 H1.3 V6 H-1.3 V0.6 H-5 V-2 H-1.3 Z',
    magie:        'M0,-6 L1.5,-1.6 L6,-1.6 L2.4,1.2 L3.8,5.6 L0,2.9 L-3.8,5.6 L-2.4,1.2 L-6,-1.6 L-1.5,-1.6 Z',
    gouvernement: 'M-5,-4 H5 V-2 H-5 Z M-3.4,-2 H-1.7 V3 H-3.4 Z M-0.85,-2 H0.85 V3 H-0.85 Z M1.7,-2 H3.4 V3 H1.7 Z M-5,3 H5 V5 H-5 Z',
    militaire:    'M0,-6 L5,-3.5 V0.5 Q5,4.6 0,6 Q-5,4.6 -5,0.5 V-3.5 Z',
    noble:        'M-6,4 V-3 L-2.5,0.6 L0,-4.6 L2.5,0.6 L6,-3 V4 Z',
    commerce:     'M0,-6 L5.6,0 L0,6 L-5.6,0 Z',
    taverne:      'M-4.6,-4 H2 V5 H-4.6 Z M2,-2 Q5.6,-2 5.6,1 Q5.6,4 2,4 V2.2 Q3.3,2.2 3.3,1 Q3.3,-0.2 2,-0.2 Z',
    crime:        'M0,6 L-2.3,-1 H2.3 Z M-4,-1 H4 V-2.5 H-4 Z M-1,-5.6 H1 V-2.5 H-1 Z',
    mort:         'M-4,6 V-1.6 Q-4,-6 0,-6 Q4,-6 4,-1.6 V6 Z',
    autre:        'M0,-3.3 A3.3,3.3 0 1,0 0.01,-3.3 Z'
  };

  // --- in-poster legend panel drawn over a blank cartouche, if configured ---
  // M.legendBox = {x,y,w,h} in viewBox coords. Sits on the parchment (pans/zooms
  // with the map). Lists the section colour key (clickable → showSection) + the
  // type-icon key. Generic: maps without legendBox render nothing here.
  if (M.legendBox && (M.legendBox.w || 0) > 0) {
    var LB = M.legendBox, pad = 14, cy = LB.y + pad + 4;
    gLegendBox.appendChild(el('rect', { x: LB.x, y: LB.y, width: LB.w, height: LB.h,
      rx: 7, 'class': 'carte-legendbox-bg' }));
    var ttl = el('text', { x: LB.x + LB.w / 2, y: cy + 12, 'text-anchor': 'middle',
      'class': 'carte-legendbox-title' });
    ttl.textContent = M.title || 'Légende'; gLegendBox.appendChild(ttl);
    cy += 30;
    if ((M.sections || []).length) {
      var sh = el('text', { x: LB.x + pad, y: cy, 'class': 'carte-legendbox-h' });
      sh.textContent = 'Secteurs'; gLegendBox.appendChild(sh); cy += 17;
      M.sections.forEach(function (s) {
        var row = el('g', { 'class': 'carte-legendbox-row', tabindex: 0 });
        row.appendChild(el('rect', { x: LB.x + pad, y: cy - 9, width: 12, height: 12, rx: 2,
          fill: SECTION_COLOR[s.key] || '#8a7a5a', stroke: 'rgba(28,18,10,0.6)', 'stroke-width': 0.6 }));
        var t = el('text', { x: LB.x + pad + 19, y: cy, 'class': 'carte-legendbox-t' });
        t.textContent = s.label; row.appendChild(t);
        row.addEventListener('click', function () { showSection(s.key); });
        row.addEventListener('keydown', function (ev) {
          if (ev.key === 'Enter' || ev.key === ' ') { ev.preventDefault(); showSection(s.key); } });
        gLegendBox.appendChild(row); cy += 17;
      });
      cy += 8;
    }
    if ((M.types || []).length) {
      var th = el('text', { x: LB.x + pad, y: cy, 'class': 'carte-legendbox-h' });
      th.textContent = 'Types de lieux'; gLegendBox.appendChild(th); cy += 17;
      M.types.forEach(function (ty) {
        var row = el('g', { 'class': 'carte-legendbox-row' });
        row.appendChild(el('circle', { cx: LB.x + pad + 6, cy: cy - 4, r: 6,
          fill: 'var(--t-' + ty.key + ')', stroke: 'rgba(28,18,10,0.6)', 'stroke-width': 0.6 }));
        row.appendChild(el('path', { d: TYPE_GLYPH[ty.key] || TYPE_GLYPH.autre,
          'class': 'carte-legendbox-glyph',
          transform: 'translate(' + (LB.x + pad + 6) + ',' + (cy - 4) + ') scale(0.7)' }));
        var t = el('text', { x: LB.x + pad + 19, y: cy, 'class': 'carte-legendbox-t' });
        t.textContent = ty.label; row.appendChild(t);
        gLegendBox.appendChild(row); cy += 15.5;
      });
    }
  }

  // POI markers
  var dots = [], labels = [], hits = [], glyphs = [], rings = [];
  M.pois.forEach(function (p) {
    var g = el('g', { 'class': 'carte-poi', 'data-id': p.id, 'data-type': p.type || 'autre', tabindex: 0 });
    var hit = el('circle', { cx: p.x, cy: p.y, r: 14, fill: 'transparent', 'class': 'carte-poi-hit' });
    var ring = el('circle', { cx: p.x, cy: p.y, r: 11, 'class': 'carte-poi-ring' });
    var dot = el('circle', { cx: p.x, cy: p.y, r: 6, 'class': 'carte-poi-dot' });
    var gl = el('path', { d: TYPE_GLYPH[p.type] || TYPE_GLYPH.autre, 'class': 'carte-poi-glyph' });
    var lab = el('text', { x: p.x + 9, y: p.y - 8, 'class': 'carte-poi-label' });
    lab.textContent = p.name;
    g.appendChild(hit); g.appendChild(ring); g.appendChild(dot); g.appendChild(gl); g.appendChild(lab);
    g.addEventListener('click', function () {
      if (routeMode) { route.push(p.id); drawRoute(); } else selectPoi(p.id); });
    g.addEventListener('keydown', function (ev) {
      if (ev.key === 'Enter' || ev.key === ' ') { ev.preventDefault();
        if (routeMode) { route.push(p.id); drawRoute(); } else selectPoi(p.id); } });
    gPois.appendChild(g);
    poiNodes[p.id] = g; dots.push(dot); labels.push(lab); hits.push(hit); glyphs.push(gl); rings.push(ring);
  });

  // keep markers at constant screen size as we zoom (r,font scale with viewBox)
  function updateSizes(r) {
    for (var i = 0; i < dots.length; i++) {
      var gp = poiById[M.pois[i].id];
      dots[i].setAttribute('r', (6 * r).toFixed(2));
      dots[i].setAttribute('stroke-width', (2.2 * r).toFixed(2));
      hits[i].setAttribute('r', (15 * r).toFixed(2));
      labels[i].setAttribute('font-size', (13 * r).toFixed(2));
      labels[i].setAttribute('x', (gp.x + 9 * r).toFixed(2));
      labels[i].setAttribute('y', (gp.y - 8 * r).toFixed(2));
      labels[i].setAttribute('stroke-width', (3 * r).toFixed(2));
      glyphs[i].setAttribute('transform', 'translate(' + gp.x + ',' + gp.y + ') scale(' + (r * 0.8).toFixed(3) + ')');
      glyphs[i].setAttribute('stroke-width', '1.1');
      rings[i].setAttribute('r', (11 * r).toFixed(2));
      rings[i].setAttribute('stroke-width', (2 * r).toFixed(2));
    }
    for (var j = 0; j < distLabels.length; j++)
      distLabels[j].setAttribute('font-size', (15 * r).toFixed(2));
    declutter();
  }

  // Greedy label declutter: show names in priority order (Notable first),
  // hiding any whose box overlaps one already shown. Recomputed on zoom, so
  // more names surface as you zoom in. Only runs when "Noms" is active.
  var poiOrder = M.pois.slice().sort(function (a, b) {
    return ((a.importance === 'Mineur') ? 1 : 0) - ((b.importance === 'Mineur') ? 1 : 0);
  });
  function declutter() {
    if (!svg.classList.contains('show-names')) return;
    var r = view.w / W0, placed = [];
    for (var k = 0; k < poiOrder.length; k++) {
      var p = poiOrder[k], node = poiNodes[p.id];
      if (node.classList.contains('filtered-out')) { node.classList.remove('lbl-on'); continue; }
      var fs = 13 * r, x0 = p.x + 9 * r, w = (p.name.length || 1) * fs * 0.52;
      var rc = [x0, p.y - 8 * r - fs, x0 + w, p.y - 8 * r + 2 * r], hit = false;
      for (var m = 0; m < placed.length; m++) {
        var q = placed[m];
        if (!(rc[2] < q[0] || rc[0] > q[2] || rc[3] < q[1] || rc[1] > q[3])) { hit = true; break; }
      }
      if (hit) node.classList.remove('lbl-on');
      else { node.classList.add('lbl-on'); placed.push(rc); }
    }
  }

  // ---- pan / zoom (manipulate the viewBox) ----
  function clampView() {
    var margin = 0.25;
    view.w = Math.min(view.w, W0 * (1 + margin));
    view.h = view.w * (H0 / W0);
    view.x = Math.max(-W0 * margin, Math.min(view.x, W0 - view.w + W0 * margin));
    view.y = Math.max(-H0 * margin, Math.min(view.y, H0 - view.h + H0 * margin));
  }
  function zoomAt(cx, cy, factor) {
    var rect = svg.getBoundingClientRect();
    var fx = (cx - rect.left) / rect.width, fy = (cy - rect.top) / rect.height;
    var wx = view.x + fx * view.w, wy = view.y + fy * view.h;
    var minW = W0 / 8, maxW = W0;
    var nw = Math.max(minW, Math.min(maxW, view.w / factor));
    var nh = nw * (H0 / W0);
    view.x = wx - fx * nw; view.y = wy - fy * nh; view.w = nw; view.h = nh;
    clampView(); applyView();
  }
  function focusOn(x, y, w) {
    w = w || W0 * 0.4; var h = w * (H0 / W0);
    view.x = x - w / 2; view.y = y - h / 2; view.w = w; view.h = h;
    clampView(); applyView();
  }
  svg.addEventListener('wheel', function (e) {
    e.preventDefault();
    zoomAt(e.clientX, e.clientY, e.deltaY < 0 ? 1.18 : 1 / 1.18);
  }, { passive: false });

  // Pan (1 pointer) + pinch-zoom (2 pointers, mobile). Pointer events unify
  // touch + mouse; #carte-svg has touch-action:none so the browser doesn't steal
  // the gesture. Only capture the pointer AFTER real movement, so a plain tap
  // still reaches the POI (capturing on pointerdown would retarget the click).
  var pdown = false, dragging = false, moved = false, captured = false;
  var pid = null, downX = 0, downY = 0, lastX = 0, lastY = 0;
  var pointers = {}, pinchDist = 0;
  function ptrPts() { return Object.keys(pointers).map(function (k) { return pointers[k]; }); }
  function ptrDist() { var p = ptrPts(); return Math.hypot(p[0].x - p[1].x, p[0].y - p[1].y); }
  function ptrMid() { var p = ptrPts(); return [(p[0].x + p[1].x) / 2, (p[0].y + p[1].y) / 2]; }
  svg.addEventListener('pointerdown', function (e) {
    pointers[e.pointerId] = { x: e.clientX, y: e.clientY };
    if (Object.keys(pointers).length === 2) {   // enter pinch: cancel any single-pan
      pdown = false;
      if (captured) { try { svg.releasePointerCapture(pid); } catch (_) {} captured = false; }
      if (dragging) { dragging = false; svg.classList.remove('grabbing'); }
      pinchDist = ptrDist(); moved = true;
      return;
    }
    pdown = true; dragging = false; moved = false; captured = false; pid = e.pointerId;
    downX = lastX = e.clientX; downY = lastY = e.clientY;
  });
  svg.addEventListener('pointermove', function (e) {
    if (pointers[e.pointerId]) { pointers[e.pointerId].x = e.clientX; pointers[e.pointerId].y = e.clientY; }
    if (Object.keys(pointers).length >= 2) {     // pinch-zoom around the fingers' midpoint
      var nd = ptrDist();
      if (pinchDist > 0 && nd > 0) { var mid = ptrMid(); zoomAt(mid[0], mid[1], nd / pinchDist); }
      pinchDist = nd; moved = true;
      return;
    }
    if (!pdown) return;
    if (!dragging) {
      if (Math.abs(e.clientX - downX) + Math.abs(e.clientY - downY) < 4) return;
      dragging = true; moved = true; svg.classList.add('grabbing');
      try { svg.setPointerCapture(pid); captured = true; } catch (_) {}
    }
    var rect = svg.getBoundingClientRect();
    var dx = (e.clientX - lastX) / rect.width * view.w;
    var dy = (e.clientY - lastY) / rect.height * view.h;
    view.x -= dx; view.y -= dy; lastX = e.clientX; lastY = e.clientY;
    clampView(); applyView();
  });
  function endDrag(e) {
    if (e && pointers[e.pointerId]) delete pointers[e.pointerId];
    if (Object.keys(pointers).length < 2) pinchDist = 0;
    pdown = false;
    if (captured) { try { svg.releasePointerCapture(pid); } catch (_) {} captured = false; }
    if (dragging) { dragging = false; svg.classList.remove('grabbing'); }
  }
  svg.addEventListener('pointerup', endDrag);
  svg.addEventListener('pointercancel', endDrag);
  // swallow click after a drag so we don't accidentally select on pan-release
  gPois.addEventListener('click', function (e) { if (moved) { e.stopPropagation(); } }, true);

  // ---- panel ----
  var panel = document.getElementById('carte-panel');
  var selectedId = null;
  function selectPoi(id) {
    var p = poiById[id]; if (!p) return;
    selectedId = id;
    for (var k in poiNodes) poiNodes[k].classList.remove('selected');
    poiNodes[id].classList.add('selected');
    var h = '<h3>' + esc(p.name) + '</h3>';
    var meta = [];
    var tl = (M.types || []).find(function (t) { return t.key === p.type; });
    if (tl) meta.push(esc(tl.label));
    if (p.importance) meta.push(esc(p.importance));
    if (p.approx) meta.push('position approchée');
    if (meta.length) h += '<span class="carte-zone-tag">' + meta.join(' · ') + '</span>';
    if (p.section) h += '<div class="carte-quarter-tag">Section : ' + esc(sectionLabel(p.section))
      + (p.quartier ? ' · Quartier : ' + esc(p.quartier) : '') + '</div>';
    if (p.desc) h += '<p class="carte-desc">' + esc(p.desc) + '</p>';
    h += ficheLink(p.ficheUrl, 'Fiche du lieu');
    // Scenes are shown ONLY for the currently selected scenario (nothing if "aucun").
    var cur = document.getElementById('carte-scenario').value;
    var refs = cur && p.scenarios ? p.scenarios[cur] : null;
    if (refs && refs.length) {
      h += '<h4>' + esc(cur) + '</h4><ul class="carte-scenes">';
      refs.forEach(function (ref) {
        h += ref.url ? '<li><a href="' + esc(ref.url) + '">' + esc(ref.label) + '</a></li>'
                     : '<li>' + esc(ref.label) + '</li>';
      });
      h += '</ul>';
    }
    panel.innerHTML = h;
  }

  function sectionLabel(key) {
    var s = (M.sections || []).find(function (x) { return x.key === key; });
    return s ? s.label : key;
  }
  // POI, quartier and section all link to their fiche the same way.
  function ficheLink(url, label) {
    return url ? '<a class="carte-fiche-link" href="' + esc(url) + '">' + label + ' →</a>' : '';
  }
  function bindPanelLinks() {
    panel.querySelectorAll('[data-poi]').forEach(function (a) {
      a.addEventListener('click', function () { selectPoi(a.getAttribute('data-poi')); }); });
    panel.querySelectorAll('[data-quarter]').forEach(function (a) {
      a.addEventListener('click', function () { showQuarterByName(a.getAttribute('data-quarter')); }); });
  }
  function showSection(key) {
    for (var k in poiNodes) poiNodes[k].classList.remove('selected');
    selectedId = null;
    var meta = (M.sections || []).find(function (x) { return x.key === key; });
    var h = '<h3>' + esc(meta ? meta.label : key) + '</h3><span class="carte-zone-tag">Section</span>';
    if (meta && meta.desc) h += '<p class="carte-desc">' + esc(meta.desc) + '</p>';
    h += ficheLink(meta && meta.ficheUrl, 'Fiche de la section');
    var quartiers = (M.quarterPolygons && M.quarterPolygons.length
        ? M.quarterPolygons.filter(function (q) { return q.section === key; })
                           .map(function (q) { return q.name; })
        : seeds.filter(function (s) { return s.ref.section === key; })
               .map(function (s) { return s.name; })).sort();
    if (quartiers.length) {
      h += '<h4>Quartiers</h4><ul class="carte-poi-list">';
      quartiers.forEach(function (n) { h += '<li><a data-quarter="' + esc(n) + '">' + esc(n) + '</a></li>'; });
      h += '</ul>';
    }
    panel.innerHTML = h; bindPanelLinks();
  }
  function showQuarter(seed) {
    if (!seed) return;
    if (seed.kind === 'poi') { selectPoi(seed.ref.id); return; }  // a quarter-POI is a place
    for (var k in poiNodes) poiNodes[k].classList.remove('selected');
    selectedId = null;
    var dz = seed.ref;
    var h = '<h3>' + esc(dz.name) + '</h3>'
          + '<span class="carte-zone-tag">Quartier · ' + esc(sectionLabel(dz.section)) + '</span>';
    if (dz.desc) h += '<p class="carte-desc">' + esc(dz.desc) + '</p>';
    h += liveZoneNote(dz.section);
    h += ficheLink(dz.ficheUrl, 'Fiche du quartier');
    panel.innerHTML = h;
  }

  // ---- scenario selector ----
  var sel = document.getElementById('carte-scenario');
  var toggle = document.getElementById('carte-toggle-scenario');
  var hubLink = document.getElementById('carte-hub-link');
  var scenNames = {};
  (M.scenarios || []).forEach(function (s) { scenNames[s.name] = s; });
  M.pois.forEach(function (p) { Object.keys(p.scenarios || {}).forEach(function (nm) {
    if (!scenNames[nm]) scenNames[nm] = { name: nm }; }); });
  Object.keys(scenNames).forEach(function (nm) {
    var o = document.createElement('option'); o.value = nm; o.textContent = nm; sel.appendChild(o); });
  function poiHasScenario(p, nm) { return p.scenarios && p.scenarios[nm] && p.scenarios[nm].length > 0; }
  function applyScenario() {
    var nm = sel.value, hideOthers = toggle.checked;
    M.pois.forEach(function (p) {
      var node = poiNodes[p.id]; node.classList.remove('has-scenario', 'dim');
      if (!nm) return;
      if (poiHasScenario(p, nm)) node.classList.add('has-scenario');
      else if (hideOthers) node.classList.add('dim');
    });
    var meta = scenNames[nm];
    if (nm && meta && meta.hubUrl) { hubLink.href = meta.hubUrl; hubLink.hidden = false;
      hubLink.textContent = 'Hub : ' + nm + ' →'; } else { hubLink.hidden = true; }
    applySearch();
    syncLive();
    if (selectedId) selectPoi(selectedId);   // re-render panel for the new scenario
  }
  sel.addEventListener('change', applyScenario);
  toggle.addEventListener('change', applyScenario);

  // ---- Écran live (#7): heure × zone × variante → ce qui se passe autour des PJ ----
  var LIVE = M.scenarioLive || {};
  var liveEl = document.getElementById('carte-live');
  var liveHourSel = document.getElementById('carte-live-hour');
  var liveVarsEl = document.getElementById('carte-live-vars');
  var liveShowBtn = document.getElementById('carte-live-show');
  var activeVars = {};            // B/C/D heat flags (A = baseline)
  var liveScenario = null;
  function syncLive() {
    var lv = LIVE[sel.value];
    liveEl.hidden = !lv;
    if (!lv) { liveScenario = null; return; }
    if (liveScenario !== sel.value) {       // (re)build controls only on scenario change
      liveScenario = sel.value;
      liveHourSel.innerHTML = lv.hours.map(function (H, i) {
        return '<option value="' + i + '">' + esc(H.label) + '</option>'; }).join('');
      activeVars = {};
      liveVarsEl.innerHTML = lv.variantes.filter(function (v) { return v.key !== 'A'; })
        .map(function (v) { return '<button class="carte-chip" data-var="' + v.key + '">'
          + v.key + ' · ' + esc(v.label) + '</button>'; }).join('');
      liveVarsEl.querySelectorAll('[data-var]').forEach(function (b) {
        b.addEventListener('click', function () {
          var k = b.getAttribute('data-var'); activeVars[k] = !activeVars[k];
          b.classList.toggle('active', activeVars[k]); renderLive(); }); });
    }
  }
  function liveZoneNote(section) {
    var lv = LIVE[sel.value]; if (!lv) return '';
    var wealth = (lv.wealthBySection || {})[section]; if (!wealth) return '';
    var H = lv.hours[+liveHourSel.value || 0];
    return '<p class="carte-desc"><strong>Écran live · zone ' + esc(wealth) + '</strong> ('
      + esc(H.label) + ') : ' + esc(wealth === 'pauvre' ? H.pauvre : H.riche) + '</p>';
  }
  function renderLive() {
    var lv = LIVE[sel.value]; if (!lv) return;
    var H = lv.hours[+liveHourSel.value || 0];
    var anyHeat = activeVars.B || activeVars.C || activeVars.D;
    var h = '<h3>Écran live</h3><span class="carte-zone-tag">' + esc(H.label) + '</span>'
      + '<h4>Dans la rue</h4>'
      + '<p class="carte-desc"><strong>Riches</strong> (rive sud) : ' + esc(H.riche) + '</p>'
      + '<p class="carte-desc"><strong>Pauvres</strong> (Reikerbahn / docks) : ' + esc(H.pauvre) + '</p>'
      + '<h4>Rumeurs</h4><p class="carte-desc">Période : ' + esc(H.rumeur) + '.</p>';
    if (H.clock && H.clock.length)
      h += '<h4>Horloge</h4><ul class="carte-poi-list">'
        + H.clock.map(function (c) { return '<li>' + esc(c) + '</li>'; }).join('') + '</ul>';
    var tq = H.traque || {}, tl = [];
    if (tq.base) tl.push(tq.base);
    ['B', 'C', 'D'].forEach(function (k) { if (activeVars[k] && tq[k]) tl.push('[' + k + '] ' + tq[k]); });
    if (tl.length)
      h += '<h4>Traque</h4><ul class="carte-poi-list">'
        + tl.map(function (t) { return '<li>' + esc(t) + '</li>'; }).join('') + '</ul>';
    var defs = lv.variantes.filter(function (v) {
      return v.key === 'A' ? !anyHeat : activeVars[v.key]; });
    if (defs.length) {
      h += '<h4>Variante' + (defs.length > 1 ? 's' : '') + '</h4>';
      defs.forEach(function (v) { h += '<p class="carte-desc"><strong>' + v.key + ' — '
        + esc(v.label) + '</strong> : ' + esc(v.desc) + '</p>'; });
    }
    panel.innerHTML = h;
  }
  liveHourSel.addEventListener('change', renderLive);
  liveShowBtn.addEventListener('click', renderLive);

  // ---- search ----
  var searchInput = document.getElementById('carte-search');
  function applySearch() {
    var q = norm(searchInput.value.trim());
    M.pois.forEach(function (p) {
      var node = poiNodes[p.id]; node.classList.remove('search-hit');
      if (q && norm(p.name).indexOf(q) !== -1) node.classList.add('search-hit');
    });
  }
  searchInput.addEventListener('input', applySearch);

  // ---- filters: type (chips) + importance ----
  var activeTypes = {}; (M.types || []).forEach(function (t) { activeTypes[t.key] = true; });
  var showImp = { Notable: true, Mineur: true };
  var filtersEl = document.getElementById('carte-filters');
  function poiVisible(p) {
    if (!activeTypes[p.type || 'autre']) return false;
    if (!showImp[p.importance || 'Notable']) return false;
    return true;
  }
  function applyFilters() {
    M.pois.forEach(function (p) {
      poiNodes[p.id].classList.toggle('filtered-out', !poiVisible(p));
    });
    declutter();
  }
  if (filtersEl) {
    var h = '<span class="carte-filter-lbl">Types</span>';
    (M.types || []).forEach(function (t) {
      h += '<button class="carte-chip active" data-type="' + esc(t.key) + '">' + esc(t.label) + '</button>'; });
    h += '<span class="carte-filter-lbl">Importance</span>'
       + '<button class="carte-chip active" data-imp="Notable">Notable</button>'
       + '<button class="carte-chip active" data-imp="Mineur">Mineur</button>';
    filtersEl.innerHTML = h;
    filtersEl.querySelectorAll('[data-type]').forEach(function (b) {
      b.addEventListener('click', function () {
        var k = b.getAttribute('data-type'); activeTypes[k] = !activeTypes[k];
        b.classList.toggle('active', activeTypes[k]); applyFilters(); }); });
    filtersEl.querySelectorAll('[data-imp]').forEach(function (b) {
      b.addEventListener('click', function () {
        var k = b.getAttribute('data-imp'); showImp[k] = !showImp[k];
        b.classList.toggle('active', showImp[k]); applyFilters(); }); });
  }
  applyFilters();

  // ---- pathfinding (#6): REAL graph Dijkstra over the quarter graph ----
  // Quarter seeds — used only to label "quartiers traversés" in the panel.
  var QSEED = [];
  if (M.quarterPolygons && M.quarterPolygons.length)
    M.quarterPolygons.forEach(function (q) { QSEED.push({ name: q.name, x: q.cx, y: q.cy }); });
  else seeds.forEach(function (s) { QSEED.push({ name: s.name, x: s.x, y: s.y }); });
  function quarterAt(x, y) {
    var best = null, bd = Infinity;
    QSEED.forEach(function (s) { var d = (s.x - x) * (s.x - x) + (s.y - y) * (s.y - y); if (d < bd) { bd = d; best = s.name; } });
    return best;
  }

  // ---- Walkable navmesh + A* (trajets stay on land, cross water ONLY at bridges) ----
  // M.nav = {w,h,cell,bits}: a w×h grid (cell viewBox-units each) of walkable cells
  // (inside the walls, minus water, plus carved bridge corridors), packed 1 bit/cell.
  var NW = 0, NH = 0, NCELL = 1, WALK = null;
  if (M.nav && M.nav.bits) {
    NW = M.nav.w; NH = M.nav.h; NCELL = M.nav.cell;
    var _bin = atob(M.nav.bits); WALK = new Uint8Array(NW * NH);
    for (var _i = 0; _i < NW * NH; _i++) WALK[_i] = (_bin.charCodeAt(_i >> 3) >> (7 - (_i & 7))) & 1;
  }
  function wOK(gx, gy) { return gx >= 0 && gy >= 0 && gx < NW && gy < NH && WALK[gy * NW + gx]; }
  function toCell(x, y) { return [Math.max(0, Math.min(NW - 1, Math.floor(x / NCELL))), Math.max(0, Math.min(NH - 1, Math.floor(y / NCELL)))]; }
  function snapWalk(gx, gy) {
    if (wOK(gx, gy)) return [gx, gy];
    for (var r = 1; r < 90; r++)
      for (var dy = -r; dy <= r; dy++) for (var dx = -r; dx <= r; dx++) {
        if (Math.max(Math.abs(dx), Math.abs(dy)) !== r) continue;
        if (wOK(gx + dx, gy + dy)) return [gx + dx, gy + dy];
      }
    return null;
  }
  function astar(s, g) {
    if (!WALK) return null;
    var N = NW * NH, si = s[1] * NW + s[0], gi = g[1] * NW + g[0];
    if (!WALK[si] || !WALK[gi]) return null;
    function hh(x, y) { var dx = Math.abs(x - g[0]), dy = Math.abs(y - g[1]); return (dx + dy) + (1.4142 - 2) * Math.min(dx, dy); }
    var gs = new Float64Array(N); gs.fill(Infinity); gs[si] = 0;
    var came = new Int32Array(N); came.fill(-1);
    var closed = new Uint8Array(N), heap = [[hh(s[0], s[1]), si]];
    function push(f, i) { heap.push([f, i]); var c = heap.length - 1; while (c > 0) { var p = (c - 1) >> 1; if (heap[p][0] <= heap[c][0]) break; var t = heap[p]; heap[p] = heap[c]; heap[c] = t; c = p; } }
    function pop() { var top = heap[0], last = heap.pop(); if (heap.length) { heap[0] = last; var c = 0, n = heap.length; for (;;) { var l = 2 * c + 1, r = l + 1, m = c; if (l < n && heap[l][0] < heap[m][0]) m = l; if (r < n && heap[r][0] < heap[m][0]) m = r; if (m === c) break; var t = heap[m]; heap[m] = heap[c]; heap[c] = t; c = m; } } return top; }
    var DIR = [[1, 0, 1], [-1, 0, 1], [0, 1, 1], [0, -1, 1], [1, 1, 1.4142], [1, -1, 1.4142], [-1, 1, 1.4142], [-1, -1, 1.4142]];
    while (heap.length) {
      var ci = pop()[1];
      if (closed[ci]) continue; closed[ci] = 1;
      if (ci === gi) break;
      var cx = ci % NW, cy = (ci / NW) | 0, base = gs[ci];
      for (var d = 0; d < 8; d++) {
        var nx = cx + DIR[d][0], ny = cy + DIR[d][1];
        if (!wOK(nx, ny)) continue;
        if (DIR[d][2] > 1 && (!wOK(nx, cy) || !wOK(cx, ny))) continue;   // no corner-cutting
        var ni = ny * NW + nx, ng = base + DIR[d][2];
        if (ng < gs[ni]) { gs[ni] = ng; came[ni] = ci; push(ng + hh(nx, ny), ni); }
      }
    }
    if (gi !== si && came[gi] < 0) return null;
    var path = [], c = gi; while (c !== -1) { path.push([c % NW, (c / NW) | 0]); if (c === si) break; c = came[c]; }
    return path.reverse();
  }
  function losClear(a, b) {   // dense line-of-sight (≥2 samples/cell) so a smoothed
    // segment never clips a water corner the coarse Bresenham line would skip
    var dx = b[0] - a[0], dy = b[1] - a[1], n = Math.max(Math.abs(dx), Math.abs(dy)) * 2 || 1;
    for (var i = 0; i <= n; i++) {
      if (!wOK(Math.round(a[0] + dx * i / n), Math.round(a[1] + dy * i / n))) return false;
    }
    return true;
  }
  function smooth(path) {   // string-pull: drop intermediate cells with clear line-of-sight
    if (!path || path.length < 3) return path;
    var out = [path[0]], i = 0;
    while (i < path.length - 1) { var j = path.length - 1; while (j > i + 1 && !losClear(path[i], path[j])) j--; out.push(path[j]); i = j; }
    return out;
  }
  function cellCenter(c) { return [(c[0] + 0.5) * NCELL, (c[1] + 0.5) * NCELL]; }
  function drawRoute() {
    while (gRoute.firstChild) gRoute.removeChild(gRoute.firstChild);
    var pts = route.map(function (id) { return poiById[id]; }).filter(Boolean);
    var line = [], quarters = [];
    for (var i = 0; i < pts.length - 1; i++) {
      var a = pts[i], b = pts[i + 1];
      if (i === 0) line.push([a.x, a.y]);
      var sa = (function (c) { return snapWalk(c[0], c[1]); })(toCell(a.x, a.y));
      var sb = (function (c) { return snapWalk(c[0], c[1]); })(toCell(b.x, b.y));
      var seg = (sa && sb) ? astar(sa, sb) : null;
      if (seg) { seg = smooth(seg); for (var k = 0; k < seg.length; k++) line.push(cellCenter(seg[k])); }
      line.push([b.x, b.y]);
    }
    line.forEach(function (p) { var q = quarterAt(p[0], p[1]); if (q && quarters[quarters.length - 1] !== q) quarters.push(q); });
    if (line.length >= 2)
      gRoute.appendChild(el('polyline', { points: line.map(function (p) { return p[0].toFixed(1) + ',' + p[1].toFixed(1); }).join(' '),
        'class': 'carte-route' }));
    pts.forEach(function (p) { gRoute.appendChild(el('circle', { cx: p.x, cy: p.y, r: 5, 'class': 'carte-route-pt' })); });
    var seen = {}, qd = quarters.filter(function (q) { if (seen[q]) return false; seen[q] = 1; return true; });
    var h = '<h3>Trajet</h3><span class="carte-zone-tag">' + pts.length + ' point(s)</span>';
    if (!pts.length) h += '<p class="carte-panel-empty">Clique des lieux pour tracer un trajet (à pied, par les ponts).</p>';
    if (pts.length) h += '<h4>Étapes</h4><ul class="carte-poi-list">'
      + pts.map(function (p) { return '<li>' + esc(p.name) + '</li>'; }).join('') + '</ul>';
    if (qd.length) h += '<h4>Quartiers traversés (' + qd.length + ')</h4><ul class="carte-poi-list">'
      + qd.map(function (q) { return '<li>' + esc(q) + '</li>'; }).join('') + '</ul>';
    panel.innerHTML = h;
  }
  var routeBtn = document.getElementById('carte-route-toggle');
  if (routeBtn) routeBtn.addEventListener('click', function () {
    routeMode = !routeMode; route = [];
    routeBtn.classList.toggle('active', routeMode);
    while (gRoute.firstChild) gRoute.removeChild(gRoute.firstChild);
    svg.classList.toggle('route-mode', routeMode);
    if (routeMode) { panel.innerHTML = '<h3>Trajet</h3><p class="carte-panel-empty">'
      + 'Mode trajet activé. Clique des lieux dans l\\'ordre ; les quartiers traversés s\\'affichent. '
      + 'Re-clique « Trajet » pour effacer.</p>'; }
    else { panel.innerHTML = '<p class="carte-panel-empty">Clique un lieu sur la carte.</p>'; }
  });

  // ---- "Noms" toggle + reset view ----
  var namesToggle = document.getElementById('carte-toggle-names');
  if (namesToggle) namesToggle.addEventListener('change', function () {
    svg.classList.toggle('show-names', namesToggle.checked);
    declutter();
  });
  var zonesToggle = document.getElementById('carte-toggle-zones');
  if (zonesToggle) zonesToggle.addEventListener('change', function () {
    svg.classList.toggle('hide-zones', !zonesToggle.checked);
  });
  // Zone fill mode: "secteur" (by section colour) | "quartier" (per-quarter hue).
  var zoneModeSel = document.getElementById('carte-zone-mode');
  function applyZoneMode() {
    var mode = zoneModeSel ? zoneModeSel.value : 'secteur';
    var attr = (mode === 'quartier') ? 'data-quart-color' : 'data-sec-color';
    gZones.querySelectorAll('.carte-zone').forEach(function (z) {
      var c = z.getAttribute(attr); if (c) z.setAttribute('fill', c);
    });
  }
  if (zoneModeSel) zoneModeSel.addEventListener('change', applyZoneMode);
  // ---- clickable section legend (influence zones → their fiche via showSection) ----
  var secLegend = document.getElementById('carte-section-legend');
  if (secLegend && (M.sections || []).length) {
    var lbl = document.createElement('span');
    lbl.className = 'carte-seclegend-lbl'; lbl.textContent = 'Secteurs :';
    secLegend.appendChild(lbl);
    M.sections.forEach(function (s) {
      var chip = document.createElement('button');
      chip.type = 'button'; chip.className = 'carte-seclegend-chip';
      chip.setAttribute('data-section', s.key);
      var sw = document.createElement('span');
      sw.className = 'carte-seclegend-sw';
      sw.style.background = SECTION_COLOR[s.key] || '#8a7a5a';
      chip.appendChild(sw);
      chip.appendChild(document.createTextNode(s.label));
      chip.addEventListener('click', function () {
        showSection(s.key);
        gZones.querySelectorAll('.carte-zone').forEach(function (z) {
          z.classList.toggle('sec-flash', z.getAttribute('data-section') === s.key);
        });
      });
      secLegend.appendChild(chip);
    });
    secLegend.hidden = false;
  }
  var resetBtn = document.getElementById('carte-reset');
  if (resetBtn) resetBtn.addEventListener('click', function () {
    view = { x: 0, y: 0, w: W0, h: H0 }; applyView();
  });

  applyView();
})();
"""


def _load_map_data() -> list[dict]:
    """Walk Notes MJ/Cartes/*.json and return parsed map objects."""
    if not CARTES_DIR.exists():
        return []
    maps: list[dict] = []
    for f in sorted(CARTES_DIR.glob("*.json")):
        if f.name.startswith("_"):
            continue   # working data (anchors, etc.), not a map
        try:
            maps.append(json.loads(f.read_text(encoding="utf-8")))
        except (json.JSONDecodeError, OSError) as exc:
            print(f"  ! carte: failed to parse {f.name}: {exc}")
    return maps


def _carte_entities_from_fiches(map_id: str) -> tuple[list[dict], list[dict], list[dict]]:
    """Read POIs / quartiers / sections for a map from the `carte:` frontmatter
    of Notes MJ/Lieux fiches. This is the authoritative source (the map JSON no
    longer carries pois/districts/sections). Returns (pois, districts, sections)."""
    pois: list[dict] = []
    districts: list[dict] = []
    sections: list[dict] = []
    lieux = NOTES_MJ_DIR / "Lieux"
    if not lieux.exists():
        return pois, districts, sections
    for f in sorted(lieux.glob("*.md")):
        c = _parse_carte_block(f.read_text(encoding="utf-8"))
        if not c or c.get("map") != map_id:
            continue
        kind = c.get("kind", "lieu")
        name = f.stem
        if kind == "section":
            sections.append({"key": c.get("section", ""), "label": name,
                             "desc": c.get("desc", ""), "color": c.get("color", "")})
            continue
        try:
            x = float(c["x"]); y = float(c["y"])
        except (KeyError, ValueError, TypeError):
            print(f"  ! carte: fiche sans coords x/y ignorée : {name}")
            continue
        if kind == "quartier":
            districts.append({"name": name, "x": x, "y": y,
                              "section": c.get("section", ""), "desc": c.get("desc", "")})
        else:  # lieu
            poi = {"id": _mj_slug(name), "name": name, "x": x, "y": y,
                   "type": c.get("type", "autre"),
                   "importance": c.get("importance", "Notable"),
                   "section": c.get("section", ""), "desc": c.get("desc", ""),
                   "fiche": name, "scenarios": {}}
            if str(c.get("seed", "")).lower() in ("true", "oui", "1"):
                poi["seed"] = True
            if c.get("quartier"):
                poi["quartier"] = c["quartier"]
            pois.append(poi)
    return pois, districts, sections


def _build_lieux_url_index(pages: list[Page]) -> dict[str, Path]:
    """norm-key → site_rel for public Lieux pages + MJ-only Lieux entities.
    Public pages win when both exist (canonical reader-facing page)."""
    idx: dict[str, Path] = {}
    # MJ-only standalone Lieux fiches first (lower priority — overwritten below).
    if _MJ_ONLY_ENTITIES:
        for e in _MJ_ONLY_ENTITIES.get("Lieux", []):
            idx[e.norm_key] = Path(f"mj-{MJ_TOKEN}") / e.out_url
    # Public Lieux pages (higher priority).
    for pg in pages:
        if pg.post.folder == "Lieux":
            idx[_norm_entity_key(pg.post.title or pg.slug)] = pg.site_rel
    return idx


def _resolve_carte_links(map_obj: dict, current_dir: Path,
                         lieux_idx: dict[str, Path],
                         mj_idx: dict[str, Path],
                         scenario_hub_url: dict[str, str]) -> dict:
    """Return a copy of the map with fiche/scene/hub references resolved to
    URLs relative to the carte page. Unresolved targets are logged + dropped."""
    out = json.loads(json.dumps(map_obj))  # deep copy
    mj_root = Path(f"mj-{MJ_TOKEN}")

    def lieux_url(stem: str) -> str | None:
        target = lieux_idx.get(_norm_entity_key(stem))
        if target is None:
            # try manual alias (blog orthographic variants)
            alias = _MJ_MANUAL_ALIASES.get(stem)
            if alias:
                target = lieux_idx.get(_norm_entity_key(alias))
        return relative_url(current_dir, target) if target else None

    def scene_url(stem: str) -> str | None:
        for k in (stem.lower(), *(_norm_keys_for_match(stem))):
            if k in mj_idx:
                return relative_url(current_dir, mj_root / mj_idx[k])
        return None

    # map-level fiche
    if out.get("fiche"):
        u = lieux_url(out["fiche"])
        out["ficheUrl"] = u
        if u is None:
            print(f"  ! carte: fiche introuvable « {out['fiche']} »")
    # scenarios hub urls
    for sc in out.get("scenarios", []):
        sc["hubUrl"] = scenario_hub_url.get(sc["name"])
    # pois
    for poi in out.get("pois", []):
        if poi.get("fiche"):
            u = lieux_url(poi["fiche"])
            poi["ficheUrl"] = u
            if u is None:
                print(f"  ! carte: fiche introuvable « {poi['fiche']} » (POI {poi['id']})")
        for sc_name, refs in (poi.get("scenarios") or {}).items():
            for ref in refs:
                ref["url"] = scene_url(ref["scene"])
                if ref["url"] is None:
                    print(f"  ! carte: scène introuvable « {ref['scene']} » "
                          f"(POI {poi['id']}, scénario {sc_name})")
    # quartiers & sections are fiches too (kind: quartier / section) → link them
    # like POIs so the panel offers "Fiche du quartier / de la section →".
    for dz in out.get("districts", []):
        if dz.get("name"):
            dz["ficheUrl"] = lieux_url(dz["name"])
            if dz["ficheUrl"] is None:
                print(f"  ! carte: fiche quartier introuvable « {dz['name']} »")
    for sec in out.get("sections", []):
        nm = sec.get("label") or sec.get("key")
        if nm:
            sec["ficheUrl"] = lieux_url(nm)
            if sec["ficheUrl"] is None:
                print(f"  ! carte: fiche section introuvable « {nm} »")
    return out


def render_carte_pages(pages: list[Page], buckets: dict[int, ArcBucket],
                       mj_idx: dict[str, Path]) -> dict[Path, str]:
    """Build the MJ carte index + one page per map. Returns {out_rel: html}."""
    if not MJ_TOKEN:
        return {}
    maps = _load_map_data()
    if not maps:
        return {}
    lieux_idx = _build_lieux_url_index(pages)

    cartes_dir = Path(f"mj-{MJ_TOKEN}") / "cartes"
    mj_root = Path(f"mj-{MJ_TOKEN}")

    out_pages: dict[Path, str] = {}
    map_links = []
    for map_obj in maps:
        map_id = map_obj.get("id") or _mj_slug(map_obj.get("title", "carte"))
        page_dir = cartes_dir
        # POIs / quartiers / sections are read from the Lieux fiches (authoritative),
        # not from the map JSON. The scenario overlay (which can't live in durable
        # fiches) is merged from the JSON, keyed by POI id.
        fpois, fdist, fsec = _carte_entities_from_fiches(map_id)
        if fpois or fdist or fsec:
            map_obj = dict(map_obj)
            overlay = map_obj.get("scenarioOverlay", {})
            for p in fpois:
                if p["id"] in overlay:
                    p["scenarios"] = overlay[p["id"]]
            map_obj["pois"] = fpois
            map_obj["districts"] = fdist
            map_obj["sections"] = fsec or map_obj.get("sections", [])
            print(f"  - carte {map_id}: {len(fpois)} POI, {len(fdist)} quartiers, "
                  f"{len(fsec)} sections (depuis fiches)")
        # hub urls per scenario, resolved against this page dir
        sc_hub: dict[str, str] = {}
        for sc in map_obj.get("scenarios", []):
            tgt = mj_idx.get(sc["name"].lower())
            if tgt:
                sc_hub[sc["name"]] = relative_url(page_dir, mj_root / tgt)
        resolved = _resolve_carte_links(map_obj, page_dir, lieux_idx, mj_idx, sc_hub)
        if map_obj.get("poster"):
            resolved["posterUrl"] = relative_url(page_dir, mj_root / map_obj["poster"])

        data_json = json.dumps(resolved, ensure_ascii=False)
        title = map_obj.get("title", "Carte")
        # content-hash version → busts stale browser/CDN cache when assets change
        ver = hashlib.md5((CARTE_JS + CARTE_CSS).encode("utf-8")).hexdigest()[:8]
        body = f"""
<nav class="mj-breadcrumb">
  <a href="index.html">Cartes</a> · <span>{html.escape(title)}</span>
  <span class="mj-badge">MJ</span>
</nav>
<link rel="stylesheet" href="../carte.css?v={ver}">
<div id="carte-app" class="carte-app" data-map="{html.escape(map_id)}">
  <div class="carte-toolbar">
    <label class="carte-field">Scénario
      <select id="carte-scenario"><option value="">— aucun —</option></select>
    </label>
    <label class="carte-field carte-search-field">Lieu
      <input id="carte-search" type="search" placeholder="Rechercher un lieu…"
             autocomplete="off" spellcheck="false">
    </label>
    <label class="carte-toggle"><input type="checkbox" id="carte-toggle-scenario" checked>
      Lieux du scénario</label>
    <label class="carte-toggle"><input type="checkbox" id="carte-toggle-names">
      Noms</label>
    <label class="carte-toggle"><input type="checkbox" id="carte-toggle-zones" checked>
      Couleur</label>
    <label class="carte-field"><span>Zones</span>
      <select id="carte-zone-mode">
        <option value="secteur">par secteur</option>
        <option value="quartier">par quartier</option>
      </select></label>
    <button type="button" id="carte-route-toggle" class="carte-btn">Trajet</button>
    <button type="button" id="carte-reset" class="carte-btn">Vue entière</button>
    <a id="carte-hub-link" class="carte-hub-link" hidden href="#">Hub du scénario →</a>
  </div>
  <div class="carte-filters" id="carte-filters"></div>
  <div class="carte-seclegend" id="carte-section-legend" hidden></div>
  <div class="carte-live" id="carte-live" hidden>
    <span class="carte-filter-lbl">Écran live</span>
    <label class="carte-field carte-live-hour-field">Heure
      <select id="carte-live-hour"></select>
    </label>
    <span class="carte-filter-lbl">Variantes</span>
    <span id="carte-live-vars"></span>
    <button type="button" id="carte-live-show" class="carte-btn">Écran live →</button>
  </div>
  <div class="carte-stage">
    <div class="carte-svg-wrap"><svg id="carte-svg" role="img"
         aria-label="Carte schématique d'Altdorf"></svg></div>
    <aside id="carte-panel" class="carte-panel" aria-live="polite">
      <p class="carte-panel-empty">Clique un lieu sur la carte.</p>
    </aside>
  </div>
</div>
<script type="application/json" id="carte-data">{data_json}</script>
<script src="../carte.js?v={ver}" defer></script>
"""
        out_pages[Path("cartes") / f"{map_id}.html"] = layout(
            page_dir, title, body, extra_class="page-carte", buckets=buckets)
        map_links.append(
            f'<li><a class="entry-card" href="{html.escape(map_id)}.html">'
            f'<span class="entry-title">{html.escape(title)}</span>'
            f'<span class="mj-badge">MJ</span></a></li>')

    # index
    idx_body = f"""
<nav class="mj-breadcrumb"><span>Cartes</span> <span class="mj-badge">MJ</span></nav>
<article class="post mj-content">
  <header class="post-header"><h1 class="post-title">Cartes interactives
    <span class="mj-badge">MJ</span></h1></header>
  <div class="post-body">
    <ul class="card-grid card-grid-entries">{''.join(map_links)}</ul>
  </div>
</article>
"""
    out_pages[Path("cartes") / "index.html"] = layout(
        cartes_dir, "Cartes", idx_body, extra_class="page-mj-overlay", buckets=buckets)
    return out_pages


def render_mj_overlay(pages: list[Page], buckets: dict[int, ArcBucket],
                     url_map: dict[str, Path],
                     label_map: dict[str, Path],
                     entity_popover_map: dict[str, EntityPopover] | None = None) -> int:
    """Generate the MJ overlay under _site/mj-{TOKEN}/. Returns page count.
    No-op if no MJ token is configured (env var or .mj-token file)."""
    if not MJ_TOKEN or MJ_OUT_DIR is None:
        print("MJ overlay: skipped (no MJ_TOKEN configured — set env var "
              "MJ_TOKEN or create .mj-token file to enable).")
        return 0
    if not NOTES_MJ_DIR.exists():
        return 0
    mj_pages = _walk_notes_mj_overlay()
    if not mj_pages:
        return 0
    mj_idx = _build_mj_wikilink_index(mj_pages)

    # Group scenario pages by scenario name, for the per-scenario left rail.
    scenario_pages_by_name: dict[str, list[MJOverlayPage]] = {}
    for p in mj_pages:
        if (p.category in ("scenario_hub", "scenario_scene", "scenario_ref")
                and p.scenario):
            scenario_pages_by_name.setdefault(p.scenario, []).append(p)

    count = 0
    for mj_page in mj_pages:
        siblings = scenario_pages_by_name.get(mj_page.scenario or "")
        html_out = _render_mj_overlay_page(mj_page, mj_idx, url_map, buckets,
                                            entity_popover_map, siblings)
        out_path = MJ_OUT_DIR / mj_page.out_rel_path
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_text(html_out, encoding="utf-8")
        count += 1

    # Phase 3: standalone pages for MJ-only entities (no blog counterpart)
    if _MJ_ONLY_ENTITIES:
        for src_folder, entities in _MJ_ONLY_ENTITIES.items():
            for e in entities:
                fake_out_rel = Path(e.out_url)
                body_html = _render_mj_entity_body(e, fake_out_rel, mj_idx, url_map)
                # Entity popovers for inline mentions. Use alias-resolved map.
                cur_dir = (MJ_OUT_DIR / fake_out_rel).parent.relative_to(OUT)
                popover_map = _MJ_POPOVER_MAP if _MJ_POPOVER_MAP else entity_popover_map
                if popover_map:
                    body_html = inject_entity_popovers(body_html, popover_map, cur_dir)
                # MJ-only canon refs popover + click navigation
                body_html = inject_canon_refs(body_html, cur_dir)
                cat_label = FOLDER_TO_LABEL.get(src_folder, src_folder)
                page_body = (
                    f'<nav class="mj-breadcrumb">'
                    f'<a href="{_relpath_within_mj(fake_out_rel.parent, Path("index.html"))}">Notes MJ</a>'
                    f' · <span>{html.escape(cat_label)}</span> · <span>{html.escape(e.title)}</span>'
                    f'</nav>'
                    f'<article class="post mj-content mj-entity-only">'
                    f'<header class="post-header"><h1 class="post-title">{html.escape(e.title)} '
                    f'<span class="mj-badge">MJ</span></h1></header>'
                    f'<div class="post-body">{body_html}</div>'
                    f'</article>'
                )
                current_dir = (MJ_OUT_DIR / fake_out_rel).parent.relative_to(OUT)
                html_out = layout(current_dir, e.title, page_body,
                                  extra_class="page-mj-overlay", buckets=buckets)
                out_path = MJ_OUT_DIR / fake_out_rel
                out_path.parent.mkdir(parents=True, exist_ok=True)
                out_path.write_text(html_out, encoding="utf-8")
                count += 1

    # Index pages
    index_pages = _render_mj_index_pages(mj_pages, buckets)
    for rel, html_out in index_pages.items():
        out_path = MJ_OUT_DIR / rel
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_text(html_out, encoding="utf-8")
        count += 1

    # Cartes interactives (schéma-graphe SVG cliquable)
    carte_pages = render_carte_pages(pages, buckets, mj_idx)
    for rel, html_out in carte_pages.items():
        out_path = MJ_OUT_DIR / rel
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_text(html_out, encoding="utf-8")
        count += 1
    if carte_pages:
        (MJ_OUT_DIR / "carte.css").write_text(CARTE_CSS, encoding="utf-8")
        (MJ_OUT_DIR / "carte.js").write_text(CARTE_JS, encoding="utf-8")
        # Copy poster/raster assets (PDF isn't on the deploy pipeline, so the
        # rendered image must travel as a committed asset under Notes MJ/Cartes/).
        if CARTES_DIR.exists():
            for asset in list(CARTES_DIR.glob("*.jpg")) + list(CARTES_DIR.glob("*.png")):
                shutil.copyfile(asset, MJ_OUT_DIR / asset.name)
        print(f"  - carte pages: {len(carte_pages)}")

    # Canon Source pages (target of canon-ref click navigation)
    source_pages = _render_canon_source_pages(buckets)
    for rel, html_out in source_pages.items():
        out_path = MJ_OUT_DIR / rel
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_text(html_out, encoding="utf-8")
        count += 1
    if source_pages:
        print(f"  - canon source pages: {len(source_pages)}")

    # MJ search index
    (MJ_OUT_DIR / "search-index.json").write_text(
        json.dumps(_build_mj_search_index(mj_pages), ensure_ascii=False),
        encoding="utf-8")

    # robots.txt within mj subdir (defense in depth)
    (MJ_OUT_DIR / "robots.txt").write_text(
        "User-agent: *\nDisallow: /\n", encoding="utf-8")

    print(f"MJ overlay: {count} pages → {MJ_OUT_DIR}")
    return count


_MJ_SCENARIOS_BY_ARC: dict[int, list[str]] | None = None


def _discover_scenarios_by_arc() -> dict[int, list[str]]:
    """Walk Notes MJ/Scénarios/ and read frontmatter to build arc→scenarios."""
    out: dict[int, list[str]] = {}
    sc_root = NOTES_MJ_DIR / "Scénarios"
    if not sc_root.exists():
        return out
    for scenario_dir in sorted(sc_root.iterdir()):
        if not scenario_dir.is_dir():
            continue
        arc_num = _scenario_arc_from_hub(scenario_dir / "Hub.md")
        if arc_num is None:
            continue
        out.setdefault(arc_num, []).append(scenario_dir.name)
    return out


def _get_scenarios_by_arc() -> dict[int, list[str]]:
    global _MJ_SCENARIOS_BY_ARC
    if _MJ_SCENARIOS_BY_ARC is None:
        _MJ_SCENARIOS_BY_ARC = _discover_scenarios_by_arc()
    return _MJ_SCENARIOS_BY_ARC


def mj_scenarios_section_html(arc_num: int) -> str:
    """Return the HTML block for the Scénarios section on an arc landing page.
    Block has class .mj-only so it stays invisible until MJ mode toggled.
    Returns empty string if no MJ token configured or no scenarios for this arc."""
    if not MJ_TOKEN:
        return ""
    scenarios = _get_scenarios_by_arc().get(arc_num, [])
    if not scenarios:
        return ""
    items = []
    for sc in scenarios:
        sc_slug = _mj_slug(sc)
        sc_url = f"mj-{MJ_TOKEN}/scenarios/{sc_slug}/index.html"
        items.append(
            f'<li><a class="entry-card" href="{html.escape(sc_url)}">'
            f'<span class="entry-title">{html.escape(sc)}</span>'
            f'<span class="mj-badge">MJ</span></a></li>')
    inner = "\n".join(items)
    return f"""
<section class="arc-cat mj-only" id="scenarios">
  <h2 class="cat-heading">
    <a href="mj-{MJ_TOKEN}/scenarios/index.html">Scénarios <span class="mj-badge">MJ</span></a>
    <span class="cat-rule"></span>
    <span class="cat-tally">{len(scenarios)}</span>
  </h2>
  <ul class="card-grid card-grid-entries">
    {inner}
  </ul>
</section>
"""


MJ_MODE_JS = """\
// MJ mode toggle: ?mj=TOKEN sets localStorage flag; ?mj=off clears.
// When the flag matches the build-time token, body.mj-mode is applied
// and CSS reveals .mj-only sections + adds a corner badge.
(function() {
  var TOKEN = "__MJ_TOKEN__";
  var p = new URLSearchParams(window.location.search);
  var fromUrl = p.get('mj');
  function cleanUrl() {
    p.delete('mj');
    var s = p.toString();
    var u = window.location.pathname + (s ? '?' + s : '') + window.location.hash;
    window.history.replaceState({}, '', u);
  }
  if (fromUrl === TOKEN) {
    localStorage.setItem('mjMode', TOKEN);
    cleanUrl();
  } else if (fromUrl === 'off') {
    localStorage.removeItem('mjMode');
    cleanUrl();
  }

  function activate() {
    document.documentElement.classList.add('mj-mode');
    if (!document.body) return;
    document.body.classList.add('mj-mode');
    if (document.getElementById('mj-mode-badge')) return;
    var b = document.createElement('div');
    b.id = 'mj-mode-badge';
    b.innerHTML = '<span>Mode MJ</span><button title="Quitter le mode MJ" aria-label="Quitter">×</button>';
    b.querySelector('button').addEventListener('click', function() {
      localStorage.removeItem('mjMode');
      location.reload();
    });
    document.body.appendChild(b);
  }

  if (localStorage.getItem('mjMode') === TOKEN) {
    if (document.body) document.body.dataset.mjToken = TOKEN;
    activate();
    if (!document.body) {
      document.addEventListener('DOMContentLoaded', function() {
        document.body.dataset.mjToken = TOKEN;
        activate();
      });
    }
  }

  // -------- Canon ref popover (MJ-only) --------------------------------
  // Triggered by hover on <span class="canon-ref" data-source data-extract>.
  // Shows the cited markdown lines from the Source/ book tree.
  var cPop = null, cShowTimer = null, cHideTimer = null;

  function cEnsurePopover() {
    if (cPop) return cPop;
    cPop = document.createElement('div');
    cPop.className = 'canon-popover';
    cPop.addEventListener('mouseenter', function () {
      if (cHideTimer) { clearTimeout(cHideTimer); cHideTimer = null; }
    });
    cPop.addEventListener('mouseleave', cSchedulePopoverHide);
    (document.body || document.documentElement).appendChild(cPop);
    return cPop;
  }

  function cFillPopover(trigger) {
    var p = cEnsurePopover();
    var src     = trigger.getAttribute('data-source')  || trigger.textContent.trim();
    var extract = trigger.getAttribute('data-extract') || '';
    p.innerHTML = '';
    var hdr = document.createElement('div');
    hdr.className = 'canon-popover-header';
    hdr.textContent = src;
    p.appendChild(hdr);
    var body = document.createElement('div');
    body.className = 'canon-popover-body';
    body.textContent = extract;
    p.appendChild(body);
    p.style.display = 'block';
    cPositionPopover(p, trigger);
  }

  function cPositionPopover(p, trigger) {
    var rect = trigger.getBoundingClientRect();
    var pw = p.offsetWidth, ph = p.offsetHeight;
    var top  = rect.bottom + 8;                       // prefer below
    var left = rect.left + rect.width / 2 - pw / 2;
    if (top + ph > window.innerHeight - 8) {
      top = rect.top - ph - 8;                        // flip above if no room
      if (top < 8) top = 8;
    }
    if (left < 8) left = 8;
    var maxLeft = window.innerWidth - pw - 8;
    if (left > maxLeft) left = Math.max(8, maxLeft);
    p.style.top = top + 'px';
    p.style.left = left + 'px';
  }

  function cSchedulePopoverShow(trigger) {
    if (cHideTimer) { clearTimeout(cHideTimer); cHideTimer = null; }
    if (cShowTimer) clearTimeout(cShowTimer);
    cShowTimer = setTimeout(function () { cFillPopover(trigger); }, 150);
  }
  function cSchedulePopoverHide() {
    if (cShowTimer) { clearTimeout(cShowTimer); cShowTimer = null; }
    if (cHideTimer) clearTimeout(cHideTimer);
    cHideTimer = setTimeout(function () {
      if (cPop) cPop.style.display = 'none';
    }, 200);
  }

  document.addEventListener('mouseover', function (e) {
    var t = e.target.closest && e.target.closest('.canon-ref');
    if (t) cSchedulePopoverShow(t);
  });
  document.addEventListener('mouseout', function (e) {
    var t = e.target.closest && e.target.closest('.canon-ref');
    if (t) cSchedulePopoverHide();
  });
})();
"""


MJ_CSS = """\
/* === MJ MODE === */
.mj-only { display: none !important; }
body.mj-mode .mj-only { display: revert !important; }

#mj-mode-badge {
  position: fixed;
  bottom: 1rem;
  right: 1rem;
  background: #6b3a2c;
  color: #efe5cf;
  padding: 0.4rem 0.9rem;
  border-radius: 3px;
  z-index: 9999;
  font-family: "IM Fell English SC", serif;
  font-size: 0.85rem;
  letter-spacing: 0.08em;
  display: flex;
  align-items: center;
  gap: 0.5rem;
  box-shadow: 0 2px 6px rgba(0,0,0,0.3);
}
#mj-mode-badge button {
  background: transparent;
  border: 1px solid rgba(239, 229, 207, 0.5);
  color: #efe5cf;
  cursor: pointer;
  font-size: 1.1rem;
  padding: 0 0.4rem;
  border-radius: 2px;
  line-height: 1;
  font-family: inherit;
}
#mj-mode-badge button:hover {
  background: rgba(239, 229, 207, 0.15);
}

.mj-badge {
  display: inline-block;
  background: #6b3a2c;
  color: #efe5cf;
  font-size: 0.6em;
  padding: 0.1em 0.45em;
  border-radius: 2px;
  margin-left: 0.4em;
  letter-spacing: 0.08em;
  vertical-align: middle;
  font-family: "IM Fell English SC", serif;
  font-weight: normal;
}

.broken-link {
  color: #b85c5c;
  text-decoration: line-through dashed;
  cursor: help;
}

.mj-breadcrumb {
  font-size: 0.85rem;
  color: #6b5a3c;
  margin-bottom: 1rem;
  padding-bottom: 0.4rem;
  border-bottom: 1px solid #c4b58a;
}
.mj-breadcrumb a { color: #6b3a2c; text-decoration: none; }
.mj-breadcrumb a:hover { text-decoration: underline; }

.mj-content table {
  border-collapse: collapse;
  width: 100%;
  margin: 1rem 0;
  font-size: 0.93rem;
}
.mj-content th, .mj-content td {
  border: 1px solid #c4b58a;
  padding: 0.4rem 0.65rem;
  text-align: left;
  vertical-align: top;
}
.mj-content th { background: #efe5cf; }
.mj-content blockquote {
  border-left: 4px solid #6b3a2c;
  padding: 0.4rem 1rem;
  margin: 0.8rem 0;
  background: rgba(107, 58, 44, 0.05);
  font-style: italic;
}
.mj-content blockquote p { margin: 0.3rem 0; }
.mj-content h1 {
  color: #6b3a2c;
  border-bottom: 2px solid #6b3a2c;
  padding-bottom: 0.3rem;
}
.mj-content h2 {
  color: #6b3a2c;
  border-bottom: 1px solid #c4b58a;
  padding-bottom: 0.2rem;
  margin-top: 2rem;
}
.mj-content h3 { color: #6b3a2c; margin-top: 1.5rem; }
.mj-content pre {
  background: #efe5cf;
  padding: 0.8rem;
  border-radius: 3px;
  overflow-x: auto;
  font-size: 0.88rem;
  border-left: 3px solid #6b3a2c;
}
.mj-cat-list { display: grid; gap: 1rem; }

/* MJ-only entries in the top nav are tinted */
body.mj-mode .site-nav .mj-only a {
  color: #6b3a2c;
  font-style: italic;
}
body.mj-mode .site-nav .mj-only a:hover {
  text-decoration: underline;
}

/* Search result MJ badge */
.search-mj-badge {
  display: inline-block;
  background: #6b3a2c;
  color: #efe5cf;
  font-size: 0.65em;
  padding: 0.1em 0.4em;
  border-radius: 2px;
  margin-left: 0.5em;
  letter-spacing: 0.08em;
  vertical-align: middle;
  font-family: "IM Fell English SC", serif;
}
.search-result-mj { background: rgba(107, 58, 44, 0.04); }
.search-result-mj:hover { background: rgba(107, 58, 44, 0.10); }

/* MJ enrichment section on public entity pages */
.mj-entity-enrichment {
  margin-top: 2.5rem;
  padding: 0;
}
.mj-entity-enrichment .mj-separator {
  border: none;
  border-top: 2px solid #6b3a2c;
  margin: 2rem 0 1rem;
  position: relative;
}
.mj-entity-enrichment .mj-label {
  display: inline-block;
  background: #6b3a2c;
  color: #efe5cf;
  padding: 0.25rem 0.8rem;
  border-radius: 3px;
  font-family: "IM Fell English SC", serif;
  font-size: 0.85rem;
  letter-spacing: 0.1em;
  margin-bottom: 1rem;
}
.mj-entity-enrichment .mj-content {
  padding: 0.5rem 0;
}

/* MJ-only entries in public category indexes */
ul.mj-only-entries {
  margin-top: 1.5rem;
  padding-top: 1rem;
  border-top: 1px dashed #6b3a2c;
}
ul.mj-only-entries::before {
  content: "Entrées MJ";
  display: block;
  font-family: "IM Fell English SC", serif;
  color: #6b3a2c;
  font-size: 0.9rem;
  letter-spacing: 0.08em;
  margin-bottom: 0.6rem;
}

/* === Canon refs (MJ-only) ============================================= */
/* `<code>EiR Intro l.205-218</code>` blocks become hover popover triggers
   that show the cited markdown lines from Source/. As <a> they also
   navigate to the rendered Source page at the cited line anchor. */
.canon-ref {
  color: #6b3a2c;
  cursor: help;
  text-decoration: none;
  transition: color 120ms ease;
}
a.canon-ref { text-decoration: none; }
.canon-ref sup {
  font-size: 0.75em;
  font-family: "Courier New", Courier, monospace;
  font-weight: 600;
  padding: 0 0.15em;
  border-radius: 2px;
  vertical-align: super;
  line-height: 0;
}
.canon-ref:hover { color: #4a2820; }
.canon-ref:hover sup {
  background: rgba(107, 58, 44, 0.14);
}

/* === Line anchors on canon Source pages =============================== */
/* Each raw markdown line gets a `<a class="line-anchor" id="LN"></a>` prefix
   so canon refs can deep-link to #L144. The anchor itself is invisible
   (zero-width inline). On :target the parent element flashes a highlight. */
.line-anchor {
  display: inline;
  width: 0;
  height: 0;
  overflow: hidden;
  position: relative;
  /* Scroll past the sticky header (if any) when jumping to a line. */
  scroll-margin-top: 80px;
}
@keyframes canon-target-flash {
  0%   { background-color: rgba(255, 220, 90, 0.85); }
  60%  { background-color: rgba(255, 220, 90, 0.55); }
  100% { background-color: transparent; }
}
/* Highlight the parent block of the targeted line anchor.
   :target only matches the element with the id, so we style the anchor
   itself + use a sibling-aware rule for common block parents.
   The cheapest effective rule: outline the anchor's containing block via
   :has() — supported in evergreen browsers. */
.line-anchor:target {
  background: rgba(255, 220, 90, 0.85);
  outline: 2px solid rgba(180, 130, 30, 0.6);
  outline-offset: 2px;
  animation: canon-target-flash 2.2s ease-out;
}
/* Better: highlight the parent block (heading, paragraph, list item) of
   the targeted line anchor. :has() ensures the flash covers the whole
   logical line, not just the zero-width anchor itself. */
.canon-source-page :is(h1,h2,h3,h4,h5,h6,p,li,blockquote,td,th):has(> .line-anchor:target),
.canon-source-page :is(h1,h2,h3,h4,h5,h6,p,li,blockquote,td,th):has(> .line-anchor:target *) {
  background: rgba(255, 220, 90, 0.55);
  animation: canon-target-flash 2.2s ease-out;
}
/* Canon Source pages get a slightly denser type for code-heavy chapters. */
.canon-source-page .post-body pre {
  font-size: 0.9rem;
}

.canon-popover {
  position: fixed;
  z-index: 110;
  display: none;
  max-width: 520px;
  min-width: 280px;
  background: #fbf3df;
  border: 1px solid #6b3a2c;
  box-shadow: 0 4px 18px rgba(60, 30, 10, 0.28);
  padding: 0.75rem 0.95rem;
  animation: canon-pop-fade 140ms ease-out;
  pointer-events: auto;
  font-family: "EB Garamond", "Garamond", serif;
}
@keyframes canon-pop-fade {
  from { opacity: 0; transform: translateY(2px); }
  to   { opacity: 1; transform: translateY(0); }
}
.canon-popover-header {
  font-family: "IM Fell English SC", serif;
  font-size: 0.78rem;
  letter-spacing: 0.14em;
  text-transform: uppercase;
  color: #6b3a2c;
  margin-bottom: 0.45rem;
  border-bottom: 1px solid #c4b58a;
  padding-bottom: 0.3rem;
}
.canon-popover-body {
  font-size: 0.92rem;
  line-height: 1.4;
  color: #2c241a;
  white-space: pre-wrap;
  max-height: 320px;
  overflow-y: auto;
}
@media (hover: none) {
  .canon-popover { display: none !important; }
}
"""


def build(clean: bool) -> int:
    setup_utf8_stdout()

    if clean and OUT.exists():
        print(f"Cleaning {OUT} ...")
        shutil.rmtree(OUT)
    OUT.mkdir(parents=True, exist_ok=True)

    print(f"Reading existing notes under {ROOT} ...")
    existing = read_existing_index(ALL_FOLDERS)
    print(f"  -> {len(existing)} files indexed")

    print("Fetching blog posts ...")
    with requests.Session() as s:
        s.headers["User-Agent"] = "blog-site-build/1.0"
        posts = fetch_all_posts(s)
    print(f"  -> {len(posts)} posts received")

    classify(posts, existing, ALL_FOLDERS)
    pages = build_pages(posts)
    compute_arc_session_ranges(pages)
    compute_variant_groups(pages)
    siblings_idx = variant_siblings_index(pages)

    # Populate the global main_for_group used by link_for() so any place that
    # constructs a URL to a Page transparently redirects variants to their
    # main + anchor.
    _MAIN_FOR_GROUP.clear()
    _PATH_TO_PAGE.clear()
    for pg in pages:
        if pg.variant_group and pg.is_main:
            _MAIN_FOR_GROUP[(pg.post.folder, pg.variant_group)] = pg
        _PATH_TO_PAGE[pg.site_rel] = pg
    url_map, label_map = build_site_url_map(pages)

    # Indexes
    pages_by_cat: dict[str, list[Page]] = {}
    for pg in pages:
        pages_by_cat.setdefault(pg.post.folder, []).append(pg)

    # Compute per-arc buckets (résumés by session num, others by which sessions
    # link to them via /search/label/ references in the HTML body).
    intros = find_intro_pages(pages)
    buckets = bucket_by_arc(pages, intros)
    pages_by_session = build_pages_by_session(pages)
    session_by_num_map = build_session_pages_by_num(pages)
    entity_popover_map = build_entity_popover_map(pages)

    # Phase 3: pre-compute MJ entity match cache so render_post_page and
    # render_category_index can look up enrichment + MJ-only entries.
    # Needs entity_popover_map for tooltip injection in the enrichment section.
    _populate_mj_entity_caches(pages, url_map, label_map, entity_popover_map)

    missing_intros = [arc for arc in ARCS if arc.num not in intros]
    if missing_intros:
        print("  ! intro post not found for arcs: " +
              ", ".join(f"{a.num} ({a.intro_title!r})" for a in missing_intros))

    print("Writing pages ...")
    # Home
    write(OUT / "index.html",
          render_home(buckets, pages_by_cat, pages, url_map, label_map))
    # Arc landing pages
    for arc in ARCS:
        b = buckets.get(arc.num)
        if b is None:
            continue
        write(OUT / f"arc-{arc.num}.html",
              render_arc_page(b, buckets, url_map, label_map))
    # Category index pages + individual pages (visible in nav)
    for out_folder, src_folder, lbl in CATEGORIES:
        cat_pages = pages_by_cat.get(src_folder, [])
        write(OUT / out_folder / "index.html",
              render_category_index(out_folder, src_folder, lbl, cat_pages,
                                    buckets))
        for pg in cat_pages:
            write(pg.out_path,
                  render_post_page(pg, pages, url_map, label_map, buckets,
                                   pages_by_session, siblings_idx,
                                   session_by_num_map, entity_popover_map))

    # Hidden folders: render individual pages but no index, no nav entry.
    # (E.g. Tomes — arc-intro pages already shown as arc page bodies.)
    for _out_folder, src_folder in HIDDEN_FOLDERS:
        for pg in pages_by_cat.get(src_folder, []):
            write(pg.out_path,
                  render_post_page(pg, pages, url_map, label_map, buckets,
                                   pages_by_session, siblings_idx,
                                   session_by_num_map, entity_popover_map))

    write(OUT / "search" / "index.html", render_search_page())
    write(OUT / "style.css", CSS + "\n\n" + MJ_CSS)
    write(OUT / "search.js", SEARCH_JS)
    if MJ_TOKEN:
        write(OUT / "mj-mode.js", MJ_MODE_JS.replace("__MJ_TOKEN__", MJ_TOKEN))
    else:
        write(OUT / "mj-mode.js", "// MJ token not configured — script inert.\n")
    render_mj_overlay(pages, buckets, url_map, label_map,
                      entity_popover_map=entity_popover_map)
    (OUT / "search-index.json").write_text(
        json.dumps(build_search_index(pages, siblings_idx), ensure_ascii=False),
        encoding="utf-8")
    write(OUT / "sitemap.xml", build_sitemap(pages))
    write(OUT / "robots.txt", ROBOTS_TXT)

    total = sum(len(v) for v in pages_by_cat.values())
    print(f"\nDone. {total} post pages + {len(ARCS)} arc pages + "
          f"{len(CATEGORIES)} category indexes + home, in {OUT}")
    print(f"Open: {(OUT / 'index.html').resolve().as_uri()}")
    return 0


def render_search_page() -> str:
    """Static shell for /search/?q=... — the JS reads ?q= and fills it in."""
    body = [
        '<section class="search-page">',
        '<h1 class="search-page-heading">Recherche</h1>',
        '<p class="search-page-query" id="search-page-query"></p>',
        '<div id="search-page-results" data-base="../"></div>',
        '</section>',
    ]
    og = OgMeta(description="Recherche dans Mon Ennemi Intérieur.",
                og_type="website", url=absolute_url("search/"))
    return layout(Path("search"), "Recherche", "\n".join(body),
                  extra_class="page-search", buckets=None, og=og)


def serve(port: int) -> int:
    """Tiny dev server for local browsing."""
    import http.server, socketserver, os
    os.chdir(OUT)
    handler = http.server.SimpleHTTPRequestHandler
    with socketserver.TCPServer(("127.0.0.1", port), handler) as httpd:
        print(f"Serving {OUT} at http://127.0.0.1:{port}/  (Ctrl-C to stop)")
        httpd.serve_forever()
    return 0


# --------------------------------------------------------------------------- #
# Deployment to GitHub Pages
# --------------------------------------------------------------------------- #

DEPLOY_REPO = "cgauche/cgauche.github.io"
DEPLOY_REPO_URL = f"https://github.com/{DEPLOY_REPO}.git"
DEPLOY_LOCAL = Path(__file__).parent.parent / "cgauche.github.io"
DEPLOY_SUBDIR = "mon-ennemi-interieur"


def _run(cmd: list[str], cwd: Path | None = None,
         check: bool = True, capture: bool = False) -> subprocess.CompletedProcess:
    if capture:
        r = subprocess.run(cmd, cwd=str(cwd) if cwd else None,
                           check=False, capture_output=True, text=True,
                           encoding="utf-8")
    else:
        r = subprocess.run(cmd, cwd=str(cwd) if cwd else None, check=False)
    if check and r.returncode != 0:
        msg = r.stderr if capture else ""
        raise RuntimeError(f"command failed: {' '.join(cmd)}\n{msg}")
    return r


def deploy(no_push: bool = False) -> int:
    """Clone or update the gh-pages repo, sync _site/ into the subdir,
    commit, and push. Idempotent and safe to re-run."""
    if not OUT.exists():
        print(f"error: {OUT} doesn't exist — run a build first", file=sys.stderr)
        return 2

    print(f"\n--- Deploying to {DEPLOY_REPO} (subdir: {DEPLOY_SUBDIR}/) ---")

    # 1. Clone or pull
    if not (DEPLOY_LOCAL / ".git").exists():
        print(f"  cloning {DEPLOY_REPO_URL} → {DEPLOY_LOCAL}")
        try:
            _run(["git", "clone", DEPLOY_REPO_URL, str(DEPLOY_LOCAL)])
        except RuntimeError as e:
            print(f"clone failed: {e}", file=sys.stderr)
            return 1
    else:
        print(f"  pulling latest in {DEPLOY_LOCAL}")
        try:
            _run(["git", "pull", "--ff-only"], cwd=DEPLOY_LOCAL)
        except RuntimeError as e:
            print(f"pull failed: {e}", file=sys.stderr)
            return 1

    # 2. Wipe the subdir then copy fresh content
    target = DEPLOY_LOCAL / DEPLOY_SUBDIR
    if target.exists():
        for child in target.iterdir():
            if child.is_dir():
                shutil.rmtree(child)
            else:
                child.unlink()
    else:
        target.mkdir(parents=True)

    print(f"  syncing _site/ → {DEPLOY_SUBDIR}/")
    for item in OUT.iterdir():
        dst = target / item.name
        if item.is_dir():
            shutil.copytree(item, dst)
        else:
            shutil.copy2(item, dst)

    # 3. Stage everything in the subdir (handles adds + removes)
    _run(["git", "add", "-A", DEPLOY_SUBDIR], cwd=DEPLOY_LOCAL)
    status = _run(["git", "status", "--porcelain", DEPLOY_SUBDIR],
                  cwd=DEPLOY_LOCAL, capture=True)

    if status.stdout.strip():
        counts = {"A": 0, "M": 0, "D": 0}
        for line in status.stdout.splitlines():
            code = line[:2].strip()
            if code.startswith("A"):
                counts["A"] += 1
            elif code.startswith("M"):
                counts["M"] += 1
            elif code.startswith("D"):
                counts["D"] += 1
        print(f"  changes: +{counts['A']}  ~{counts['M']}  -{counts['D']}")

        msg = "Update Mon Ennemi Intérieur — " + datetime.now().strftime("%Y-%m-%d %H:%M")
        _run(["git", "commit", "-m", msg], cwd=DEPLOY_LOCAL)
    else:
        print("  no file changes since last build.")

    # 4. Push any unpushed commits (covers both the new commit and any
    #    leftover commit from a previous failed push).
    ahead = _run(["git", "rev-list", "--count", "@{u}..HEAD"],
                 cwd=DEPLOY_LOCAL, capture=True)
    pending = int(ahead.stdout.strip() or "0")
    if pending == 0:
        print("  ✓ already in sync with origin.")
        return 0

    if no_push:
        print(f"\n  {pending} commit(s) ready, not pushed (--deploy-no-push)")
        print(f"  push manually: git -C {DEPLOY_LOCAL} push")
        return 0

    print(f"  pushing {pending} commit(s) to {DEPLOY_REPO_URL}")
    try:
        _run(["git", "push"], cwd=DEPLOY_LOCAL)
    except RuntimeError as e:
        print(f"push failed: {e}", file=sys.stderr)
        return 1

    print(f"\n  ✓ deployed → https://cgauche.github.io/{DEPLOY_SUBDIR}/")
    return 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--clean", action="store_true",
                    help="remove _site/ before rebuilding")
    ap.add_argument("--serve", type=int, metavar="PORT", default=0,
                    help="rebuild then serve locally on PORT")
    ap.add_argument("--deploy", action="store_true",
                    help="rebuild then deploy to cgauche.github.io")
    ap.add_argument("--deploy-no-push", action="store_true",
                    help="like --deploy but stop before pushing")
    args = ap.parse_args(argv)

    rc = build(clean=args.clean)
    if rc != 0:
        return rc

    if args.deploy or args.deploy_no_push:
        rc = deploy(no_push=args.deploy_no_push)
        if rc != 0:
            return rc

    if args.serve:
        return serve(args.serve)
    return 0


if __name__ == "__main__":
    sys.exit(main())
