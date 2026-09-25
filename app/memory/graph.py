import networkx as nx
from typing import List, Dict, Set, Optional, Tuple, Any
from app.memory.models import MemoryNode, MemoryEdge

class MemoryGraph:
    """In-memory Graph database to hold connections between notes."""
    
    def __init__(self):
        self.graph = nx.DiGraph()
        self.nodes_data: Dict[str, MemoryNode] = {}
        
    def _normalize_title(self, title: str) -> str:
        """Normalize node titles (case-insensitive internally)."""
        return title.strip().lower()

    def add_node(self, node: MemoryNode):
        """Add or update a node in the graph."""
        norm = self._normalize_title(node.title)
        
        # If node already exists, remove its outgoing edges before we update
        if self.graph.has_node(norm):
            out_edges = list(self.graph.out_edges(norm))
            self.graph.remove_edges_from(out_edges)
            
        self.nodes_data[norm] = node
        self.graph.add_node(norm, type="memory", original_title=node.title)
        
    def add_edge(self, edge: MemoryEdge):
        """Add a directed edge between two memories."""
        norm_source = self._normalize_title(edge.source_title)
        norm_target = self._normalize_title(edge.target_title)
        
        # Ensure nodes exist in the graph structurally even if we don't have their full markdown yet
        if not self.graph.has_node(norm_source):
            self.graph.add_node(norm_source, type="stub", original_title=edge.source_title)
        if not self.graph.has_node(norm_target):
            self.graph.add_node(norm_target, type="stub", original_title=edge.target_title)
            
        self.graph.add_edge(norm_source, norm_target, type=edge.type, context=edge.context)
        
    def clear(self):
        """Clear the graph."""
        self.graph.clear()
        self.nodes_data.clear()

    def get_node(self, title: str) -> Optional[MemoryNode]:
        """Get a node by title."""
        norm = self._normalize_title(title)
        return self.nodes_data.get(norm)
        
    def get_related(self, title: str, depth: int = 1) -> Set[str]:
        """Get all memories connected to this one within a certain depth."""
        norm = self._normalize_title(title)
        if not self.graph.has_node(norm):
            return set()
            
        related = set()
        undirected_G = self.graph.to_undirected()
        
        try:
            paths = nx.single_source_shortest_path_length(undirected_G, norm, cutoff=depth)
            for n, dist in paths.items():
                if dist > 0: # Don't include the source node itself
                    original = self.graph.nodes[n].get("original_title", n)
                    related.add(original)
        except Exception:
            pass
            
        return related
        
    def get_backlinks(self, title: str) -> List[Tuple[str, str]]:
        """Get nodes that link TO this title. Returns list of (source_title, context)."""
        norm = self._normalize_title(title)
        if not self.graph.has_node(norm):
            return []
            
        backlinks = []
        for pred in self.graph.predecessors(norm):
            edge_data = self.graph.get_edge_data(pred, norm)
            context = edge_data.get("context", "")
            original = self.graph.nodes[pred].get("original_title", pred)
            backlinks.append((original, context))
            
        return backlinks
        
    def get_shortest_path(self, source: str, target: str) -> Optional[List[str]]:
        """Find the shortest connection path between two memories."""
        norm_source = self._normalize_title(source)
        norm_target = self._normalize_title(target)
        
        if not self.graph.has_node(norm_source) or not self.graph.has_node(norm_target):
            return None
            
        undirected_G = self.graph.to_undirected()
        try:
            path = nx.shortest_path(undirected_G, norm_source, norm_target)
            return [self.graph.nodes[n].get("original_title", n) for n in path]
        except nx.NetworkXNoPath:
            return None

    def get_important_memories(self, top_k: int = 5) -> List[Tuple[str, float]]:
        """Returns the most important memories using PageRank algorithm."""
        if len(self.graph) == 0:
            return []
        pagerank = nx.pagerank(self.graph)
        sorted_nodes = sorted(pagerank.items(), key=lambda x: x[1], reverse=True)
        results = []
        for n, score in sorted_nodes[:top_k]:
            orig = self.graph.nodes[n].get("original_title", n)
            results.append((orig, score))
        return results
        
    def search(self, query: str) -> List[MemoryNode]:
        """Basic text search over nodes' title, content, and tags."""
        query = query.lower()
        results = []
        for node in self.nodes_data.values():
            if query in node.title.lower() or query in node.content.lower():
                results.append(node)
                continue
            for tag in node.tags:
                if query in tag.lower():
                    results.append(node)
                    break
        return results
