// Neo4j Aura import script for GitHub/raw HTTPS CSV chunks.
// Replace YOUR_GITHUB_RAW_BASE_URL with your real raw GitHub base URL.
// Example: https://raw.githubusercontent.com/USER/REPO/main

CREATE CONSTRAINT user_id IF NOT EXISTS
FOR (u:User) REQUIRE u.user_id IS UNIQUE;

CREATE CONSTRAINT product_id IF NOT EXISTS
FOR (p:Product) REQUIRE p.product_id IS UNIQUE;

CREATE CONSTRAINT review_id IF NOT EXISTS
FOR (r:Review) REQUIRE r.review_id IS UNIQUE;

CREATE CONSTRAINT category_id IF NOT EXISTS
FOR (c:Category) REQUIRE c.category_id IS UNIQUE;

CREATE CONSTRAINT store_id IF NOT EXISTS
FOR (s:Store) REQUIRE s.store_id IS UNIQUE;

CREATE CONSTRAINT feature_id IF NOT EXISTS
FOR (f:Feature) REQUIRE f.feature_id IS UNIQUE;


LOAD CSV WITH HEADERS FROM 'https://raw.githubusercontent.com/USER/REPO/main/all_beauty/nodes_users.csv' AS row
MERGE (:User {user_id: row.user_id});


LOAD CSV WITH HEADERS FROM 'https://raw.githubusercontent.com/USER/REPO/main/all_beauty/nodes_products.csv' AS row
MERGE (p:Product {product_id: row.product_id})
SET p.title = row.title,
    p.main_category = row.main_category,
    p.price = CASE row.price WHEN '' THEN null ELSE toFloat(row.price) END,
    p.average_rating = CASE row.average_rating WHEN '' THEN null ELSE toFloat(row.average_rating) END,
    p.rating_number = CASE row.rating_number WHEN '' THEN null ELSE toInteger(row.rating_number) END;


LOAD CSV WITH HEADERS FROM 'https://raw.githubusercontent.com/USER/REPO/main/all_beauty/nodes_reviews_part001.csv' AS row
MERGE (r:Review {review_id: row.review_id})
SET r.title = row.title,
    r.text = row.text,
    r.rating = CASE row.rating WHEN '' THEN null ELSE toFloat(row.rating) END,
    r.timestamp = CASE row.timestamp WHEN '' THEN null ELSE toInteger(row.timestamp) END,
    r.helpful_vote = CASE row.helpful_vote WHEN '' THEN 0 ELSE toInteger(row.helpful_vote) END,
    r.verified_purchase = CASE row.verified_purchase WHEN 'True' THEN true WHEN 'False' THEN false ELSE null END;


LOAD CSV WITH HEADERS FROM 'https://raw.githubusercontent.com/USER/REPO/main/all_beauty/nodes_reviews_part002.csv' AS row
MERGE (r:Review {review_id: row.review_id})
SET r.title = row.title,
    r.text = row.text,
    r.rating = CASE row.rating WHEN '' THEN null ELSE toFloat(row.rating) END,
    r.timestamp = CASE row.timestamp WHEN '' THEN null ELSE toInteger(row.timestamp) END,
    r.helpful_vote = CASE row.helpful_vote WHEN '' THEN 0 ELSE toInteger(row.helpful_vote) END,
    r.verified_purchase = CASE row.verified_purchase WHEN 'True' THEN true WHEN 'False' THEN false ELSE null END;


LOAD CSV WITH HEADERS FROM 'https://raw.githubusercontent.com/USER/REPO/main/all_beauty/nodes_reviews_part003.csv' AS row
MERGE (r:Review {review_id: row.review_id})
SET r.title = row.title,
    r.text = row.text,
    r.rating = CASE row.rating WHEN '' THEN null ELSE toFloat(row.rating) END,
    r.timestamp = CASE row.timestamp WHEN '' THEN null ELSE toInteger(row.timestamp) END,
    r.helpful_vote = CASE row.helpful_vote WHEN '' THEN 0 ELSE toInteger(row.helpful_vote) END,
    r.verified_purchase = CASE row.verified_purchase WHEN 'True' THEN true WHEN 'False' THEN false ELSE null END;


LOAD CSV WITH HEADERS FROM 'https://raw.githubusercontent.com/USER/REPO/main/all_beauty/nodes_categories.csv' AS row
MERGE (c:Category {category_id: row.category_id})
SET c.name = row.name;


LOAD CSV WITH HEADERS FROM 'https://raw.githubusercontent.com/USER/REPO/main/all_beauty/nodes_stores.csv' AS row
MERGE (s:Store {store_id: row.store_id})
SET s.name = row.name;


LOAD CSV WITH HEADERS FROM 'https://raw.githubusercontent.com/USER/REPO/main/all_beauty/nodes_features_part001.csv' AS row
MERGE (f:Feature {feature_id: row.feature_id})
SET f.text = row.text,
    f.normalized_text = row.normalized_text;


LOAD CSV WITH HEADERS FROM 'https://raw.githubusercontent.com/USER/REPO/main/all_beauty/nodes_features_part002.csv' AS row
MERGE (f:Feature {feature_id: row.feature_id})
SET f.text = row.text,
    f.normalized_text = row.normalized_text;


LOAD CSV WITH HEADERS FROM 'https://raw.githubusercontent.com/USER/REPO/main/all_beauty/rel_wrote.csv' AS row
MATCH (u:User {user_id: row.user_id})
MATCH (r:Review {review_id: row.review_id})
MERGE (u)-[:WROTE]->(r);


LOAD CSV WITH HEADERS FROM 'https://raw.githubusercontent.com/USER/REPO/main/all_beauty/rel_reviews.csv' AS row
MATCH (r:Review {review_id: row.review_id})
MATCH (p:Product {product_id: row.product_id})
MERGE (r)-[:REVIEWS]->(p);


LOAD CSV WITH HEADERS FROM 'https://raw.githubusercontent.com/USER/REPO/main/all_beauty/rel_rated.csv' AS row
MATCH (u:User {user_id: row.user_id})
MATCH (p:Product {product_id: row.product_id})
MERGE (u)-[rel:RATED]->(p)
SET rel.rating = CASE row.rating WHEN '' THEN null ELSE toFloat(row.rating) END,
    rel.timestamp = CASE row.timestamp WHEN '' THEN null ELSE toInteger(row.timestamp) END,
    rel.verified_purchase = CASE row.verified_purchase WHEN 'True' THEN true WHEN 'False' THEN false ELSE null END;


LOAD CSV WITH HEADERS FROM 'https://raw.githubusercontent.com/USER/REPO/main/all_beauty/rel_product_category.csv' AS row
MATCH (p:Product {product_id: row.product_id})
MATCH (c:Category {category_id: row.category_id})
MERGE (p)-[:BELONGS_TO]->(c);


LOAD CSV WITH HEADERS FROM 'https://raw.githubusercontent.com/USER/REPO/main/all_beauty/rel_product_store.csv' AS row
MATCH (p:Product {product_id: row.product_id})
MATCH (s:Store {store_id: row.store_id})
MERGE (p)-[:SOLD_BY]->(s);


LOAD CSV WITH HEADERS FROM 'https://raw.githubusercontent.com/USER/REPO/main/all_beauty/rel_product_feature.csv' AS row
MATCH (p:Product {product_id: row.product_id})
MATCH (f:Feature {feature_id: row.feature_id})
MERGE (p)-[:HAS_FEATURE]->(f);


// Quick checks
MATCH (n)
RETURN labels(n) AS labels, count(*) AS count
ORDER BY labels;

MATCH ()-[r]->()
RETURN type(r) AS relationship, count(*) AS count
ORDER BY relationship;
