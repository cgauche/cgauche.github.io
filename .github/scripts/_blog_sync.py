#!/usr/bin/env python3
"""
_blog_sync.py — Synchronize local notes with monennemiinterieur.blogspot.com.

Mirrors the blog into "Mon Ennemi Intérieur Blog/" :
  - new post on blog     → new local file
  - post updated on blog → local file overwritten
  - post deleted on blog → local file deleted
  - cross-post links rewritten to relative local paths
  - sub-folder indexes (00 - Index.md) regenerated at the end

Touches ONLY these sub-folders (Notes MJ/ is GM-authored, left alone):
    Résumés/, PJ/, PNJ/, Lieux/, Documents/, Annexes/

Classification strategy:
  1. Primary — URL match. Each existing local file starts with
     "*Source : <blog-url>*". We map every existing local URL → path; any
     blog post with a matching URL keeps its existing folder/filename.
  2. Secondary (new posts only) — heuristic:
       - title starts with "NN)"  → Résumés/
       - otherwise we DO NOT auto-create. The post is listed under
         "uncategorized new posts" so the GM can decide where it belongs.

Safety: if classification looks broken (e.g. lots of existing files
would be orphaned at once), the script aborts before any write. Override
with --force when you really mean it.

Usage:
    python _blog_sync.py                  # full sync, write changes
    python _blog_sync.py --dry-run        # show what would change, no writes
    python _blog_sync.py --folders Résumés,PJ      # restrict to subset
    python _blog_sync.py --no-delete      # never delete orphan local files
    python _blog_sync.py --force          # bypass the safety abort

Dependencies: requests, beautifulsoup4, markdownify
    python -m pip install requests beautifulsoup4 markdownify
"""

from __future__ import annotations

import argparse
import re
import sys
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urlparse, unquote

import requests
from bs4 import BeautifulSoup
from markdownify import markdownify as html_to_md

# --------------------------------------------------------------------------- #
# Configuration
# --------------------------------------------------------------------------- #

BLOG_HOST = "monennemiinterieur.blogspot.com"
BLOG_URL = f"https://{BLOG_HOST}"
ATOM_URL = f"{BLOG_URL}/feeds/posts/default"
ROOT = Path(__file__).parent / "Mon Ennemi Intérieur Blog"
ALL_FOLDERS = ["Résumés", "PJ", "PNJ", "Lieux", "Documents", "Univers", "Tomes"]

# Windows-illegal filename characters. Square brackets are stripped separately
# (titles sometimes wrap a name in brackets; existing filenames don't have them).
_FS_ILLEGAL = re.compile(r'[<>:"/\\|?*\x00-\x1f]')

# Match the "NN)" leading number in Résumés post titles (e.g. "62) Le conseil impérial")
_RESUME_NUM = re.compile(r"^\s*(\d{1,3})\s*[\)\.]\s*(.+)$")

# Pattern used in existing .md files to record the source URL.
_SOURCE_LINE = re.compile(r"^\*Source\s*:\s*\[([^\]]+)\]")

# Matches "/search/label/<name>" — Blogger's tag-search URL, used by
# in-content links that point to a character/place by name.
_SEARCH_LABEL = re.compile(r"^/search/label/(.+)$")


# --------------------------------------------------------------------------- #
# Data model
# --------------------------------------------------------------------------- #


def setup_utf8_stdout() -> None:
    """Force UTF-8 on stdout/stderr so accented paths print on Windows."""
    try:
        sys.stdout.reconfigure(encoding="utf-8")
        sys.stderr.reconfigure(encoding="utf-8")
    except (AttributeError, OSError):
        pass


@dataclass
class Post:
    """A normalised view of a Blogger Atom <entry>."""
    blog_url: str            # canonical post URL on the blog
    title: str               # post title as published
    labels: list[str]        # blog labels (categories)
    html: str                # post HTML content
    published: str           # ISO timestamp from <published>
    updated: str             # ISO timestamp from <updated>

    # Filled in by classify():
    folder: str | None = None       # local folder name, or None if unclassified
    filename: str | None = None     # final filename (with .md)


@dataclass
class SyncPlan:
    create:    list[tuple[Path, str]] = field(default_factory=list)
    overwrite: list[tuple[Path, str]] = field(default_factory=list)
    unchanged: list[Path] = field(default_factory=list)
    delete:    list[Path] = field(default_factory=list)
    new_unclassified: list[Post] = field(default_factory=list)


# --------------------------------------------------------------------------- #
# Atom feed → list[Post]
# --------------------------------------------------------------------------- #


def fetch_all_posts(session: requests.Session) -> list[Post]:
    posts: list[Post] = []
    start_index = 1
    page_size = 150
    while True:
        params = {"alt": "atom", "max-results": page_size, "start-index": start_index}
        r = session.get(ATOM_URL, params=params, timeout=30)
        r.raise_for_status()
        soup = BeautifulSoup(r.text, "xml")
        entries = soup.find_all("entry")
        if not entries:
            break
        for entry in entries:
            posts.append(_entry_to_post(entry))
        if len(entries) < page_size:
            break
        start_index += page_size
    return posts


def _entry_to_post(entry) -> Post:
    title = (entry.find("title").get_text() or "").strip()
    content = entry.find("content")
    html = content.get_text() if content else ""
    published = entry.find("published").get_text() if entry.find("published") else ""
    updated = entry.find("updated").get_text() if entry.find("updated") else ""

    blog_url = ""
    for link in entry.find_all("link"):
        if link.get("rel") == "alternate" and link.get("type") == "text/html":
            blog_url = link.get("href", "")
            break

    labels = [c.get("term", "") for c in entry.find_all("category") if c.get("term")]
    return Post(blog_url=blog_url, title=title, labels=labels,
                html=html, published=published, updated=updated)


# --------------------------------------------------------------------------- #
# Filesystem index — read existing files to learn current URL→path mapping
# --------------------------------------------------------------------------- #


def _normalise_url(u: str) -> str:
    """Compare URLs by host+path only (ignore scheme/query/fragment).

    Path is lowercased; safe because Blogger emits only lowercase URLs, but
    would lose information against a case-sensitive server."""
    parsed = urlparse(u)
    return (parsed.netloc.lower() + parsed.path.rstrip("/").lower())


def read_existing_index(folders: list[str]) -> dict[str, Path]:
    """Walk the tracked folders and return {normalised_blog_url: path}."""
    out: dict[str, Path] = {}
    for folder in folders:
        d = ROOT / folder
        if not d.exists():
            continue
        for md in d.glob("*.md"):
            if md.name == "00 - Index.md":
                continue
            url = _extract_source_url(md)
            if url:
                out[_normalise_url(url)] = md
    return out


def _extract_source_url(path: Path) -> str | None:
    # The Source line is on line 3 of every well-formed file. Read a few lines.
    try:
        with path.open("r", encoding="utf-8") as f:
            for _ in range(5):
                line = f.readline()
                if not line:
                    return None
                m = _SOURCE_LINE.match(line.strip())
                if m:
                    return m.group(1)
    except OSError:
        return None
    return None


# --------------------------------------------------------------------------- #
# Filename helpers
# --------------------------------------------------------------------------- #


def sanitize_filename(name: str) -> str:
    cleaned = _FS_ILLEGAL.sub("", name)
    # Strip square brackets — existing filenames don't preserve them.
    cleaned = cleaned.replace("[", "").replace("]", "")
    cleaned = re.sub(r"\s+", " ", cleaned).strip().rstrip(".")
    return cleaned or "untitled"


def resume_filename(title: str) -> tuple[str, int] | None:
    """If title looks like 'NN) something', return (filename, num)."""
    m = _RESUME_NUM.match(title)
    if not m:
        return None
    num = int(m.group(1))
    rest = sanitize_filename(m.group(2).strip())
    return f"{num:02d} - {num:02d}) {rest}.md", num


# --------------------------------------------------------------------------- #
# Classification
# --------------------------------------------------------------------------- #


# The blog encodes content type via the Atom <published> year — the GM
# back-dated each post to a year reserved for one category:
#   2018 → Annexes    2021 → Lieux       2023 → PJ (main bios)
#   2019 → Documents  2022 → PJ variants 2024+ → Résumés / arc-intro
#   2020 → PNJ
PUBLISHED_YEAR_FOLDER: dict[str, str] = {
    "2018": "Univers",
    "2019": "Documents",
    "2020": "PNJ",
    "2021": "Lieux",
    "2022": "PJ",
    "2023": "PJ",
}

# Titles that are Blogger navigation / divider pages, not real content.
# They appear in the Atom feed but shouldn't be synced as .md files.
_NAV_TITLES = {
    "", "*", "$", "^",
    "Lieux", "PJ", "PJs", "PNJ", "PNJs",
    "Documents", "Annexes", "Résumés",
    "Personnages joueurs", "Personnes non-joueurs",
}


def target_folder_for(post: Post) -> str | None:
    """Return the canonical folder for a post based on its published year.
    2024+ posts are split between Résumés and Annexes by title pattern.
    Generic navigation/divider posts return None (not synced)."""
    if not post.published:
        return None
    title = (post.title or "").strip()
    if not title or title in _NAV_TITLES:
        return None
    year = post.published[:4]
    target = PUBLISHED_YEAR_FOLDER.get(year)
    if target is not None:
        return target
    if year >= "2024":
        # 2024+ : NN) titles = session recaps, everything else = tome / arc
        # presentation page (used as arc body, not surfaced in nav).
        return "Résumés" if _RESUME_NUM.match(title) else "Tomes"
    return None


def classify(posts: list[Post], existing: dict[str, Path],
             folders: list[str]) -> None:
    """Classify every post by its <published> year (the blog's convention).

    Two-pass:
      1. Decide each post's target folder from the year. If an existing local
         file is in the SAME folder, reuse its filename for stability;
         otherwise compute a fresh filename.
      2. Within each folder, resolve same-title collisions with (2)/(3)/...
         suffixes, earliest publication date keeps the bare name.
    """
    # Pass 1 — folder + base filename
    by_folder: dict[str, list[Post]] = {}
    for p in posts:
        target = target_folder_for(p)
        if target is None or target not in folders:
            # No classification — surfaced as `new_unclassified` for the GM
            # to triage manually (see build_plan).
            continue

        p.folder = target
        match = existing.get(_normalise_url(p.blog_url))
        if match is not None and match.parent.name == target:
            # Existing file already in the right folder — keep its filename
            p.filename = match.name
        else:
            # New post, or file is moving folders — generate a fresh filename
            if target == "Résumés":
                r = resume_filename(p.title)
                p.filename = r[0] if r else sanitize_filename(p.title) + ".md"
            else:
                p.filename = sanitize_filename(p.title) + ".md"
        by_folder.setdefault(target, []).append(p)

    # Pass 2 — resolve collisions inside each folder (earliest published = bare)
    for folder, posts_in in by_folder.items():
        posts_in.sort(key=lambda p: p.published or "zzz")
        used: dict[str, int] = {}
        for p in posts_in:
            base = re.sub(r" \(\d+\)\.md$", ".md", p.filename or "")
            n = used.get(base, 0)
            used[base] = n + 1
            if n > 0:
                # Insert " (N)" before .md
                stem = base[:-3] if base.endswith(".md") else base
                p.filename = f"{stem} ({n + 1}).md"
            else:
                p.filename = base


# --------------------------------------------------------------------------- #
# HTML → Markdown body, with internal link rewriting
# --------------------------------------------------------------------------- #


def build_url_map(posts: list[Post]) -> dict[str, Path]:
    out: dict[str, Path] = {}
    for p in posts:
        if p.folder and p.filename:
            out[_normalise_url(p.blog_url)] = Path(p.folder) / p.filename
    return out


def build_label_map(posts: list[Post]) -> dict[str, Path]:
    """Map blog tag names (decoded) → local path of the best-matching post.

    Bare-name posts (no '(2)/(3)' suffix) win over variants — this matches
    the original polish-script behaviour where, e.g., `/search/label/Phineas`
    rewrites to `PJ/Phineas.md`, not `PJ/Phineas, artiste.md`.
    """
    def bareness(p: Post) -> int:
        return 0 if (p.filename and "(" not in p.filename) else 1

    out: dict[str, Path] = {}
    for p in sorted(posts, key=bareness):
        if not (p.folder and p.filename):
            continue
        local = Path(p.folder) / p.filename
        out.setdefault(p.title.strip(), local)
        out.setdefault(p.filename[:-3], local)  # stem
    return out


def render_post(post: Post, url_map: dict[str, Path],
                label_map: dict[str, Path]) -> str:
    md_body = html_to_md(
        post.html,
        heading_style="ATX",
        bullets="-",
        strip=["script", "style"],
    )

    md_body = _rewrite_internal_links(md_body, post, url_map, label_map)
    md_body = _postprocess_md(md_body)

    return (
        f"# {post.title}\n"
        f"\n"
        f"*Source : [{post.blog_url}]({post.blog_url})*\n"
        f"\n"
        f"{md_body}\n"
    )


def _postprocess_md(md: str) -> str:
    """Clean markdownify output to match the original polish-script style."""
    # 1. Strip trailing regular spaces and tabs only — NOT non-breaking space
    #    (\xa0), which the blog uses inside paragraphs and which the original
    #    polish script preserved.
    lines = [ln.rstrip(" \t") for ln in md.splitlines()]
    # 2. Collapse runs of blank lines to at most one.
    out: list[str] = []
    prev_blank = False
    for ln in lines:
        if ln == "":
            if not prev_blank:
                out.append("")
            prev_blank = True
        else:
            out.append(ln)
            prev_blank = False
    md = "\n".join(out).strip()
    # 3. Normalise escaped/literal horizontal rules to "---".
    md = re.sub(r"^\s*\\\*\\\*\\\*\s*$", "---", md, flags=re.MULTILINE)
    md = re.sub(r"^\s*\*{3,}\s*$",       "---", md, flags=re.MULTILINE)
    # 4. Ensure a blank line precedes "---" (matches the original polish style).
    lines = md.split("\n")
    out = []
    for ln in lines:
        if ln.strip() == "---" and out and out[-1].strip() != "":
            out.append("")
        out.append(ln)
    return "\n".join(out)


def _rewrite_internal_links(md: str, current_post: Post,
                            url_map: dict[str, Path],
                            label_map: dict[str, Path]) -> str:
    current_dir = Path(current_post.folder) if current_post.folder else Path(".")

    def resolve(url_clean: str) -> Path | None:
        parsed = urlparse(url_clean)
        if parsed.netloc and parsed.netloc.lower() != BLOG_HOST:
            return None
        # 1. Direct post URL → local file
        local = url_map.get(_normalise_url(url_clean))
        if local is not None:
            return local
        # 2. /search/label/<name> → look up label in label_map
        m = _SEARCH_LABEL.match(parsed.path)
        if m:
            name = unquote(m.group(1)).strip()
            return label_map.get(name)
        return None

    def repl(match: re.Match) -> str:
        label, url = match.group(1), match.group(2)
        url_clean = url.strip().lstrip("<").rstrip(">")
        local = resolve(url_clean)
        if local is None:
            return match.group(0)
        rel = relative_url(current_dir, local)
        return f"[{label}](<{rel}>)"

    pattern = re.compile(r"\[([^\]]+)\]\(([^)]+)\)")
    return pattern.sub(repl, md)


def relative_url(from_dir: Path, target: Path) -> str:
    """POSIX-style relative path from `from_dir` to `target` (both paths
    relative to a shared root). Used to rewrite cross-links between local
    notes files (sync) and between rendered HTML pages (site build)."""
    from_parts = from_dir.parts
    to_parts = target.parts
    i = 0
    while i < len(from_parts) and i < len(to_parts) - 1 and from_parts[i] == to_parts[i]:
        i += 1
    ups = [".."] * (len(from_parts) - i)
    downs = list(to_parts[i:])
    return "/".join(ups + downs) if ups else "/".join(downs)


# --------------------------------------------------------------------------- #
# Filesystem diff
# --------------------------------------------------------------------------- #


def build_plan(posts: list[Post], url_map: dict[str, Path],
               label_map: dict[str, Path],
               folders: list[str], allow_delete: bool) -> SyncPlan:
    plan = SyncPlan()
    generated: dict[Path, str] = {}

    for p in posts:
        if p.folder is None:
            plan.new_unclassified.append(p)
            continue
        if p.folder not in folders:
            # post belongs to a folder we're not syncing — leave it alone
            continue
        target = ROOT / p.folder / p.filename
        generated[target] = render_post(p, url_map, label_map)

    for target, body in generated.items():
        if not target.exists():
            plan.create.append((target, body))
        else:
            existing_body = target.read_text(encoding="utf-8")
            if existing_body == body:
                plan.unchanged.append(target)
            else:
                plan.overwrite.append((target, body))

    if allow_delete:
        expected = set(generated.keys())
        for folder in folders:
            d = ROOT / folder
            if not d.exists():
                continue
            for md in d.glob("*.md"):
                if md.name == "00 - Index.md":
                    continue
                if md not in expected:
                    plan.delete.append(md)

    return plan


def apply_plan(plan: SyncPlan, dry_run: bool) -> None:
    for path, body in plan.create:
        verb = "+ CREATE   " if dry_run else "+ created  "
        print(f"  {verb}{path.relative_to(ROOT.parent)}")
        if not dry_run:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(body, encoding="utf-8")
    for path, body in plan.overwrite:
        verb = "~ UPDATE   " if dry_run else "~ updated  "
        print(f"  {verb}{path.relative_to(ROOT.parent)}")
        if not dry_run:
            path.write_text(body, encoding="utf-8")
    for path in plan.delete:
        verb = "- DELETE   " if dry_run else "- deleted  "
        print(f"  {verb}{path.relative_to(ROOT.parent)}")
        if not dry_run:
            path.unlink()


# --------------------------------------------------------------------------- #
# Safety check
# --------------------------------------------------------------------------- #


def safety_check(plan: SyncPlan, existing: dict[str, Path], force: bool) -> str | None:
    """Return an error message if the plan looks broken, else None."""
    total_existing = len(existing)
    if total_existing == 0:
        return None  # first-ever run, nothing to lose

    would_delete = len(plan.delete)
    would_change = len(plan.create) + len(plan.overwrite) + len(plan.delete)

    # Hard rule: never delete the bulk of the local content in a single run
    # unless --force is given. 50% is an intentional very loose cap; in
    # practice an incremental update should delete 0-2 files at most.
    if would_delete > max(20, total_existing // 2) and not force:
        return (
            f"Refusing to proceed: would delete {would_delete} of "
            f"{total_existing} existing files. This usually means the "
            f"classifier is broken or the blog feed is incomplete. "
            f"Re-run with --force if you really mean it."
        )

    # If essentially nothing matched, also abort
    if would_change > 0 and len(plan.unchanged) + len(plan.overwrite) == 0 \
            and not force:
        return (
            "Refusing to proceed: no existing file matched any blog post — "
            "URL mapping is almost certainly broken. Re-run with --force "
            "to override."
        )
    return None


# --------------------------------------------------------------------------- #
# Index regeneration
# --------------------------------------------------------------------------- #


def regenerate_indexes(folders: list[str], dry_run: bool) -> None:
    folder_files: dict[str, list[Path]] = {}
    for folder in folders:
        d = ROOT / folder
        if not d.exists():
            continue
        files = sorted(p for p in d.glob("*.md") if p.name != "00 - Index.md")
        folder_files[folder] = files
        idx = d / "00 - Index.md"
        body = _render_subindex(folder, files)
        verb = "i INDEX    " if dry_run else "i index    "
        print(f"  {verb}{idx.relative_to(ROOT.parent)}  ({len(files)} entries)")
        if not dry_run:
            idx.write_text(body, encoding="utf-8")

    root_idx = ROOT / "00 - Index.md"
    body = _render_root_index(folder_files)
    verb = "i INDEX    " if dry_run else "i index    "
    print(f"  {verb}{root_idx.relative_to(ROOT.parent)}")
    if not dry_run:
        root_idx.write_text(body, encoding="utf-8")


def _render_subindex(folder: str, files: list[Path]) -> str:
    lines = [f"# {folder} — Index\n"]
    for f in files:
        lines.append(f"- [{f.stem}](<{f.name}>)")
    return "\n".join(lines) + "\n"


def _render_root_index(folder_files: dict[str, list[Path]]) -> str:
    lines = ["# Mon Ennemi Intérieur - Index complet\n"]
    for folder in ALL_FOLDERS:
        files = folder_files.get(folder)
        if files is None:
            continue
        lines.append(f"\n## {folder} ({len(files)})\n")
        for f in files:
            lines.append(f"- [{f.stem}](<{folder}/{f.name}>)")
    return "\n".join(lines) + "\n"


# --------------------------------------------------------------------------- #
# Entrypoint
# --------------------------------------------------------------------------- #


def main(argv: list[str] | None = None) -> int:
    setup_utf8_stdout()

    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--dry-run", action="store_true",
                    help="don't write or delete anything, just report")
    ap.add_argument("--folders", default=",".join(ALL_FOLDERS),
                    help=f"comma-separated subset of {ALL_FOLDERS}")
    ap.add_argument("--no-delete", action="store_true",
                    help="don't delete orphan local files even if blog post is gone")
    ap.add_argument("--force", action="store_true",
                    help="bypass safety abort (large delete count / no URL matches)")
    args = ap.parse_args(argv)

    folders = [f.strip() for f in args.folders.split(",") if f.strip()]
    unknown = [f for f in folders if f not in ALL_FOLDERS]
    if unknown:
        print(f"error: unknown folder(s): {unknown}", file=sys.stderr)
        return 2

    if not ROOT.exists():
        print(f"error: blog root not found: {ROOT}", file=sys.stderr)
        return 2

    print(f"Reading existing files in {', '.join(folders)} ...")
    existing = read_existing_index(folders)
    print(f"  -> {len(existing)} files indexed by source URL")

    print(f"\nFetching blog posts from {BLOG_URL} ...")
    with requests.Session() as s:
        s.headers["User-Agent"] = "blog-sync/1.0"
        posts = fetch_all_posts(s)
    print(f"  -> {len(posts)} posts received")

    classify(posts, existing, folders)
    url_map = build_url_map(posts)
    label_map = build_label_map(posts)

    matched = sum(1 for p in posts if p.folder is not None)
    print(f"\nClassification: {matched}/{len(posts)} posts assigned a folder")
    print(f"  - via existing URL match: "
          f"{sum(1 for p in posts if _normalise_url(p.blog_url) in existing)}")
    print(f"  - via 'NN)' heuristic:    "
          f"{sum(1 for p in posts if p.folder == 'Résumés' and _normalise_url(p.blog_url) not in existing)}")

    plan = build_plan(posts, url_map, label_map, folders,
                      allow_delete=not args.no_delete)

    print(f"\nPlan ({'DRY RUN' if args.dry_run else 'will write'}):")
    print(f"  create:    {len(plan.create)}")
    print(f"  overwrite: {len(plan.overwrite)}")
    print(f"  unchanged: {len(plan.unchanged)}")
    print(f"  delete:    {len(plan.delete)}")
    print(f"  new unclassified: {len(plan.new_unclassified)}")
    print()

    err = safety_check(plan, existing, args.force)
    if err:
        print(f"ABORTING — {err}", file=sys.stderr)
        return 3

    apply_plan(plan, dry_run=args.dry_run)

    if plan.create or plan.overwrite or plan.delete:
        print("\nRegenerating indexes ...")
        regenerate_indexes(folders, dry_run=args.dry_run)

    if plan.new_unclassified:
        print(f"\nNew uncategorized posts ({len(plan.new_unclassified)}) — "
              f"create their .md manually then re-run to sync:")
        for p in plan.new_unclassified[:30]:
            print(f"  · {p.title!r}  labels={p.labels}  {p.blog_url}")
        if len(plan.new_unclassified) > 30:
            print(f"  ... and {len(plan.new_unclassified) - 30} more")

    print("\nDone.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
