"""Split page bodies into overlapping FTS chunks with line spans."""

from __future__ import annotations

from .constants import _CHUNK_MAX, _CHUNK_MIN_MERGE, _CHUNK_OVERLAP
from .types import ChunkSpan


def _hard_split(text: str, max_chars: int, overlap: int) -> list[str]:
    if len(text) <= max_chars:
        return [text]
    out: list[str] = []
    start = 0
    while start < len(text):
        end = min(start + max_chars, len(text))
        out.append(text[start:end])
        if end >= len(text):
            break
        start = max(0, end - overlap)
    return out


def _hard_split_span(span: ChunkSpan, max_chars: int, overlap: int) -> list[ChunkSpan]:
    if len(span.text) <= max_chars:
        return [span]

    def scaled_line(char_pos: int) -> int:
        if span.line_end <= span.line_start or len(span.text) <= 1:
            return span.line_start
        clamped = min(max(char_pos, 0), len(span.text) - 1)
        frac = clamped / (len(span.text) - 1)
        delta = span.line_end - span.line_start
        return min(span.line_end, span.line_start + int(round(delta * frac)))

    out: list[ChunkSpan] = []
    start = 0
    while start < len(span.text):
        end = min(start + max_chars, len(span.text))
        chunk_text = span.text[start:end]
        if "\n" in span.text:
            chunk_start = span.line_start + span.text[:start].count("\n")
            chunk_end = chunk_start + chunk_text.count("\n")
        else:
            chunk_start = scaled_line(start)
            chunk_end = max(chunk_start, scaled_line(max(start, end - 1)))
        out.append(ChunkSpan(chunk_text, chunk_start, chunk_end))
        if end >= len(span.text):
            break
        start = max(0, end - overlap)
    return out


def _merge_paragraphs(
    paragraphs: list[str], max_chars: int, min_merge: int
) -> list[str]:
    chunks: list[str] = []
    buf: list[str] = []
    size = 0
    for p in paragraphs:
        add_len = len(p) + (2 if buf else 0)
        if buf and size + add_len > max_chars and size >= min_merge:
            chunks.append("\n\n".join(buf))
            buf = [p]
            size = len(p)
        else:
            buf.append(p)
            size += add_len
    if buf:
        chunks.append("\n\n".join(buf))
    return chunks


def _merge_spans(
    parts: list[ChunkSpan], max_chars: int, min_merge: int
) -> list[ChunkSpan]:
    chunks: list[ChunkSpan] = []
    buf: list[ChunkSpan] = []
    size = 0
    for part in parts:
        add_len = len(part.text) + (2 if buf else 0)
        if buf and size + add_len > max_chars and size >= min_merge:
            chunks.append(
                ChunkSpan(
                    text="\n\n".join(item.text for item in buf),
                    line_start=buf[0].line_start,
                    line_end=buf[-1].line_end,
                )
            )
            buf = [part]
            size = len(part.text)
        else:
            buf.append(part)
            size += add_len
    if buf:
        chunks.append(
            ChunkSpan(
                text="\n\n".join(item.text for item in buf),
                line_start=buf[0].line_start,
                line_end=buf[-1].line_end,
            )
        )
    return chunks


def _body_to_chunks_with_lines(body: str) -> list[ChunkSpan]:
    text = body.strip()
    raw_lines = body.splitlines()
    if not text:
        return [ChunkSpan("", 1, 1)]

    paras: list[ChunkSpan] = []
    buf: list[str] = []
    start_line: int | None = None
    end_line: int | None = None
    for lineno, raw_line in enumerate(raw_lines, start=1):
        if raw_line.strip():
            if start_line is None:
                start_line = lineno
            buf.append(raw_line)
            end_line = lineno
            continue
        if buf:
            paras.append(
                ChunkSpan(
                    text="\n".join(buf).strip(),
                    line_start=start_line or lineno,
                    line_end=end_line or lineno,
                )
            )
            buf = []
            start_line = None
            end_line = None
    if buf:
        paras.append(
            ChunkSpan(
                text="\n".join(buf).strip(),
                line_start=start_line or 1,
                line_end=end_line or max(1, len(raw_lines)),
            )
        )
    if not paras:
        return [ChunkSpan(text, 1, max(1, len(raw_lines)))]

    # Single block with mostly single newlines (common Sphinx output): merge lines.
    if len(paras) == 1 and paras[0].text.count("\n") > 8 and "\n\n" not in text:
        single = paras[0]
        line_items = [
            (lineno, raw_line.strip())
            for lineno, raw_line in enumerate(raw_lines, start=1)
            if single.line_start <= lineno <= single.line_end and raw_line.strip()
        ]
        pseudo: list[ChunkSpan] = []
        pseudo_buf: list[str] = []
        pseudo_start: int | None = None
        pseudo_end: int | None = None
        pseudo_size = 0
        for lineno, line_text in line_items:
            add = len(line_text) + (1 if pseudo_buf else 0)
            if (
                pseudo_buf
                and pseudo_size + add > _CHUNK_MIN_MERGE
                and pseudo_size + add > _CHUNK_MAX * 0.9
            ):
                pseudo.append(
                    ChunkSpan(
                        text=" ".join(pseudo_buf),
                        line_start=pseudo_start or lineno,
                        line_end=pseudo_end or lineno,
                    )
                )
                pseudo_buf = [line_text]
                pseudo_start = lineno
                pseudo_end = lineno
                pseudo_size = len(line_text)
            else:
                if pseudo_start is None:
                    pseudo_start = lineno
                pseudo_buf.append(line_text)
                pseudo_end = lineno
                pseudo_size += add
        if pseudo_buf:
            pseudo.append(
                ChunkSpan(
                    text=" ".join(pseudo_buf),
                    line_start=pseudo_start or 1,
                    line_end=pseudo_end or max(1, len(raw_lines)),
                )
            )
        paras = pseudo

    merged = _merge_spans(paras, _CHUNK_MAX, _CHUNK_MIN_MERGE)
    out: list[ChunkSpan] = []
    for merged_span in merged:
        if len(merged_span.text) <= _CHUNK_MAX:
            out.append(merged_span)
        else:
            out.extend(_hard_split_span(merged_span, _CHUNK_MAX, _CHUNK_OVERLAP))
    return out if out else [ChunkSpan(text[:_CHUNK_MAX], 1, max(1, len(raw_lines)))]


def _body_to_chunks(body: str) -> list[str]:
    """Split page text into overlapping segments so FTS BM25 is not dominated by huge pages."""
    return [span.text for span in _body_to_chunks_with_lines(body)]
