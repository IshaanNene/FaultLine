"""Parsing and chunking the corpus.

Two rules shape this file, and both come from the domain rather than from
retrieval theory.

**Never cut a procedure.** Half a rollback procedure is worse than none -- a
responder who runs steps 1 to 3 of a 5-step rollback has left a deployment at an
unknown revision mid-incident. A section containing an ordered list is kept whole
even when that makes it larger than the target chunk size.

**Parent-child.** Child chunks are small so a match is precise; the parent
section is what gets returned, so the model reads a complete thought rather than
a fragment. Matching and reading want different sizes, so they get different
objects.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path

from faultline.ids import content_hash

# Roughly four characters per token. Good enough for sizing; nothing here needs a
# tokenizer's precision.
CHARS_PER_TOKEN = 4
CHILD_TARGET_TOKENS = 300
CHILD_MAX_TOKENS = 400
PARENT_MAX_TOKENS = 1_500

_FRONTMATTER = re.compile(r"\A---\n(.*?)\n---\n", re.DOTALL)
_HEADING = re.compile(r"^(#{1,6})\s+(.*)$", re.MULTILINE)
_ORDERED_STEP = re.compile(r"^\s*\d+\.\s", re.MULTILINE)


@dataclass(slots=True)
class Document:
    path: str
    doc_type: str
    service: str | None
    owner: str | None
    last_verified: str | None
    title: str
    body: str
    metadata: dict[str, str] = field(default_factory=dict)


@dataclass(slots=True)
class Section:
    """A parent: one heading and everything under it. What generation reads."""

    document: Document
    heading: str
    section_path: str
    text: str
    anchor: str

    @property
    def citation(self) -> str:
        """path#anchor -- resolvable to the exact section in a browser or editor."""
        return f"{self.document.path}#{self.anchor}"

    @property
    def token_estimate(self) -> int:
        return len(self.text) // CHARS_PER_TOKEN


@dataclass(slots=True)
class Chunk:
    """A child: what gets indexed and matched against."""

    id: str
    section: Section
    text: str

    @property
    def indexed_text(self) -> str:
        """Deterministic contextualization.

        The document title, section path, service and type are prepended to every
        child. It costs nothing, needs no model, and it means a chunk about
        rolling back is findable by the service it belongs to even when the
        chunk's own prose never names it.

        The blueprint plans an ablation against LLM-written chunk context; this is
        the baseline that has to be beaten.
        """
        header = " | ".join(
            part
            for part in (
                self.section.document.title,
                self.section.section_path,
                self.section.document.service,
                self.section.document.doc_type,
            )
            if part and part != "*"
        )
        return f"{header}\n{self.text}"


def parse_document(path: Path, root: Path | None = None) -> Document:
    raw = path.read_text()
    metadata: dict[str, str] = {}
    if match := _FRONTMATTER.match(raw):
        for line in match.group(1).splitlines():
            if ":" in line:
                key, _, value = line.partition(":")
                metadata[key.strip()] = value.strip().strip('"')
        raw = raw[match.end() :]

    heading = _HEADING.search(raw)
    title = heading.group(2).strip() if heading else path.stem.replace("-", " ")
    relative = str(path.relative_to(root)) if root else str(path)

    return Document(
        path=relative,
        doc_type=metadata.get("doc_type", "unknown"),
        service=metadata.get("service") or None,
        owner=metadata.get("owner") or None,
        last_verified=metadata.get("last_verified") or None,
        title=title,
        body=raw,
        metadata=metadata,
    )


def split_sections(document: Document) -> list[Section]:
    """One section per heading, carrying the full heading path for context."""
    matches = list(_HEADING.finditer(document.body))
    if not matches:
        return [
            Section(
                document=document,
                heading=document.title,
                section_path=document.title,
                text=document.body.strip(),
                anchor=_anchor(document.title),
            )
        ]

    sections: list[Section] = []
    trail: dict[int, str] = {}
    for index, match in enumerate(matches):
        level = len(match.group(1))
        heading = match.group(2).strip()
        trail[level] = heading
        for deeper in [k for k in trail if k > level]:
            del trail[deeper]

        end = matches[index + 1].start() if index + 1 < len(matches) else len(document.body)
        text = document.body[match.end() : end].strip()
        if not text:
            # A heading with nothing under it is a container, not a section.
            continue

        sections.append(
            Section(
                document=document,
                heading=heading,
                section_path=" > ".join(trail[k] for k in sorted(trail)),
                text=text,
                anchor=_anchor(heading),
            )
        )
    return sections


def split_children(section: Section) -> list[Chunk]:
    """Break a section into child chunks, keeping procedures intact."""
    if section.token_estimate <= CHILD_MAX_TOKENS or _has_procedure(section.text):
        # Small enough, or contains numbered steps that must not be separated.
        return [_chunk(section, section.text, 0)]

    paragraphs = [p.strip() for p in section.text.split("\n\n") if p.strip()]
    chunks: list[Chunk] = []
    buffer: list[str] = []
    size = 0

    for paragraph in paragraphs:
        tokens = len(paragraph) // CHARS_PER_TOKEN
        if _has_procedure(paragraph):
            # Flush whatever is buffered, then emit the procedure alone and whole.
            if buffer:
                chunks.append(_chunk(section, "\n\n".join(buffer), len(chunks)))
                buffer, size = [], 0
            chunks.append(_chunk(section, paragraph, len(chunks)))
            continue
        if size + tokens > CHILD_TARGET_TOKENS and buffer:
            chunks.append(_chunk(section, "\n\n".join(buffer), len(chunks)))
            buffer, size = [], 0
        buffer.append(paragraph)
        size += tokens

    if buffer:
        chunks.append(_chunk(section, "\n\n".join(buffer), len(chunks)))
    return chunks


def chunk_document(document: Document) -> list[Chunk]:
    return [chunk for section in split_sections(document) for chunk in split_children(section)]


def _chunk(section: Section, text: str, ordinal: int) -> Chunk:
    # Content-addressed, so re-ingesting an unchanged document is a no-op and an
    # embedding cache keyed on this never recomputes a vector it already has.
    return Chunk(
        id=content_hash(section.document.path, section.anchor, str(ordinal), text)[:20],
        section=section,
        text=text,
    )


def _has_procedure(text: str) -> bool:
    """Two or more numbered steps. One '1.' is a list of one, not a procedure."""
    return len(_ORDERED_STEP.findall(text)) >= 2


def _anchor(heading: str) -> str:
    slug = re.sub(r"[^a-z0-9\s-]", "", heading.lower())
    return re.sub(r"\s+", "-", slug).strip("-")
