"""
Deprecated: functionality merged into import_kg_to_neo4j.py.

Attribute nodes (nodes_attributes.csv), HAS_ATTRIBUTE edges (rel_has_attribute.csv),
and MENTIONS edges (rel_mentions.csv) are now imported automatically by
import_kg_to_neo4j.py when those files are present.
"""
print(
    "This script is deprecated.\n"
    "Run: python import_kg_to_neo4j.py\n"
    "(Attribute files are imported automatically if present.)"
)
