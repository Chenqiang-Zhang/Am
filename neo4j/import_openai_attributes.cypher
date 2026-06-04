// Optional local Neo4j import for LLM-extracted product attributes.
// Run this after the base Product nodes have been imported.
// Copy nodes_attributes.csv and rel_product_attribute.csv into $NEO4J_HOME/import/all_beauty/.

CREATE CONSTRAINT attribute_id IF NOT EXISTS
FOR (a:Attribute) REQUIRE a.attribute_id IS UNIQUE;


LOAD CSV WITH HEADERS FROM 'file:///all_beauty/nodes_attributes.csv' AS row
MERGE (a:Attribute {attribute_id: row.attribute_id})
SET a.name = row.name,
    a.value = row.value,
    a.attribute_type = row.attribute_type;


LOAD CSV WITH HEADERS FROM 'file:///all_beauty/rel_product_attribute.csv' AS row
MATCH (p:Product {product_id: row.product_id})
MATCH (a:Attribute {attribute_id: row.attribute_id})
MERGE (p)-[rel:HAS_ATTRIBUTE]->(a)
SET rel.confidence = CASE row.confidence WHEN '' THEN null ELSE toFloat(row.confidence) END,
    rel.evidence = row.evidence,
    rel.model = row.model;


MATCH (a:Attribute)
RETURN a.attribute_type AS attribute_type, count(*) AS count
ORDER BY count DESC;
