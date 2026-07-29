import math
from datetime import datetime, timedelta

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from typing import List, Optional
from pydantic import BaseModel
from mock_data import inventory_items, orders, demand_forecasts, backlog_items, spending_summary, monthly_spending, category_spending, recent_transactions, purchase_orders, restock_catalog

app = FastAPI(title="Factory Inventory Management System")

# Quarter mapping for date filtering
QUARTER_MAP = {
    'Q1-2025': ['2025-01', '2025-02', '2025-03'],
    'Q2-2025': ['2025-04', '2025-05', '2025-06'],
    'Q3-2025': ['2025-07', '2025-08', '2025-09'],
    'Q4-2025': ['2025-10', '2025-11', '2025-12']
}

def filter_by_month(items: list, month: Optional[str]) -> list:
    """Filter items by month/quarter based on order_date field"""
    if not month or month == 'all':
        return items

    if month.startswith('Q'):
        # Handle quarters
        if month in QUARTER_MAP:
            months = QUARTER_MAP[month]
            return [item for item in items if any(m in item.get('order_date', '') for m in months)]
    else:
        # Direct month match
        return [item for item in items if month in item.get('order_date', '')]

    return items

def apply_filters(items: list, warehouse: Optional[str] = None, category: Optional[str] = None,
                 status: Optional[str] = None) -> list:
    """Apply common filters to a list of items"""
    filtered = items

    if warehouse and warehouse != 'all':
        filtered = [item for item in filtered if item.get('warehouse') == warehouse]

    if category and category != 'all':
        filtered = [item for item in filtered if item.get('category', '').lower() == category.lower()]

    if status and status != 'all':
        filtered = [item for item in filtered if item.get('status', '').lower() == status.lower()]

    return filtered

# CORS middleware
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Data models
class InventoryItem(BaseModel):
    id: str
    sku: str
    name: str
    category: str
    warehouse: str
    quantity_on_hand: int
    reorder_point: int
    unit_cost: float
    location: str
    last_updated: str

class Order(BaseModel):
    id: str
    order_number: str
    customer: str
    items: List[dict]
    status: str
    order_date: str
    expected_delivery: str
    total_value: float
    actual_delivery: Optional[str] = None
    warehouse: Optional[str] = None
    category: Optional[str] = None

class DemandForecast(BaseModel):
    id: str
    item_sku: str
    item_name: str
    current_demand: int
    forecasted_demand: int
    trend: str
    period: str

class BacklogItem(BaseModel):
    id: str
    order_id: str
    item_sku: str
    item_name: str
    quantity_needed: int
    quantity_available: int
    days_delayed: int
    priority: str
    has_purchase_order: Optional[bool] = False

class RestockRecommendation(BaseModel):
    item_sku: str
    item_name: str
    trend: str
    current_demand: int
    forecasted_demand: int
    # required_quantity is the raw 30-day need; order_quantity rounds it up to a full pack
    required_quantity: int
    order_quantity: int
    pack_size: int
    unit_cost: float
    line_cost: float
    supplier: str
    lead_time_days: int
    selected: bool
    reason: str

class RestockRecommendationsResponse(BaseModel):
    budget: float
    budget_min: float
    budget_max: float
    budget_step: float
    budget_default: float
    total_cost: float
    total_units: int
    items_selected: int
    items_total: int
    full_restock_cost: float
    recommendations: List[RestockRecommendation]

# A purchase order carries multiple lines: a restocking order covers several SKUs at once.
# backlog_item_id stays optional so the single-item backlog flow can reuse the same shape.
class PurchaseOrderLine(BaseModel):
    item_sku: str
    item_name: str
    quantity: int
    unit_cost: float
    supplier: str
    lead_time_days: int
    # Optional on input, always recomputed server-side before the order is stored
    line_cost: float = 0.0

class PurchaseOrder(BaseModel):
    id: str
    po_number: str
    items: List[PurchaseOrderLine]
    total_cost: float
    total_units: int
    status: str
    created_date: str
    expected_delivery: str
    # Lead time of the whole order is the slowest line: the order isn't complete until all arrive
    lead_time_days: int
    budget: Optional[float] = None
    backlog_item_id: Optional[str] = None
    notes: Optional[str] = None

class CreatePurchaseOrderRequest(BaseModel):
    items: List[PurchaseOrderLine]
    budget: Optional[float] = None
    backlog_item_id: Optional[str] = None
    notes: Optional[str] = None

# API endpoints
@app.get("/")
def root():
    return {"message": "Factory Inventory Management System API", "version": "1.0.0"}

@app.get("/api/inventory", response_model=List[InventoryItem])
def get_inventory(
    warehouse: Optional[str] = None,
    category: Optional[str] = None
):
    """Get all inventory items with optional filtering"""
    return apply_filters(inventory_items, warehouse, category)

@app.get("/api/inventory/{item_id}", response_model=InventoryItem)
def get_inventory_item(item_id: str):
    """Get a specific inventory item"""
    item = next((item for item in inventory_items if item["id"] == item_id), None)
    if not item:
        raise HTTPException(status_code=404, detail="Item not found")
    return item

@app.get("/api/orders", response_model=List[Order])
def get_orders(
    warehouse: Optional[str] = None,
    category: Optional[str] = None,
    status: Optional[str] = None,
    month: Optional[str] = None
):
    """Get all orders with optional filtering"""
    filtered_orders = apply_filters(orders, warehouse, category, status)
    filtered_orders = filter_by_month(filtered_orders, month)
    return filtered_orders

@app.get("/api/orders/{order_id}", response_model=Order)
def get_order(order_id: str):
    """Get a specific order"""
    order = next((order for order in orders if order["id"] == order_id), None)
    if not order:
        raise HTTPException(status_code=404, detail="Order not found")
    return order

@app.get("/api/demand", response_model=List[DemandForecast])
def get_demand_forecasts():
    """Get demand forecasts"""
    return demand_forecasts

@app.get("/api/backlog", response_model=List[BacklogItem])
def get_backlog():
    """Get backlog items with purchase order status"""
    # Add has_purchase_order flag to each backlog item
    result = []
    for item in backlog_items:
        item_dict = dict(item)
        # Check if this backlog item has a purchase order.
        # .get() rather than [] because restocking orders carry no backlog_item_id — indexing
        # would raise KeyError and take this endpoint (and the dashboard) down.
        has_po = any(po.get("backlog_item_id") == item["id"] for po in purchase_orders)
        item_dict["has_purchase_order"] = has_po
        result.append(item_dict)
    return result

# Trend drives restocking urgency: rising demand must be covered first, falling demand last.
TREND_PRIORITY = {'increasing': 0, 'stable': 1, 'decreasing': 2}

# Suppliers quote in whole packs and budgets are set in round figures, so the slider snaps to this.
BUDGET_STEP = 1000.0

def build_restock_candidates() -> List[dict]:
    """Join demand forecasts with the restocking catalog and size an order line per SKU.

    A forecast SKU with no catalog row has no price anywhere in the dataset, so it comes back
    unpriced and unselectable instead of being silently dropped or crashing the join.
    """
    catalog_by_sku = {row['item_sku']: row for row in restock_catalog}
    candidates = []

    for forecast in demand_forecasts:
        catalog = catalog_by_sku.get(forecast['item_sku'])
        required = forecast['forecasted_demand']

        base = {
            'item_sku': forecast['item_sku'],
            'item_name': forecast['item_name'],
            'trend': forecast['trend'],
            'current_demand': forecast['current_demand'],
            'forecasted_demand': forecast['forecasted_demand'],
            'required_quantity': required,
            'selected': False,
        }

        if not catalog:
            candidates.append({
                **base,
                'order_quantity': 0,
                'pack_size': 0,
                'unit_cost': 0.0,
                'line_cost': 0.0,
                'supplier': '',
                'lead_time_days': 0,
                'reason': 'no_catalog_entry',
            })
            continue

        # Suppliers ship whole packs, so round the 30-day need up to the next full pack
        pack_size = catalog['pack_size'] or 1
        order_quantity = math.ceil(required / pack_size) * pack_size

        candidates.append({
            **base,
            'order_quantity': order_quantity,
            'pack_size': pack_size,
            'unit_cost': catalog['unit_cost'],
            'line_cost': round(order_quantity * catalog['unit_cost'], 2),
            'supplier': catalog['supplier'],
            'lead_time_days': catalog['lead_time_days'],
            'reason': 'over_budget',
        })

    return candidates

def rank_restock_candidates(candidates: List[dict]) -> List[dict]:
    """Order candidates by urgency: trend first, then relative growth, then cheapest line."""
    def sort_key(candidate):
        current = candidate['current_demand']
        # Relative growth, negated so the steepest climb sorts first
        growth = (candidate['forecasted_demand'] - current) / current if current > 0 else 0.0
        return (
            TREND_PRIORITY.get(candidate['trend'].lower(), len(TREND_PRIORITY)),
            -growth,
            candidate['line_cost'],
        )

    return sorted(candidates, key=sort_key)

@app.get("/api/restocking/recommendations", response_model=RestockRecommendationsResponse)
def get_restocking_recommendations(budget: Optional[float] = None):
    """Recommend which forecast items to restock within a budget.

    Returns every candidate with a `selected` flag rather than only the affordable ones, so the
    client can show what a bigger budget would buy. The slider range is derived from the data:
    at budget_max the whole restock fits.
    """
    if budget is not None and budget < 0:
        raise HTTPException(status_code=400, detail="Budget cannot be negative")

    candidates = rank_restock_candidates(build_restock_candidates())

    full_restock_cost = round(sum(c['line_cost'] for c in candidates), 2)
    # Round the ceiling up to a whole step so the slider's far end always affords everything
    budget_max = math.ceil(full_restock_cost / BUDGET_STEP) * BUDGET_STEP
    budget_default = math.floor((budget_max / 2) / BUDGET_STEP) * BUDGET_STEP
    effective_budget = budget_default if budget is None else budget

    # Greedy fill that skips instead of stopping: a cheap line further down the ranking can
    # still fit in the budget an expensive line above it could not.
    remaining = effective_budget
    for candidate in candidates:
        if candidate['line_cost'] > 0 and candidate['line_cost'] <= remaining:
            candidate['selected'] = True
            candidate['reason'] = candidate['trend'].lower()
            remaining = round(remaining - candidate['line_cost'], 2)

    selected = [c for c in candidates if c['selected']]

    return {
        'budget': effective_budget,
        'budget_min': 0.0,
        'budget_max': budget_max,
        'budget_step': BUDGET_STEP,
        'budget_default': budget_default,
        'total_cost': round(sum(c['line_cost'] for c in selected), 2),
        'total_units': sum(c['order_quantity'] for c in selected),
        'items_selected': len(selected),
        'items_total': len(candidates),
        'full_restock_cost': full_restock_cost,
        'recommendations': candidates,
    }

@app.post("/api/purchase-orders", response_model=PurchaseOrder, status_code=201)
def create_purchase_order(request: CreatePurchaseOrderRequest):
    """Submit a purchase order. Serves both the restocking tab and the backlog flow."""
    if not request.items:
        raise HTTPException(status_code=400, detail="A purchase order needs at least one line item")

    created = datetime.now()
    sequence = len(purchase_orders) + 1

    # Recompute line_cost server-side rather than trusting the client's arithmetic
    lines = []
    for line in request.items:
        line_dict = line.model_dump()
        line_dict['line_cost'] = round(line.quantity * line.unit_cost, 2)
        lines.append(line_dict)

    lead_time_days = max(line['lead_time_days'] for line in lines)

    purchase_order = {
        'id': str(sequence),
        'po_number': f"PO-{created.year}-{sequence:04d}",
        'items': lines,
        'total_cost': round(sum(line['line_cost'] for line in lines), 2),
        'total_units': sum(line['quantity'] for line in lines),
        'status': 'Submitted',
        'created_date': created.strftime('%Y-%m-%d'),
        'expected_delivery': (created + timedelta(days=lead_time_days)).strftime('%Y-%m-%d'),
        'lead_time_days': lead_time_days,
        'budget': request.budget,
        'backlog_item_id': request.backlog_item_id,
        'notes': request.notes,
    }

    # In-memory only: mock_data reloads from JSON at startup, so submitted orders don't survive
    # a server restart. Same trade-off as the rest of this demo's data layer.
    purchase_orders.append(purchase_order)
    return purchase_order

@app.get("/api/purchase-orders", response_model=List[PurchaseOrder])
def get_purchase_orders():
    """Get submitted purchase orders, most recent first."""
    return list(reversed(purchase_orders))

@app.get("/api/purchase-orders/{backlog_item_id}", response_model=PurchaseOrder)
def get_purchase_order_by_backlog_item(backlog_item_id: str):
    """Get the most recent purchase order raised for a backlog item."""
    for purchase_order in reversed(purchase_orders):
        if purchase_order.get('backlog_item_id') == backlog_item_id:
            return purchase_order

    raise HTTPException(
        status_code=404,
        detail=f"Purchase order for backlog item {backlog_item_id} not found"
    )

@app.get("/api/dashboard/summary")
def get_dashboard_summary(
    warehouse: Optional[str] = None,
    category: Optional[str] = None,
    status: Optional[str] = None,
    month: Optional[str] = None
):
    """Get summary statistics for dashboard with optional filtering"""
    # Filter inventory
    filtered_inventory = apply_filters(inventory_items, warehouse, category)

    # Filter orders
    filtered_orders = apply_filters(orders, warehouse, category, status)
    filtered_orders = filter_by_month(filtered_orders, month)

    total_inventory_value = sum(item["quantity_on_hand"] * item["unit_cost"] for item in filtered_inventory)
    low_stock_items = len([item for item in filtered_inventory if item["quantity_on_hand"] <= item["reorder_point"]])
    pending_orders = len([order for order in filtered_orders if order["status"] in ["Processing", "Backordered"]])
    total_backlog_items = len(backlog_items)

    return {
        "total_inventory_value": round(total_inventory_value, 2),
        "low_stock_items": low_stock_items,
        "pending_orders": pending_orders,
        "total_backlog_items": total_backlog_items,
        "total_orders_value": sum(order["total_value"] for order in filtered_orders)
    }

@app.get("/api/spending/summary")
def get_spending_summary():
    """Get spending summary statistics"""
    return spending_summary

@app.get("/api/spending/monthly")
def get_monthly_spending():
    """Get monthly spending breakdown"""
    return monthly_spending

@app.get("/api/spending/categories")
def get_category_spending():
    """Get spending by category"""
    return category_spending

@app.get("/api/spending/transactions")
def get_recent_transactions():
    """Get recent transactions"""
    return recent_transactions

@app.get("/api/reports/quarterly")
def get_quarterly_reports():
    """Get quarterly performance reports"""
    # Calculate quarterly statistics from orders
    quarters = {}

    for order in orders:
        order_date = order.get('order_date', '')
        # Determine quarter
        if '2025-01' in order_date or '2025-02' in order_date or '2025-03' in order_date:
            quarter = 'Q1-2025'
        elif '2025-04' in order_date or '2025-05' in order_date or '2025-06' in order_date:
            quarter = 'Q2-2025'
        elif '2025-07' in order_date or '2025-08' in order_date or '2025-09' in order_date:
            quarter = 'Q3-2025'
        elif '2025-10' in order_date or '2025-11' in order_date or '2025-12' in order_date:
            quarter = 'Q4-2025'
        else:
            continue

        if quarter not in quarters:
            quarters[quarter] = {
                'quarter': quarter,
                'total_orders': 0,
                'total_revenue': 0,
                'delivered_orders': 0,
                'avg_order_value': 0
            }

        quarters[quarter]['total_orders'] += 1
        quarters[quarter]['total_revenue'] += order.get('total_value', 0)
        if order.get('status') == 'Delivered':
            quarters[quarter]['delivered_orders'] += 1

    # Calculate averages and fulfillment rate
    result = []
    for q, data in quarters.items():
        if data['total_orders'] > 0:
            data['avg_order_value'] = round(data['total_revenue'] / data['total_orders'], 2)
            data['fulfillment_rate'] = round((data['delivered_orders'] / data['total_orders']) * 100, 1)
        result.append(data)

    # Sort by quarter
    result.sort(key=lambda x: x['quarter'])
    return result

@app.get("/api/reports/monthly-trends")
def get_monthly_trends():
    """Get month-over-month trends"""
    months = {}

    for order in orders:
        order_date = order.get('order_date', '')
        if not order_date:
            continue

        # Extract month (format: YYYY-MM-DD)
        month = order_date[:7]  # Gets YYYY-MM

        if month not in months:
            months[month] = {
                'month': month,
                'order_count': 0,
                'revenue': 0,
                'delivered_count': 0
            }

        months[month]['order_count'] += 1
        months[month]['revenue'] += order.get('total_value', 0)
        if order.get('status') == 'Delivered':
            months[month]['delivered_count'] += 1

    # Convert to list and sort
    result = list(months.values())
    result.sort(key=lambda x: x['month'])
    return result

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8001)
