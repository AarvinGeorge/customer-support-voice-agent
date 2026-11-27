"""What: parse an article file (front matter) and split its body into Markdown blocks.
Use when: the chunker reads data/articles/*.md (doc 07 s16 front matter; draft A s2.3 rule 1).
Inputs: raw file text and file name; a body string. Output: Article and a list of Block.
Side effects: none. Role: pure parsing with loud IngestError messages that name the file."""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import date, datetime

import yaml

from knowledge.ingest.errors import IngestError

ALLOWED_TIERS = {"all", "free", "pro", "business", "enterprise"}
REQUIRED = ("article_id", "title", "source_url", "last_edited_time", "tiers")
TIERS_SENTINEL = re.compile(r"^\x00TIERS ([^\x00]*)\x00$")
_FENCE = re.compile(r"^\s*(```|~~~)")
_LIST = re.compile(r"^\s*(?:[-*+]|\d+[.)])\s+")
_HEADING = re.compile(r"^(#{1,6})\s+(.+?)\s*#*\s*$")


@dataclass(frozen=True)
class Article:
    """What: one parsed article. Use when: passing an article to the chunker. Inputs: front matter fields
    and the raw body. Output: frozen record. Side effects: none. Role: doc 07 s16 front matter plus body."""

    article_id: str
    title: str
    source_url: str
    last_updated: str
    tiers: list[str]
    body: str
    body_first_line: int
    filename: str = ""


@dataclass(frozen=True)
class Block:
    """What: one Markdown block. Use when: the chunker groups blocks into sections.
    Inputs: kind (heading, paragraph, list, table, blockquote, code, tiers), text, first line, heading level, policy keys rendered into it.
    Output: frozen record. Side effects: none. Role: rule 1 unit; a block is never split by the chunker."""

    kind: str
    text: str
    line: int
    level: int = 0
    keys: tuple[str, ...] = ()


def parse_article(raw: str, filename: str) -> Article:
    """What: read front matter and body. Use when: loading a snapshot file. Inputs: raw text, file name.
    Output: Article. Side effects: none. Role: raises IngestError naming the file for a missing front
    matter, a missing field, an invalid tier, an article_id that differs from the file name, or an H1 in the body."""
    m = re.match(r"---\n(.*?)\n---\n(.*)\Z", raw, re.S)
    if not m:
        raise IngestError(f"{filename}: no front matter")
    try:
        fm = yaml.safe_load(m.group(1)) or {}
    except yaml.YAMLError as exc:
        raise IngestError(f"{filename}: front matter is not valid YAML: {exc}") from exc
    for key in REQUIRED:
        if fm.get(key) in (None, "", []):
            raise IngestError(f"{filename}: front matter field {key} is missing or empty")
    tiers = list(fm["tiers"])
    bad = [t for t in tiers if t not in ALLOWED_TIERS]
    if bad:
        raise IngestError(f"{filename}: tier {bad[0]} is not one of {sorted(ALLOWED_TIERS)}")
    if fm["article_id"] != filename.removesuffix(".md"):
        raise IngestError(f"{filename}: article_id {fm['article_id']} does not match the filename")
    edited = fm["last_edited_time"]
    day = edited.date().isoformat() if isinstance(edited, datetime) else (
        edited.isoformat() if isinstance(edited, date) else str(edited)[:10])
    body = m.group(2)
    first_line = raw[:m.start(2)].count("\n") + 1
    for i, line in enumerate(body.splitlines()):
        if re.match(r"# \S", line):
            raise IngestError(f"{filename}:{first_line + i}: the body must not contain an H1 (the title is in front matter)")
    return Article(article_id=fm["article_id"], title=str(fm["title"]), source_url=str(fm["source_url"]),
                   last_updated=day, tiers=tiers, body=body, body_first_line=first_line, filename=filename)


def parse_blocks(body: str, first_line: int = 1) -> list[Block]:
    """What: split Markdown into blocks. Use when: sectioning a body. Inputs: text, line number of its
    first line. Output: Blocks in order. Side effects: none. Role: rule 1. Fenced code is one block (headings
    inside it are not headings); lists, tables and blockquotes are one block each; a tiers sentinel line
    becomes a 'tiers' block."""
    lines = body.splitlines()
    blocks: list[Block] = []
    i = 0
    while i < len(lines):
        line = lines[i]
        start = first_line + i
        if not line.strip():
            i += 1
        elif (sm := TIERS_SENTINEL.match(line.strip())):
            blocks.append(Block("tiers", sm.group(1), start))
            i += 1
        elif _FENCE.match(line):
            fence = _FENCE.match(line).group(1)
            j = i + 1
            while j < len(lines) and not lines[j].strip().startswith(fence):
                j += 1
            blocks.append(Block("code", "\n".join(lines[i:j + 1]), start))
            i = j + 1
        elif (hm := _HEADING.match(line)):
            blocks.append(Block("heading", hm.group(2), start, level=len(hm.group(1))))
            i += 1
        elif line.lstrip().startswith("|"):
            j = i
            while j < len(lines) and lines[j].lstrip().startswith("|"):
                j += 1
            blocks.append(Block("table", "\n".join(lines[i:j]), start))
            i = j
        elif line.lstrip().startswith(">"):
            j = i
            while j < len(lines) and lines[j].lstrip().startswith(">"):
                j += 1
            blocks.append(Block("blockquote", "\n".join(lines[i:j]), start))
            i = j
        elif _LIST.match(line):
            j = i + 1
            while j < len(lines) and lines[j].strip() and (_LIST.match(lines[j]) or lines[j].startswith((" ", "\t"))):
                j += 1
            blocks.append(Block("list", "\n".join(lines[i:j]), start))
            i = j
        else:
            j = i + 1
            while (j < len(lines) and lines[j].strip() and not _HEADING.match(lines[j]) and not _FENCE.match(lines[j])
                   and not lines[j].lstrip().startswith(("|", ">")) and not _LIST.match(lines[j])
                   and not TIERS_SENTINEL.match(lines[j].strip())):
                j += 1
            blocks.append(Block("paragraph", "\n".join(lines[i:j]), start))
            i = j
    return blocks
