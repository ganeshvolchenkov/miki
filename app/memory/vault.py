import os
import re
import yaml
import datetime
from typing import List, Optional
from app.memory.models import MemoryNode, MemoryEdge
from app.memory.graph import MemoryGraph

class VaultManager:
    """Manages the Obsidian-like markdown vault and populates the MemoryGraph."""
    
    WIKILINK_PATTERN = re.compile(r'\[\[(.*?)\]\]')
    FRONTMATTER_PATTERN = re.compile(r'^---\s*\n(.*?)\n---\s*\n', re.DOTALL)
    
    def __init__(self, vault_path: str):
        self.vault_path = vault_path
        self.graph = MemoryGraph()
        
    def _parse_file(self, filepath: str, filename: str) -> MemoryNode:
        with open(filepath, 'r', encoding='utf-8') as f:
            content = f.read()
            
        title = os.path.splitext(filename)[0]
        metadata = {}
        tags = set()
        clean_content = content
        
        # Extract YAML Frontmatter
        fm_match = self.FRONTMATTER_PATTERN.match(content)
        if fm_match:
            try:
                metadata = yaml.safe_load(fm_match.group(1)) or {}
                clean_content = content[fm_match.end():]
                if isinstance(metadata.get('tags'), list):
                    tags = set(metadata['tags'])
            except yaml.YAMLError:
                pass
                
        # Basic tag extraction from content (#tag)
        content_tags = re.findall(r'#(\w+)', clean_content)
        tags.update(content_tags)
        
        node = MemoryNode(
            title=title,
            path=filepath,
            content=clean_content,
            tags=tags,
            metadata=metadata
        )
        return node
        
    def _extract_links(self, title: str, content: str) -> List[MemoryEdge]:
        edges = []
        # Find all wikilinks
        for match in self.WIKILINK_PATTERN.finditer(content):
            target = match.group(1).split('|')[0].strip() # Handle aliases like [[Note|Alias]]
            
            # Simple context extraction (get some chars around the match)
            start = max(0, match.start() - 30)
            end = min(len(content), match.end() + 30)
            context = content[start:end].replace('\n', ' ').strip()
            
            edges.append(MemoryEdge(
                source_title=title,
                target_title=target,
                type="wikilink",
                context=context
            ))
        return edges

    def scan_vault(self):
        """Scans the entire vault directory, parsing files and building the graph."""
        self.graph.clear()
        
        if not os.path.exists(self.vault_path):
            os.makedirs(self.vault_path)
            
        for root, _, files in os.walk(self.vault_path):
            for file in files:
                if file.endswith('.md'):
                    filepath = os.path.join(root, file)
                    node = self._parse_file(filepath, file)
                    self.graph.add_node(node)
                    
                    edges = self._extract_links(node.title, node.content)
                    for edge in edges:
                        self.graph.add_edge(edge)
                        
    def add_memory(self, title: str, content: str, tags: Optional[List[str]] = None) -> MemoryNode:
        """Adds a new memory to the vault and updates the graph."""
        if not os.path.exists(self.vault_path):
            os.makedirs(self.vault_path)
            
        tags = tags or []
        filepath = os.path.join(self.vault_path, f"{title}.md")
        
        # Prepare frontmatter
        frontmatter = {
            "created": datetime.datetime.utcnow().isoformat(),
            "tags": tags
        }
        
        yaml_fm = yaml.dump(frontmatter, default_flow_style=False).strip()
        full_content = f"---\n{yaml_fm}\n---\n\n{content}"
        
        with open(filepath, 'w', encoding='utf-8') as f:
            f.write(full_content)
            
        # Re-parse to get node and edges safely
        node = self._parse_file(filepath, f"{title}.md")
        self.graph.add_node(node)
        
        edges = self._extract_links(node.title, node.content)
        for edge in edges:
            self.graph.add_edge(edge)
            
        return node
