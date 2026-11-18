"""The documents docs_search knows about, and how they are split into searchable chunks.

Sources are pinned to a release so every run (and every benchmark experiment) searches
exactly the same text. Adding a source is one entry in SOURCES.
"""

import re
import urllib.request
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class Source:
    id: str
    library: str
    url: str  # raw source file, pinned to a tag
    docs_url: str  # the rendered page, for links back to a section
    format: str = "markdown"  # "markdown" or "rst"


SQLA = "https://raw.githubusercontent.com/sqlalchemy/sqlalchemy/rel_2_0_58/doc/build/changelog"

SOURCES = [
    Source(
        id="pydantic-v2-migration",
        library="pydantic",
        url="https://raw.githubusercontent.com/pydantic/pydantic/v2.14.1/docs/migration.md",
        docs_url="https://docs.pydantic.dev/2.14/migration/",
    ),
    # SQLAlchemy splits its 2.0 docs into the migration guide (what to change, step by step) and
    # "what's new" (behaviour changes). Both are reStructuredText.
    Source(
        id="sqlalchemy-20-migration",
        library="sqlalchemy",
        url=f"{SQLA}/migration_20.rst",
        docs_url="https://docs.sqlalchemy.org/en/20/changelog/migration_20.html",
        format="rst",
    ),
    Source(
        id="sqlalchemy-20-whatsnew",
        library="sqlalchemy",
        url=f"{SQLA}/whatsnew_20.rst",
        docs_url="https://docs.sqlalchemy.org/en/20/changelog/whatsnew_20.html",
        format="rst",
    ),
]


@dataclass(frozen=True)
class Chunk:
    id: str
    source: str
    library: str
    title: str  # heading path, e.g. "Migration guide › Changes to config"
    url: str  # rendered docs URL with the section's anchor
    text: str


HEADING = re.compile(r"^(#{1,4})\s+(.+?)\s*#*\s*$")
FENCE = re.compile(r"^\s*(```|~~~)")


def slugify(heading: str) -> str:
    """Approximate the anchor mkdocs generates for a heading."""
    text = re.sub(r"[`*_]", "", heading).lower()
    text = re.sub(r"[^\w\s-]", "", text)
    return re.sub(r"[\s]+", "-", text).strip("-")


RST_ADORNMENT = re.compile(r"""^([=\-~^"'`#*+:.<>_])\1{2,}\s*$""")


def rst_slug(heading: str) -> str:
    """Approximate the section id Sphinx generates ("Step Six - Add ``__allow_unmapped__``"
    -> "step-six-add-allow-unmapped")."""
    return re.sub(r"[^a-z0-9]+", "-", heading.lower()).strip("-")


def chunk_document(text: str, source: Source, max_chars: int = 2500) -> list[Chunk]:
    """Chunk a source in its own format (markdown or reStructuredText)."""
    if source.format == "rst":
        return _chunks(_rst_sections(text), source, max_chars, rst_slug)
    return chunk_markdown(text, source, max_chars)


def _rst_sections(text: str) -> list[tuple[list[str], list[str]]]:
    """Split reStructuredText at its section titles: a title line followed by an underline of
    punctuation at least as long (optionally with a matching overline). As in docutils, nesting
    depth follows the order in which each adornment style first appears. RST code is indented,
    so code lines can't be mistaken for titles."""
    lines = text.splitlines()
    styles: list[tuple[str, bool]] = []  # adornment styles, in order of first appearance
    sections: list[tuple[list[str], list[str]]] = []
    stack: list[tuple[int, str]] = []
    body: list[str] = []
    i = 0
    while i < len(lines):
        line = lines[i]
        under = RST_ADORNMENT.match(lines[i + 1]) if i + 1 < len(lines) else None
        is_title = (under is not None and line.strip() and not line[0].isspace()
                    and not RST_ADORNMENT.match(line) and len(lines[i + 1].rstrip()) >= len(line.rstrip()))
        if not is_title:
            body.append(line)
            i += 1
            continue
        overline = bool(body) and body[-1].strip() == lines[i + 1].strip()
        if overline:
            body.pop()  # the overline belongs to this title, not to the previous section
        if any(l.strip() for l in body):
            sections.append(([h for _, h in stack], body))
        style = (under[1], overline)
        if style not in styles:
            styles.append(style)
        level = styles.index(style)
        while stack and stack[-1][0] >= level:
            stack.pop()
        stack.append((level, line.strip()))
        body = []
        i += 2
    if any(l.strip() for l in body):
        sections.append(([h for _, h in stack], body))
    return sections


def chunk_markdown(markdown: str, source: Source, max_chars: int = 2500) -> list[Chunk]:
    """Split a markdown document at its headings, ignoring '#' lines inside code blocks
    (the Pydantic guide's examples are full of '# V1' / '# V2' comments).

    Each chunk carries its heading path, so a hit reads as a location in the guide, not a
    loose paragraph. Sections longer than max_chars are split at paragraph boundaries.
    """
    sections: list[tuple[list[str], list[str]]] = []  # (heading path, lines)
    stack: list[tuple[int, str]] = []  # (level, heading) of the current section and its parents
    lines: list[str] = []
    in_code = False
    for line in markdown.splitlines():
        if FENCE.match(line):
            in_code = not in_code
        match = None if in_code else HEADING.match(line)
        if match:
            if any(l.strip() for l in lines):
                sections.append(([h for _, h in stack], lines))
            level = len(match[1])
            # Pop back to this heading's parent. A stack (not path[:level-1]) handles documents
            # that start at "##" with no "#" title, like the Pydantic migration guide.
            while stack and stack[-1][0] >= level:
                stack.pop()
            stack.append((level, match[2].strip()))
            lines = []
        else:
            lines.append(line)
    if any(l.strip() for l in lines):
        sections.append(([h for _, h in stack], lines))
    return _chunks(sections, source, max_chars, slugify)


def _chunks(sections: list[tuple[list[str], list[str]]], source: Source, max_chars: int, slug) -> list[Chunk]:
    """Turn (heading path, body lines) sections into chunks, splitting long sections."""
    chunks = []
    for path, body in sections:
        title = " › ".join(path) or source.id
        anchor = f"#{slug(path[-1])}" if path else ""
        for i, piece in enumerate(_split(("\n".join(body)).strip(), max_chars)):
            chunks.append(Chunk(
                id=f"{source.id}:{len(chunks)}", source=source.id, library=source.library,
                title=title + (f" (part {i + 1})" if i else ""), url=source.docs_url + anchor,
                text=piece,
            ))
    return chunks


def _split(text: str, max_chars: int) -> list[str]:
    """Split at blank lines into pieces of at most ~max_chars, never inside a code block."""
    if len(text) <= max_chars:
        return [text]
    blocks, current, in_code = [], [], False
    for line in text.splitlines():
        if FENCE.match(line):
            in_code = not in_code
        current.append(line)
        if not in_code and line.strip() == "" and sum(len(l) + 1 for l in current) >= max_chars * 0.6:
            blocks.append("\n".join(current).strip())
            current = []
    if current:
        blocks.append("\n".join(current).strip())
    pieces, piece = [], ""
    for block in blocks:
        if piece and len(piece) + len(block) > max_chars:
            pieces.append(piece)
            piece = block
        else:
            piece = f"{piece}\n\n{block}" if piece else block
    if piece:
        pieces.append(piece)
    return pieces


def download(source: Source, cache_dir: Path) -> str:
    """The source's text, downloaded once into cache_dir."""
    path = cache_dir / f"{source.id}.{'rst' if source.format == 'rst' else 'md'}"
    if not path.is_file():
        cache_dir.mkdir(parents=True, exist_ok=True)
        with urllib.request.urlopen(source.url, timeout=30) as response:
            path.write_bytes(response.read())
    return path.read_text(encoding="utf-8")
