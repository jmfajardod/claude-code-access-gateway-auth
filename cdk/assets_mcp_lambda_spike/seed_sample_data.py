"""Deterministic sample CSV generator for the query_data tool.

Generates a 2000-row sales dataset under `cdk/assets/sample_data/` which is then
uploaded to S3 via `aws_s3_deployment.BucketDeployment`.  The RNG seed is fixed
so the asset hash stays stable between synths (no spurious redeploys).
"""

import csv
import random
from datetime import date, timedelta
from pathlib import Path

_ROW_COUNT = 2000
_SEED = 42

_REGIONS = ("NA", "EU", "APAC", "LATAM")
_CHANNELS = ("Online", "In-Store", "Mobile")

_PRODUCTS: dict[str, list[tuple[str, float]]] = {
    "Electronics": [
        ("Laptop", 1200.0),
        ("Headphones", 180.0),
        ("Smartphone", 850.0),
        ("Tablet", 500.0),
        ("Smartwatch", 320.0),
    ],
    "Clothing": [
        ("T-Shirt", 25.0),
        ("Jeans", 65.0),
        ("Jacket", 140.0),
        ("Sneakers", 95.0),
        ("Dress", 110.0),
    ],
    "Home": [
        ("Coffee Maker", 90.0),
        ("Vacuum", 220.0),
        ("Bed Linen", 75.0),
        ("Cookware Set", 160.0),
        ("Lamp", 45.0),
    ],
    "Sports": [
        ("Yoga Mat", 40.0),
        ("Dumbbells", 85.0),
        ("Running Shoes", 130.0),
        ("Bicycle", 650.0),
        ("Tennis Racket", 120.0),
    ],
    "Books": [
        ("Fiction Novel", 18.0),
        ("Cookbook", 30.0),
        ("Biography", 24.0),
        ("Textbook", 85.0),
        ("Children's Book", 15.0),
    ],
}

_START_DATE = date(2025, 1, 1)
_END_DATE = date(2025, 12, 31)
_DATE_SPAN_DAYS = (_END_DATE - _START_DATE).days
_CUSTOMER_COUNT = 250


def _generate_rows(rng: random.Random) -> list[dict]:
    rows: list[dict] = []
    for i in range(1, _ROW_COUNT + 1):
        category = rng.choice(list(_PRODUCTS.keys()))
        product_name, base_price = rng.choice(_PRODUCTS[category])
        quantity = rng.randint(1, 10)
        unit_price = round(base_price * rng.uniform(0.85, 1.15), 2)
        total_price = round(unit_price * quantity, 2)
        order_day = _START_DATE + timedelta(days=rng.randint(0, _DATE_SPAN_DAYS))
        customer_id = f"CUST-{rng.randint(1, _CUSTOMER_COUNT):04d}"
        rows.append(
            {
                "order_id": f"ORD-{i:05d}",
                "order_date": order_day.isoformat(),
                "region": rng.choice(_REGIONS),
                "channel": rng.choice(_CHANNELS),
                "product_category": category,
                "product": product_name,
                "quantity": quantity,
                "unit_price": f"{unit_price:.2f}",
                "total_price": f"{total_price:.2f}",
                "customer_id": customer_id,
            }
        )
    return rows


def ensure_sample_csv() -> Path:
    """Write the sample CSV if missing and return the directory to upload."""
    out_dir = Path(__file__).parent / "sample_data"
    out_dir.mkdir(parents=True, exist_ok=True)
    csv_path = out_dir / "sample_sales.csv"

    if csv_path.exists():
        return out_dir

    rng = random.Random(_SEED)
    rows = _generate_rows(rng)

    with csv_path.open("w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=list(rows[0].keys()))
        writer.writeheader()
        writer.writerows(rows)

    return out_dir


if __name__ == "__main__":
    path = ensure_sample_csv()
    print(f"Sample data written to: {path}")
