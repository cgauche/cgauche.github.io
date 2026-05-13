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
from urllib.parse import urlparse, unquote

import requests

# Reuse the Atom fetcher, classifier and shared helpers from the sync script.
from _blog_sync import (
    fetch_all_posts, read_existing_index, classify,
    _normalise_url, _SEARCH_LABEL, BLOG_HOST,
    ALL_FOLDERS, ROOT, Post,
    relative_url, setup_utf8_stdout,
)

OUT = Path(__file__).parent / "_site"

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
    """Standard session card (thumbnail + 'Session NN' eyebrow + short title)."""
    return (
        f'<li><a class="thumb-card session-card" href="{html.escape(href)}">'
        f'{_thumb_html(pg)}'
        f'<div class="thumb-card-body">'
        f'<span class="snum">Session {pg.session_num:02d}</span>'
        f'<span class="stitle">{html.escape(session_short_title(pg.post.title))}</span>'
        f'</div></a></li>')


def session_link_html(pg: 'Page', href: str) -> str:
    """Compact session line (used in apparitions and similar lists)."""
    return (
        f'<li><a href="{html.escape(href)}">'
        f'<span class="snum">Session {pg.session_num:02d}</span>'
        f'<span class="stitle">{html.escape(session_short_title(pg.post.title))}</span>'
        f'</a></li>')


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
        pages.append(Page(post=p, out_path=out_path, site_rel=site_rel,
                          slug=slug, session_num=num,
                          thumbnail=extract_first_image(p.html)))
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
        if pg.variant_group and pg.is_main:
            for sib in siblings_idx.get((pg.post.folder, pg.variant_group), []):
                if sib.site_rel == pg.site_rel:
                    continue
                variant_titles_norm.append(normalise_for_search(sib.post.title or ""))
                excerpt_parts.append(sib.post.title or "")
                excerpt_parts.append(strip_html(sib.post.html)[:500])

        haystack = " ".join(excerpt_parts + [
            FOLDER_TO_LABEL.get(pg.post.folder, "") if pg.post.folder else "",
        ])
        entry: dict = {
            "t": pg.post.title or "",
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

    # Step 4: real groups have 2+ members; pick main = most recently published.
    for (folder, label), group_pages in by_label.items():
        if len(group_pages) < 2:
            continue
        main = max(group_pages, key=lambda p: p.post.published or '')
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
    indexPromise = fetch(base + 'search-index.json')
      .then(function (r) { return r.json(); })
      .then(function (data) { index = data; return data; });
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
      return '<a class="search-result" href="' + escapeHtml(base + e.u) + '">' +
             thumb +
             '<span class="search-meta"><span class="search-title">' + escapeHtml(e.t) + '</span>' + cat + '</span>' +
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
    var img = e.target;
    if (!img || img.tagName !== 'IMG') return;
    var anchor = img.closest('a');
    if (!anchor) return;
    var href = anchor.getAttribute('href');
    if (!href || !IMG_EXT.test(href)) return;
    e.preventDefault();
    openLightbox(href, img.getAttribute('alt'));
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
           og: OgMeta | None = None) -> str:
    """Wrap a body fragment in the site shell with sidebar nav."""
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

    sidebar = _render_sidebar(current_dir, buckets, active_arc)
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
  <nav class="site-nav">{top_nav}</nav>
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
                thumb = _thumb_html(pg)
                block.append(
                    f'<li><a class="thumb-card entry-card" href="{href}">'
                    f'{thumb}'
                    f'<div class="thumb-card-body">'
                    f'<span class="entry-name">{html.escape(pg.post.title)}</span>'
                    f'</div></a></li>')
            block.append('</ul>')
        block.append('</section>')
        cat_blocks.append("\n".join(block))

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
            thumb = _thumb_html(pg)
            body.append(
                f'<li><a class="thumb-card entry-card" href="{pg.slug}.html">'
                f'{thumb}'
                f'<div class="thumb-card-body">'
                f'<span class="entry-name">{html.escape(pg.post.title)}</span>'
                f'</div></a></li>')
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
                     session_by_num_map: dict[int, Page]) -> str:
    # Rewrite internal links in the HTML body
    body_html = rewrite_html_links(pg.post.html, pg, url_map, label_map)

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
                         aggregate_variants: bool) -> str:
        """Render an 'Apparitions' section for `target` (PNJ/PJ/Lieu/Doc/Annexe).
        If `aggregate_variants` is True, union session labels across all
        siblings (so the canonical Boris page covers all his arcs in one
        sweep). For PJ — where each variant displays its own bio + its own
        apparitions just below — set to False so each block is self-contained.
        """
        if target.session_num is not None:
            return ""
        if target.post.folder not in ENTITY_FOLDERS:
            return ""

        label_sources: list[list[str]] = [target.post.labels]
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

    # Main page apparitions:
    #   - PJ : not aggregated (each variant carries its own list further down)
    #   - PNJ / Lieux / Doc / Annexe : aggregated (no per-variant rendering)
    aggregate = pg.post.folder != "PJ"
    main_app = appearances_html(pg, pg.site_rel.parent, aggregate)
    if main_app:
        parts.append(main_app)

    # For PJ pages only: render each variant as a full inline bio after the
    # apparitions section. (PNJ / Lieux variants are reachable via session
    # links in Apparitions, no extra cards needed.)
    if pg.variant_group and pg.post.folder == "PJ":
        all_siblings = siblings_for(pg, siblings_idx)
        siblings = [s for s in all_siblings if s.site_rel != pg.site_rel]
        if siblings:
            parts.append('<div class="variants-bios">')
            for it in siblings:
                body_html_v = rewrite_html_links(it.post.html, it,
                                                 url_map, label_map)
                # Self-anchor: clicking the variant title updates the URL
                # bar without navigating elsewhere (useful for sharing).
                role = ('<span class="variant-flag">Version principale</span>'
                        if it.is_main else '')
                parts.append(f'<article class="variant-bio" id="variant-{html.escape(it.slug)}">')
                parts.append(f'<h3 class="variant-bio-title">'
                             f'<a href="#variant-{html.escape(it.slug)}">'
                             f'{html.escape(it.post.title)}</a>'
                             f'{role}</h3>')
                parts.append(f'<div class="variant-bio-body">{body_html_v}</div>')
                # Each variant gets its OWN apparitions list (its own labels)
                variant_app = appearances_html(it, pg.site_rel.parent,
                                                aggregate_variants=False)
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
                thumb = _thumb_html(it)
                rel_blocks.append(
                    f'<li><a class="thumb-card entry-card" href="{href}">'
                    f'{thumb}'
                    f'<div class="thumb-card-body">'
                    f'<span class="entry-name">{html.escape(it.post.title)}</span>'
                    f'</div></a></li>')
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
.card-grid-compact .thumb-wrap { aspect-ratio: 1 / 1; }
.card-grid-compact .entry-name { font-size: 0.85rem; }
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
.variant-bio { border-top: 1px solid var(--rule); padding-top: 1.8rem;
               overflow: hidden; /* contain the float */ }
.variant-bio:first-child { border-top: 0; padding-top: 0; }
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
                     color: var(--ink-soft); }
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
                                   session_by_num_map))

    # Hidden folders: render individual pages but no index, no nav entry.
    # (E.g. Tomes — arc-intro pages already shown as arc page bodies.)
    for _out_folder, src_folder in HIDDEN_FOLDERS:
        for pg in pages_by_cat.get(src_folder, []):
            write(pg.out_path,
                  render_post_page(pg, pages, url_map, label_map, buckets,
                                   pages_by_session, siblings_idx,
                                   session_by_num_map))

    write(OUT / "search" / "index.html", render_search_page())
    write(OUT / "style.css", CSS)
    write(OUT / "search.js", SEARCH_JS)
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
