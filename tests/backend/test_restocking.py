"""
Tests for restocking and purchase order API endpoints.
"""
from datetime import datetime

import pytest

import mock_data


TREND_PRIORITY = {"increasing": 0, "stable": 1, "decreasing": 2}

VALID_REASONS = set(TREND_PRIORITY) | {"over_budget", "no_catalog_entry"}


@pytest.fixture(autouse=True)
def isolate_purchase_orders():
    """Keep tests independent of each other's submitted orders.

    Purchase orders live in a module-level list that POST mutates, and main.py holds a reference
    to that same list object. Restoring with a slice assignment (not a rebind) keeps both views
    pointing at the same list.
    """
    original = list(mock_data.purchase_orders)
    yield
    mock_data.purchase_orders[:] = original


@pytest.fixture
def restock_order_payload(client):
    """A purchase order request built from whatever the recommender currently selects."""
    response = client.get("/api/restocking/recommendations")
    data = response.json()

    selected = [r for r in data["recommendations"] if r["selected"]]
    assert len(selected) > 0, "Expected the default budget to select at least one item"

    return {
        "budget": data["budget"],
        "items": [
            {
                "item_sku": item["item_sku"],
                "item_name": item["item_name"],
                "quantity": item["order_quantity"],
                "unit_cost": item["unit_cost"],
                "supplier": item["supplier"],
                "lead_time_days": item["lead_time_days"],
            }
            for item in selected
        ],
    }


class TestRestockingRecommendations:
    """Test suite for the restocking recommendations endpoint."""

    def test_get_recommendations_default_budget(self, client):
        """Test getting recommendations without passing a budget."""
        response = client.get("/api/restocking/recommendations")
        assert response.status_code == 200

        data = response.json()
        assert isinstance(data, dict)

        for field in [
            "budget", "budget_min", "budget_max", "budget_step", "budget_default",
            "total_cost", "total_units", "items_selected", "items_total",
            "full_restock_cost", "recommendations",
        ]:
            assert field in data

        # With no budget given, the endpoint falls back to its own default
        assert data["budget"] == data["budget_default"]
        assert isinstance(data["recommendations"], list)
        assert len(data["recommendations"]) > 0

    def test_recommendation_structure(self, client):
        """Test that each recommendation has proper structure and types."""
        response = client.get("/api/restocking/recommendations")
        data = response.json()

        for item in data["recommendations"]:
            assert isinstance(item["item_sku"], str)
            assert isinstance(item["item_name"], str)
            assert isinstance(item["trend"], str)
            assert isinstance(item["current_demand"], int)
            assert isinstance(item["forecasted_demand"], int)
            assert isinstance(item["required_quantity"], int)
            assert isinstance(item["order_quantity"], int)
            assert isinstance(item["pack_size"], int)
            assert isinstance(item["unit_cost"], (int, float))
            assert isinstance(item["line_cost"], (int, float))
            assert isinstance(item["supplier"], str)
            assert isinstance(item["lead_time_days"], int)
            assert isinstance(item["selected"], bool)
            assert item["reason"] in VALID_REASONS

            assert item["order_quantity"] >= 0
            assert item["unit_cost"] >= 0
            assert item["line_cost"] >= 0
            assert item["lead_time_days"] >= 0

    def test_recommendations_cover_every_forecast_item(self, client):
        """Test that no demand forecast item is silently dropped."""
        forecast_skus = {item["item_sku"] for item in client.get("/api/demand").json()}

        data = client.get("/api/restocking/recommendations").json()
        recommended_skus = {item["item_sku"] for item in data["recommendations"]}

        assert recommended_skus == forecast_skus
        assert data["items_total"] == len(forecast_skus)

    def test_zero_budget_selects_nothing(self, client):
        """Test that a zero budget buys nothing."""
        data = client.get("/api/restocking/recommendations?budget=0").json()

        assert data["items_selected"] == 0
        assert data["total_cost"] == 0
        assert data["total_units"] == 0
        assert all(not item["selected"] for item in data["recommendations"])

    def test_max_budget_selects_everything_priced(self, client):
        """Test that the top of the slider range affords the whole restock."""
        base = client.get("/api/restocking/recommendations?budget=0").json()
        data = client.get(
            f"/api/restocking/recommendations?budget={base['budget_max']}"
        ).json()

        priced = [item for item in data["recommendations"] if item["line_cost"] > 0]

        assert data["items_selected"] == len(priced)
        assert abs(data["total_cost"] - data["full_restock_cost"]) < 0.01

    def test_total_cost_never_exceeds_budget(self, client):
        """Test that the selection stays inside the budget at every level."""
        for budget in [0, 5000, 20000, 45000, 53000, 80000, 106000, 500000]:
            data = client.get(f"/api/restocking/recommendations?budget={budget}").json()
            assert data["total_cost"] <= budget + 0.01, (
                f"Budget {budget} overspent: {data['total_cost']}"
            )

    def test_selection_is_monotonic_in_budget(self, client):
        """Test that raising the budget never buys fewer items."""
        budgets = [0, 5000, 20000, 45000, 53000, 80000, 106000]
        counts = [
            client.get(f"/api/restocking/recommendations?budget={b}").json()["items_selected"]
            for b in budgets
        ]

        assert counts == sorted(counts), f"Selection not monotonic: {dict(zip(budgets, counts))}"

    def test_totals_match_selected_lines(self, client):
        """Test that the summary totals are the sum of the selected lines."""
        data = client.get("/api/restocking/recommendations?budget=45000").json()
        selected = [item for item in data["recommendations"] if item["selected"]]

        assert data["items_selected"] == len(selected)
        assert abs(data["total_cost"] - sum(i["line_cost"] for i in selected)) < 0.01
        assert data["total_units"] == sum(i["order_quantity"] for i in selected)

    def test_order_quantity_rounds_up_to_whole_packs(self, client):
        """Test that order quantities are whole packs covering the forecast need."""
        data = client.get("/api/restocking/recommendations").json()

        for item in data["recommendations"]:
            if item["pack_size"] == 0:
                continue  # unpriced item, nothing to order

            assert item["order_quantity"] % item["pack_size"] == 0
            assert item["order_quantity"] >= item["required_quantity"]
            # Never round up by a whole extra pack
            assert item["order_quantity"] - item["required_quantity"] < item["pack_size"]

    def test_required_quantity_matches_forecast(self, client):
        """Test that the 30-day need is the forecasted demand."""
        forecasts = {f["item_sku"]: f for f in client.get("/api/demand").json()}
        data = client.get("/api/restocking/recommendations").json()

        for item in data["recommendations"]:
            assert item["required_quantity"] == forecasts[item["item_sku"]]["forecasted_demand"]

    def test_line_cost_calculation(self, client):
        """Test that each line cost is quantity times unit cost."""
        data = client.get("/api/restocking/recommendations").json()

        for item in data["recommendations"]:
            expected = item["order_quantity"] * item["unit_cost"]
            assert abs(item["line_cost"] - expected) < 0.01

    def test_recommendations_ranked_by_trend(self, client):
        """Test that rising demand is queued ahead of stable and falling demand."""
        data = client.get("/api/restocking/recommendations").json()
        ranks = [TREND_PRIORITY[i["trend"].lower()] for i in data["recommendations"]]

        assert ranks == sorted(ranks), "Recommendations are not ordered by trend urgency"

    def test_greedy_fill_skips_unaffordable_lines(self, client):
        """Test that an unaffordable line doesn't stop the fill.

        A cheap line further down the ranking must still get bought.
        """
        base = client.get("/api/restocking/recommendations?budget=0").json()
        priced = [i for i in base["recommendations"] if i["line_cost"] > 0]
        cheapest = min(i["line_cost"] for i in priced)

        # Only meaningful if the top-ranked line costs more than the cheapest one
        if priced[0]["line_cost"] > cheapest:
            data = client.get(f"/api/restocking/recommendations?budget={cheapest}").json()
            assert data["items_selected"] >= 1, (
                "Fill stopped at the first unaffordable line instead of skipping it"
            )
            assert data["total_cost"] <= cheapest + 0.01

    def test_budget_range_derived_from_data(self, client):
        """Test that the slider range comes from the cost of a full restock."""
        data = client.get("/api/restocking/recommendations").json()

        assert data["budget_min"] == 0
        assert data["budget_step"] > 0
        assert data["budget_max"] >= data["full_restock_cost"]
        # The ceiling snaps to a whole step so the far end always affords everything
        assert data["budget_max"] % data["budget_step"] == 0
        assert 0 <= data["budget_default"] <= data["budget_max"]

    def test_unpriced_items_are_never_selected(self, client):
        """Test that an item with no catalog entry can't be ordered."""
        data = client.get(
            f"/api/restocking/recommendations"
            f"?budget={client.get('/api/restocking/recommendations').json()['budget_max']}"
        ).json()

        for item in data["recommendations"]:
            if item["reason"] == "no_catalog_entry":
                assert item["selected"] is False
                assert item["line_cost"] == 0

    def test_negative_budget_rejected(self, client):
        """Test that a negative budget is refused."""
        response = client.get("/api/restocking/recommendations?budget=-5")
        assert response.status_code == 400

        data = response.json()
        assert "detail" in data
        assert "negative" in data["detail"].lower()

    def test_non_numeric_budget_rejected(self, client):
        """Test that a non-numeric budget fails validation."""
        response = client.get("/api/restocking/recommendations?budget=abc")
        assert response.status_code == 422


class TestPurchaseOrderCreation:
    """Test suite for submitting purchase orders."""

    def test_create_purchase_order(self, client, restock_order_payload):
        """Test submitting a restocking order."""
        response = client.post("/api/purchase-orders", json=restock_order_payload)
        assert response.status_code == 201

        data = response.json()
        assert data["po_number"].startswith("PO-")
        assert data["status"] == "Submitted"
        assert len(data["items"]) == len(restock_order_payload["items"])
        assert data["budget"] == restock_order_payload["budget"]
        assert data["backlog_item_id"] is None

    def test_created_totals_match_lines(self, client, restock_order_payload):
        """Test that order totals are the sum of the order lines."""
        data = client.post("/api/purchase-orders", json=restock_order_payload).json()

        expected_cost = sum(
            line["quantity"] * line["unit_cost"] for line in restock_order_payload["items"]
        )
        expected_units = sum(line["quantity"] for line in restock_order_payload["items"])

        assert abs(data["total_cost"] - expected_cost) < 0.01
        assert data["total_units"] == expected_units

    def test_line_cost_is_recomputed_server_side(self, client):
        """Test that a wrong line_cost sent by the client is corrected."""
        payload = {
            "items": [
                {
                    "item_sku": "GSK-203",
                    "item_name": "High-Temperature Gasket",
                    "quantity": 100,
                    "unit_cost": 12.0,
                    "supplier": "Sealcraft Materials",
                    "lead_time_days": 10,
                    "line_cost": 1.0,  # deliberately wrong
                }
            ]
        }

        data = client.post("/api/purchase-orders", json=payload).json()

        assert data["items"][0]["line_cost"] == 1200.0
        assert data["total_cost"] == 1200.0

    def test_lead_time_is_slowest_line(self, client):
        """Test that the order's lead time is that of its slowest line."""
        payload = {
            "items": [
                {
                    "item_sku": "FLT-405", "item_name": "Oil Filter Cartridge",
                    "quantity": 50, "unit_cost": 18.0,
                    "supplier": "Clearflow Filtration", "lead_time_days": 7,
                },
                {
                    "item_sku": "MTR-304", "item_name": "Electric Motor 5HP",
                    "quantity": 5, "unit_cost": 340.0,
                    "supplier": "Axial Drive Systems", "lead_time_days": 35,
                },
            ]
        }

        data = client.post("/api/purchase-orders", json=payload).json()
        assert data["lead_time_days"] == 35

    def test_expected_delivery_follows_lead_time(self, client, restock_order_payload):
        """Test that expected delivery is the creation date plus the lead time."""
        data = client.post("/api/purchase-orders", json=restock_order_payload).json()

        created = datetime.strptime(data["created_date"], "%Y-%m-%d")
        delivery = datetime.strptime(data["expected_delivery"], "%Y-%m-%d")

        assert (delivery - created).days == data["lead_time_days"]

    def test_po_numbers_are_unique(self, client, restock_order_payload):
        """Test that consecutive orders get distinct numbers."""
        first = client.post("/api/purchase-orders", json=restock_order_payload).json()
        second = client.post("/api/purchase-orders", json=restock_order_payload).json()

        assert first["po_number"] != second["po_number"]
        assert first["id"] != second["id"]

    def test_empty_items_rejected(self, client):
        """Test that an order with no lines is refused."""
        response = client.post("/api/purchase-orders", json={"items": []})
        assert response.status_code == 400

        data = response.json()
        assert "detail" in data
        assert "at least one" in data["detail"].lower()

    def test_missing_line_fields_rejected(self, client):
        """Test that an incomplete order line fails validation."""
        response = client.post(
            "/api/purchase-orders",
            json={"items": [{"item_sku": "GSK-203", "quantity": 100}]},
        )
        assert response.status_code == 422

    def test_backlog_item_id_is_accepted(self, client):
        """Test that the backlog flow can raise a single-line order."""
        payload = {
            "backlog_item_id": "2",
            "notes": "Expedite for backordered customer",
            "items": [
                {
                    "item_sku": "MTR-304", "item_name": "Electric Motor 5HP",
                    "quantity": 10, "unit_cost": 340.0,
                    "supplier": "Axial Drive Systems", "lead_time_days": 35,
                }
            ],
        }

        data = client.post("/api/purchase-orders", json=payload).json()

        assert data["backlog_item_id"] == "2"
        assert data["notes"] == "Expedite for backordered customer"


class TestPurchaseOrderRetrieval:
    """Test suite for reading purchase orders back."""

    def test_get_all_purchase_orders(self, client):
        """Test getting purchase orders returns a list."""
        response = client.get("/api/purchase-orders")
        assert response.status_code == 200
        assert isinstance(response.json(), list)

    def test_submitted_order_appears_in_list(self, client, restock_order_payload):
        """Test that a submitted order can be read back."""
        created = client.post("/api/purchase-orders", json=restock_order_payload).json()

        orders = client.get("/api/purchase-orders").json()
        po_numbers = [po["po_number"] for po in orders]

        assert created["po_number"] in po_numbers

    def test_most_recent_order_first(self, client, restock_order_payload):
        """Test that orders come back newest first."""
        client.post("/api/purchase-orders", json=restock_order_payload)
        second = client.post("/api/purchase-orders", json=restock_order_payload).json()

        orders = client.get("/api/purchase-orders").json()
        assert orders[0]["po_number"] == second["po_number"]

    def test_get_purchase_order_by_backlog_item(self, client):
        """Test finding the order raised for a backlog item."""
        payload = {
            "backlog_item_id": "3",
            "items": [
                {
                    "item_sku": "VLV-506", "item_name": "Pressure Relief Valve",
                    "quantity": 80, "unit_cost": 95.0,
                    "supplier": "Baxter Valve Works", "lead_time_days": 28,
                }
            ],
        }
        created = client.post("/api/purchase-orders", json=payload).json()

        response = client.get("/api/purchase-orders/3")
        assert response.status_code == 200
        assert response.json()["po_number"] == created["po_number"]

    def test_get_nonexistent_backlog_item_order(self, client):
        """Test that a backlog item with no order returns 404."""
        response = client.get("/api/purchase-orders/nonexistent-999")
        assert response.status_code == 404

        data = response.json()
        assert "detail" in data
        assert "not found" in data["detail"].lower()


class TestBacklogPurchaseOrderIntegration:
    """Test suite for the backlog's purchase order flag."""

    def test_backlog_survives_a_restocking_order(self, client, restock_order_payload):
        """Test that a restocking order doesn't break the backlog endpoint.

        Restocking orders carry no backlog_item_id, so indexing that key while scanning
        purchase orders used to take this endpoint down with a KeyError.
        """
        client.post("/api/purchase-orders", json=restock_order_payload)

        response = client.get("/api/backlog")
        assert response.status_code == 200

        data = response.json()
        assert isinstance(data, list)
        for item in data:
            assert item["has_purchase_order"] is False

    def test_backlog_flags_item_with_an_order(self, client):
        """Test that raising an order against a backlog item sets its flag."""
        backlog = client.get("/api/backlog").json()
        assert len(backlog) > 0
        target_id = backlog[0]["id"]

        client.post(
            "/api/purchase-orders",
            json={
                "backlog_item_id": target_id,
                "items": [
                    {
                        "item_sku": backlog[0]["item_sku"],
                        "item_name": backlog[0]["item_name"],
                        "quantity": backlog[0]["quantity_needed"],
                        "unit_cost": 50.0,
                        "supplier": "Meridian Industrial",
                        "lead_time_days": 14,
                    }
                ],
            },
        )

        updated = client.get("/api/backlog").json()
        flags = {item["id"]: item["has_purchase_order"] for item in updated}

        assert flags[target_id] is True
        for item_id, flag in flags.items():
            if item_id != target_id:
                assert flag is False
