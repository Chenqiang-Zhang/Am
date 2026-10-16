#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")/.."

set -a
source .env
set +a

compose=(docker compose)
if [ -n "${OLLAMA_GPU_DEVICE:-}" ]; then
  compose+=(-f docker-compose.yml -f docker-compose.gpu.yml)
fi
compose+=(--profile llm)

services=(neo4j-live neo4j-ucsd)
if [ "$BUILD_LLM_PROVIDER" = local ]; then
  services+=(ollama)
fi
"${compose[@]}" up -d "${services[@]}"

until "${compose[@]}" exec -T neo4j-live cypher-shell -u neo4j -p "$LIVE_NEO4J_PASSWORD" "RETURN 1" >/dev/null 2>&1; do
  sleep 3
done
until "${compose[@]}" exec -T neo4j-ucsd cypher-shell -u neo4j -p "$UCSD_NEO4J_PASSWORD" "RETURN 1" >/dev/null 2>&1; do
  sleep 3
done

if [ "$BUILD_LLM_PROVIDER" = local ]; then
  until "${compose[@]}" exec -T ollama ollama list >/dev/null 2>&1; do
    sleep 2
  done
  "${compose[@]}" exec -T ollama ollama pull "$BUILD_LLM_MODEL"
fi

python -m kg.build --graph live "$@"
python -m kg.build --graph ucsd "$@"
