"""Generate a large OpenAPI file for the demo. One endpoint stays easy to retrieve."""

from __future__ import annotations

import json
from pathlib import Path

RESOURCES = [
    ("customers", "Customer", "People who place orders."),
    ("orders", "Order", "Purchases waiting for payment or delivery."),
    ("payments", "Payment", "Money captured for an order."),
    ("refunds", "Refund", "Money returned to a customer."),
    ("invoices", "Invoice", "Bills sent after checkout."),
    ("products", "Product", "Items that can be sold."),
    ("inventory", "StockItem", "How many units are in a warehouse."),
    ("warehouses", "Warehouse", "Places where stock is stored."),
    ("shipments", "Shipment", "Packages on the way to a customer."),
    ("addresses", "Address", "Shipping and billing addresses."),
    ("coupons", "Coupon", "Discounts applied at checkout."),
    ("carts", "Cart", "Items a customer has not paid for yet."),
    ("reviews", "Review", "Ratings left on a product."),
    ("vendors", "Vendor", "Sellers who list products."),
    ("payouts", "Payout", "Money sent to a vendor."),
    ("subscriptions", "Subscription", "Recurring purchases."),
    ("plans", "Plan", "Prices for a subscription."),
    ("notifications", "Notification", "Messages sent to a customer."),
    ("webhooks", "Webhook", "Callbacks sent to another service."),
    ("api-keys", "ApiKey", "Keys a developer uses to call the API."),
]

METHODS = ("get", "post", "put", "patch", "delete")
PADDING = (
    "This field exists so the full specification is large. "
    "An agent should not read this whole document. "
    "Ask for one endpoint and use only that slice. "
) * 8


def schema(name: str) -> dict:
    return {
        "type": "object",
        "required": ["id", "name"],
        "properties": {
            "id": {"type": "string", "description": PADDING},
            "name": {"type": "string", "description": PADDING},
            "status": {"type": "string", "description": PADDING},
            "createdAt": {"type": "string", "format": "date-time", "description": PADDING},
            "notes": {"type": "string", "description": PADDING},
        },
    }


def operation(resource: str, model: str, method: str, summary: str) -> dict:
    body = {
        "required": True,
        "content": {"application/json": {"schema": {"$ref": f"#/components/schemas/{model}"}}},
    }
    responses = {
        "200": {
            "description": f"{model} result. {PADDING}",
            "content": {"application/json": {"schema": {"$ref": f"#/components/schemas/{model}"}}},
        },
        "400": {"description": "The request body failed validation."},
        "404": {"description": f"{model} was not found."},
    }
    op = {
        "operationId": f"{method}_{resource.replace('-', '_')}",
        "summary": summary,
        "tags": [resource],
        "responses": responses,
    }
    if method in {"post", "put", "patch"}:
        op["requestBody"] = body
    return op


def main() -> None:
    paths: dict = {
        "/api/v1/checkout/pay": {
            "post": {
                "operationId": "payForOrder",
                "summary": "Pay for an order with a saved card",
                "tags": ["checkout"],
                "requestBody": {
                    "required": True,
                    "content": {
                        "application/json": {
                            "schema": {"$ref": "#/components/schemas/CheckoutPayment"}
                        }
                    },
                },
                "responses": {
                    "200": {
                        "description": "Payment accepted",
                        "content": {
                            "application/json": {
                                "schema": {"$ref": "#/components/schemas/CheckoutReceipt"}
                            }
                        },
                    },
                    "402": {"description": "The card was declined."},
                },
            }
        }
    }
    schemas = {
        "CheckoutPayment": {
            "type": "object",
            "required": ["orderId", "cardToken", "amount"],
            "properties": {
                "orderId": {"type": "string"},
                "cardToken": {"type": "string", "description": "Saved card token, not the raw card number."},
                "amount": {"type": "integer", "description": "Amount in minor units."},
                "currency": {"type": "string", "example": "USD"},
            },
        },
        "CheckoutReceipt": {
            "type": "object",
            "required": ["paymentId", "status"],
            "properties": {
                "paymentId": {"type": "string"},
                "status": {"type": "string", "example": "paid"},
            },
        },
    }
    for resource, model, about in RESOURCES:
        schemas[model] = schema(model)
        for method in METHODS:
            path = f"/api/v1/{resource}" if method == "post" else f"/api/v1/{resource}/{{id}}"
            if method == "get" and path.endswith("/{id}"):
                path = f"/api/v1/{resource}"
            paths.setdefault(path, {})[method] = operation(
                resource, model, method, f"{method.upper()} {resource}. {about}"
            )
    spec = {
        "openapi": "3.0.3",
        "info": {
            "title": "Marketplace API",
            "version": "4.2.0",
            "description": "A large commerce API. Agents should request one endpoint, not this whole file.",
        },
        "paths": paths,
        "components": {"schemas": schemas},
    }
    destination = Path(__file__).resolve().parent / "marketplace.openapi.json"
    destination.write_text(json.dumps(spec, ensure_ascii=False, indent=2), encoding="utf-8")
    print(destination)
    print("bytes", destination.stat().st_size)
    print("paths", len(paths))


if __name__ == "__main__":
    main()
