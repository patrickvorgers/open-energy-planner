"""Synthetic retail contract settings shared without importing scenario tests."""


def settings(*, buy="(market + 0.10) * 1.21", sell="market - 0.02"):
    """Describe a synthetic EUR contract. These are arithmetic fixtures, not supplier terms."""
    return {
        "contract_id": "test-contract",
        "market_area": "NL",
        "currency": "EUR",
        "periods": [
            {
                "effective_from": "2026-01-01T00:00:00+00:00",
                "effective_until": None,
                "import_formula": buy,
                "export_formula": sell,
            }
        ],
    }
