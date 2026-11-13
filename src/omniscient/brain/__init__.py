"""The shared brain: typed notes in the Obsidian vault."""

from omniscient.brain.models import (
    BrainError,
    Idea,
    IdeaKind,
    IdeaScores,
    IdeaStatus,
    NewsItem,
    news_id,
)
from omniscient.brain.notes import (
    find_news,
    read_news,
    set_idea_status,
    slugify,
    write_idea,
    write_news,
)

__all__ = [
    "BrainError",
    "Idea",
    "IdeaKind",
    "IdeaScores",
    "IdeaStatus",
    "NewsItem",
    "find_news",
    "news_id",
    "read_news",
    "set_idea_status",
    "slugify",
    "write_idea",
    "write_news",
]
