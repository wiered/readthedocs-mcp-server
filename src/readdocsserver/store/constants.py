"""Tuning constants for FTS queries, chunking, and symbol lookup."""

# Common English doc / NL filler — AND-ing these hurts recall on long questions.
_STOPWORDS = frozenset(
    {
        "a",
        "an",
        "and",
        "are",
        "as",
        "at",
        "be",
        "been",
        "but",
        "by",
        "can",
        "could",
        "did",
        "do",
        "does",
        "for",
        "from",
        "had",
        "has",
        "have",
        "how",
        "i",
        "if",
        "in",
        "into",
        "is",
        "it",
        "its",
        "may",
        "might",
        "must",
        "my",
        "no",
        "not",
        "of",
        "on",
        "or",
        "our",
        "should",
        "so",
        "such",
        "than",
        "that",
        "the",
        "their",
        "them",
        "then",
        "there",
        "these",
        "they",
        "this",
        "to",
        "too",
        "use",
        "using",
        "was",
        "we",
        "were",
        "what",
        "when",
        "where",
        "which",
        "who",
        "why",
        "will",
        "with",
        "would",
        "you",
        "your",
    }
)

_CHUNK_MAX = 2200
_CHUNK_OVERLAP = 180
_CHUNK_MIN_MERGE = 380
# AND-ing many rare terms yields empty hits; cap required terms.
_MAX_AND_TERMS = 6
_MAX_REQUIRED_TERMS = 3
_AUTO_FREE_TOKEN_THRESHOLD = 5
_MAX_OPTIONAL_TERMS = 20
_MAX_FALLBACK_TERMS = 12
_LOW_SIGNAL_QUERY_TERMS = frozenset(
    {
        "docs",
        "documentation",
        "documentations",
        "document",
        "documents",
        "example",
        "examples",
        "guide",
        "guides",
        "manual",
        "reference",
        "references",
        "tutorial",
        "tutorials",
    }
)

_CONTEXT_LINE_MAX = 800

_LOOKUP_CONTEXT_BEFORE = 8
_LOOKUP_CONTEXT_AFTER = 16
