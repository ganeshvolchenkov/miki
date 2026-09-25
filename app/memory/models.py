import datetime
from dataclasses import dataclass, field
from typing import List, Dict, Optional, Set

@dataclass
class MemoryNode:
    """Represents a single memory/note in the graph."""
    title: str
    path: str
    content: str = ""
    tags: Set[str] = field(default_factory=set)
    created_at: datetime.datetime = field(default_factory=datetime.datetime.utcnow)
    updated_at: datetime.datetime = field(default_factory=datetime.datetime.utcnow)
    
    # Custom metadata (from YAML frontmatter)
    metadata: Dict = field(default_factory=dict)
    
@dataclass
class MemoryEdge:
    """Represents a connection between two memories."""
    source_title: str
    target_title: str
    type: str = "link" # can be "link", "tag_cooccurrence", etc.
    context: str = ""  # The sentence or context where the link appeared
