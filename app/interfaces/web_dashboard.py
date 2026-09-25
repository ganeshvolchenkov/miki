import sys
import os

# Ensure the root path is in sys.path so we can import app modules
sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..")))

import streamlit as st
from app.core.config import get_settings
from app.core.assistant import Assistant

# Configure the Streamlit page
st.set_page_config(page_title="Miki Second Brain", page_icon="🧠", layout="wide")

# Initialize assistant only once
@st.cache_resource
def get_assistant():
    settings = get_settings()
    assistant = Assistant(settings)
    assistant.vault.scan_vault()
    return assistant

assistant = get_assistant()

st.title("🧠 Miki Second Brain Dashboard")
st.markdown("Welcome to your personal knowledge graph.")

# Sidebar stats
st.sidebar.header("Brain Statistics")
nodes_count = len(assistant.vault.graph.nodes_data)
edges_count = len(assistant.vault.graph.graph.edges)
st.sidebar.metric("Memories (Nodes)", nodes_count)
st.sidebar.metric("Connections (Edges)", edges_count)

# Top Hubs
st.sidebar.subheader("Top Hubs (PageRank)")
important = assistant.vault.graph.get_important_memories(5)
for i, (node, score) in enumerate(important):
    st.sidebar.markdown(f"**{i+1}.** {node} *(Score: {score:.2f})*")

# Main Search interface
st.subheader("🔍 Search Your Vault")
query = st.text_input("Enter a query, tag, or topic to search...")

if query:
    results = assistant.vault.graph.search(query)
    if not results:
        st.warning("No memories found.")
    else:
        st.success(f"Found {len(results)} matches!")
        for res in results:
            with st.expander(f"📝 {res.title}"):
                st.markdown(res.content)
                if res.tags:
                    st.caption("Tags: " + ", ".join(res.tags))
                
                # Show related connections
                related = assistant.vault.graph.get_related(res.title, depth=1)
                if related:
                    st.info("**Related Concepts:** " + ", ".join(related))

st.divider()
st.caption("Miki Dashboard • Powered by Streamlit")
