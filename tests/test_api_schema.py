from app.api.main import app


def test_openapi_exposes_graph_readiness_and_localized_product_contracts() -> None:
    paths = app.openapi()["paths"]

    assert "/ready" in paths
    assert "/products/{product_id}/reviews" in paths
    assert "/products/{product_id}/description" in paths
    assert "/recommend" in paths
