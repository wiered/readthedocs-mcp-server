"""Text slicing helpers."""


def slice_body_lines(body: str, start: int, end: int) -> tuple[str, int, int, int]:
    """Return (slice_text, total_lines, slice_start_used, slice_end_used)."""
    lines = body.splitlines()
    n = len(lines)
    if n == 0:
        return "", 0, start, end
    if start > n:
        return "", n, start, min(end, n)
    s = max(1, start)
    e = min(max(s, end), n)
    return "\n".join(lines[s - 1 : e]), n, s, e
