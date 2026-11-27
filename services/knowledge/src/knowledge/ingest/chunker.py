"""What: heading-aware chunker: article in, ordered Chunk records out (draft A s2.3, the 8 rules).
Use when: the ingest job chunks each article before embedding (CS-E-02.S1.T6 calls chunk_article).
Inputs: a parsed Article and the PolicyConfig. Output: list[Chunk] with ids, header, body, hashes and metadata.
Side effects: none (the tokenizer is loaded once). Role: pure function; rules in order: (1) blocks, (2) intro section,
(3) H2 body and H3 sections, (4) merge bodies under 50 tokens, (5) 150 to 400 stays whole, (6) over 500 splits at block
boundaries up to 400 and a single block over 500 fails, (7) no overlap, (8) deterministic context header."""
from __future__ import annotations

import hashlib
import re
from collections.abc import Callable
from dataclasses import dataclass, replace
from functools import lru_cache

from knowledge.ingest.errors import IngestError
from knowledge.ingest.parse import ALLOWED_TIERS, Article, Block, parse_blocks
from knowledge.ingest.render import render_policy
from knowledge.ingest.sanitize import COMMENT, is_suspect, sanitize

SMALL = 50
TARGET_MAX = 400
HARD_MAX = 500
ERROR_CODE = re.compile(r"\bE-[A-Z]{2,6}-\d{3}\b")
_TIERS_COMMENT = re.compile(r"<!--\s*tiers:\s*([A-Za-z, ]+?)\s*-->")
DOC_VERSION = 1  # the Notion snapshot carries no version; one constant until it does


@lru_cache(maxsize=1)
def _encoding():
    """What: load the cl100k_base encoding once. Use when: counting tokens. Inputs: none. Output: tiktoken
    Encoding. Side effects: first call downloads the vocabulary file if it is not cached. Role: the encoding
    text-embedding-3-small uses."""
    import tiktoken
    return tiktoken.get_encoding("cl100k_base")


def count_tokens(text: str) -> int:
    """What: number of cl100k_base tokens. Use when: sizing sections and chunks. Inputs: text.
    Output: int. Side effects: see _encoding. Role: rules 4 to 6 thresholds."""
    return len(_encoding().encode(text))


@dataclass(frozen=True)
class Chunk:
    """What: one chunk ready to embed and store. Use when: the ingest job writes rows. Inputs: fields
    named as the knowledge.chunks columns (draft A s2.4). Output: frozen record. Side effects: none.
    Role: output type of chunk_article; token_count is the body only."""

    chunk_id: str
    article_slug: str
    ordinal: int
    section_path: list[str]
    heading: str
    header: str
    body: str
    embed_text: str
    source_url: str
    tiers: list[str]
    error_codes: list[str]
    policy_keys: list[str]
    injection_suspect: bool
    token_count: int
    content_hash: str
    doc_version: int
    last_updated: str


@dataclass
class _Section:
    """What: working record for a section while rules 3 to 6 run. Use when: inside chunk_article.
    Inputs: headings, blocks, tiers, keys. Output: mutable record. Side effects: none. Role: internal."""

    h2: str | None
    h3: str | None
    kind: str  # intro | h2 | h3
    blocks: list[Block]
    tiers: list[str]
    keys: list[str]
    heading: str
    path: list[str]
    id_base: str

    def text(self) -> str:
        """What: the section body text. Use when: counting or merging. Inputs: none. Output: blocks joined by
        a blank line. Side effects: none. Role: one definition of 'body'."""
        return "\n\n".join(b.text for b in self.blocks)


def slug(text: str) -> str:
    """What: lowercase heading slug. Use when: building chunk ids. Inputs: heading text. Output: slug with
    hyphens. Side effects: none. Role: same rule as the policy_ref section slugs."""
    return re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")


def _prepare(article: Article, policy) -> list[Block]:
    """What: turn the raw body into rendered, sanitized blocks (rule 1 plus render and sanitize).
    Use when: first step of chunk_article. Inputs: article, policy. Output: blocks (headings sanitized too).
    Side effects: none. Role: tiers comments become sentinels first, then other comments are blanked with their
    line breaks kept so error line numbers stay true."""
    def tiers_sentinel(m: re.Match) -> str:
        """What: replace a tiers comment by a sentinel line. Use when: re.sub. Inputs: match. Output: text.
        Side effects: none. Role: keeps the marker through comment stripping."""
        names = [t.strip() for t in m.group(1).split(",") if t.strip()]
        bad = [t for t in names if t not in ALLOWED_TIERS]
        if bad or not names:
            raise IngestError(f"{article.filename}: tier {bad[0] if bad else '(none)'} in a tiers comment is not "
                              f"one of {sorted(ALLOWED_TIERS)}")
        return "\x00TIERS " + ",".join(names) + "\x00" + "\n" * m.group(0).count("\n")

    text = _TIERS_COMMENT.sub(tiers_sentinel, article.body)
    text = COMMENT.sub(lambda m: "\n" * m.group(0).count("\n"), text)
    out: list[Block] = []
    for b in parse_blocks(text, article.body_first_line):
        if b.kind == "tiers":
            out.append(b)
            continue
        rendered, keys = render_policy(b.text, policy, article.filename, b.line)
        out.append(replace(b, text=sanitize(rendered), keys=tuple(keys)))
    return out


def _sections(article: Article, blocks: list[Block]) -> list[_Section]:
    """What: group blocks into sections (rules 2 and 3). Use when: after _prepare. Inputs: article, blocks.
    Output: sections in order; the pre-H2 text is 'intro'; an H2 with no body makes none. Side effects: none.
    Role: section boundaries and per-section tiers."""
    secs: list[_Section] = []
    h2: str | None = None
    cur = _Section(None, None, "intro", [], list(article.tiers), [], "Introduction", ["Introduction"], "intro")
    h2_tiers = list(article.tiers)
    for b in blocks:
        if b.kind == "heading" and b.level in (2, 3):
            secs.append(cur)
            if b.level == 2:
                h2, h2_tiers = b.text, list(article.tiers)
                cur = _Section(h2, None, "h2", [], list(h2_tiers), [], h2, [h2], slug(h2))
            else:
                cur = _Section(h2, b.text, "h3", [], list(h2_tiers), [], b.text, [h2 or b.text, b.text],
                               f"{slug(h2)}/{slug(b.text)}" if h2 else slug(b.text))
        elif b.kind == "heading":
            cur.blocks.append(replace(b, kind="paragraph", text="#" * b.level + " " + b.text))
        elif b.kind == "tiers":
            cur.tiers = b.text.split(",")
            if cur.kind == "h2":
                h2_tiers = cur.tiers
        else:
            cur.blocks.append(b)
            cur.keys.extend(b.keys)
    secs.append(cur)
    return [s for s in secs if s.blocks]


def _merge(secs: list[_Section], count: Callable[[str], int]) -> list[_Section]:
    """What: rule 4, merge bodies under 50 tokens. Use when: after _sections. Inputs: sections, counter.
    Output: merged sections. Side effects: none. Role: a small section joins the previous section under the same
    H2; with none before it (and for the intro) it joins the next section; never across H2 or across tiers. The merged
    section keeps the first heading's path and id and lists both headings."""
    out: list[_Section] = []
    pending: _Section | None = None
    for s in secs:
        if pending is not None:
            if (pending.kind == "intro" or pending.h2 == s.h2) and pending.tiers == s.tiers:
                s = _Section(pending.h2, pending.h3, pending.kind, pending.blocks + s.blocks, pending.tiers,
                             pending.keys + s.keys, f"{pending.heading} + {s.heading}", pending.path, pending.id_base)
            else:
                out.append(pending)
            pending = None
        if count(s.text()) < SMALL:
            prev = out[-1] if out else None
            if prev is not None and prev.h2 == s.h2 and prev.kind != "intro" and prev.tiers == s.tiers:
                prev.blocks += s.blocks
                prev.keys += s.keys
                prev.heading = f"{prev.heading} + {s.heading}"
            else:
                pending = s
        else:
            out.append(s)
    if pending is not None:
        out.append(pending)
    return out


def _split(sec: _Section, article: Article, count: Callable[[str], int]) -> list[list[Block]]:
    """What: rules 5 to 7 for one section. Use when: building chunks. Inputs: section, article, counter.
    Output: list of block groups (one group when the body is 500 tokens or less). Side effects: none.
    Role: over 500 splits at block boundaries, greedily filling pieces up to 400; a single block over 500
    fails; no overlap."""
    if count(sec.text()) <= HARD_MAX:
        return [sec.blocks]
    for b in sec.blocks:
        if count(b.text) > HARD_MAX:
            raise IngestError(f"{article.filename}:{b.line}: section '{sec.heading}' has one {b.kind} block of "
                              f"{count(b.text)} tokens (over {HARD_MAX}) that cannot be split; shorten or split it")
    pieces: list[list[Block]] = [[]]
    for b in sec.blocks:
        if pieces[-1] and count("\n\n".join(x.text for x in pieces[-1] + [b])) > TARGET_MAX:
            pieces.append([])
        pieces[-1].append(b)
    return pieces


def chunk_article(article: Article, policy, count: Callable[[str], int] = count_tokens) -> list[Chunk]:
    """What: chunk one article. Use when: ingest. Inputs: Article, PolicyConfig, optional token counter.
    Output: ordered Chunk list with unique deterministic ids. Side effects: none.
    Role: runs render, sanitize and the 8 rules; raises IngestError for an unknown placeholder, a bad tier,
    an unsplittable block over 500 tokens, duplicate ids, or zero chunks."""
    blocks = _prepare(article, policy)
    sections = _merge(_sections(article, blocks), count)
    chunks: list[Chunk] = []
    for sec in sections:
        pieces = _split(sec, article, count)
        for n, piece in enumerate(pieces, 1):
            body = "\n\n".join(b.text for b in piece)
            path = " > ".join([article.title, *sec.path]) if sec.kind != "intro" else f"{article.title} > Introduction"
            header = f"Article: {path}\nApplies to: {', '.join(sec.tiers)}\nUpdated: {article.last_updated}"
            embed = header + "\n\n" + body
            cid = f"{article.article_id}#{sec.id_base}" + (f"~{n}" if len(pieces) > 1 else "")
            keys = sorted({k for b in piece for k in b.keys})
            chunks.append(Chunk(
                chunk_id=cid, article_slug=article.article_id, ordinal=len(chunks), section_path=list(sec.path),
                heading=sec.heading, header=header, body=body, embed_text=embed, source_url=article.source_url,
                tiers=list(sec.tiers), error_codes=sorted(set(ERROR_CODE.findall(" ".join(sec.path) + "\n" + body))), policy_keys=keys,
                injection_suspect=is_suspect(body), token_count=count(body),
                content_hash=hashlib.sha256(embed.encode()).hexdigest(), doc_version=DOC_VERSION,
                last_updated=article.last_updated))
    if not chunks:
        raise IngestError(f"{article.filename}: zero chunks produced")
    seen: set[str] = set()
    for c in chunks:
        if c.chunk_id in seen:
            raise IngestError(f"{article.filename}: duplicate chunk id {c.chunk_id}")
        seen.add(c.chunk_id)
    return chunks
