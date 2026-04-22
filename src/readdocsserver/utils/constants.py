"""Shared limits and whitelists for MCP tools."""

MAX_FETCH_LINES = 5000

VALID_EDGE_TYPES = frozenset(
    {
        "has_method",
        "inherits_from",
        "returns",
        "accepts_parameter_type",
        "references",
        "see_also",
        "mentioned_in_note",
        "mentioned_in_warning",
        "only_valid_in",
        "requires",
        "use_instead",
        "similar_to",
        "converts_to",
    }
)
