#!/usr/bin/env python3
"""
Barcode Billing System
Scans barcodes -> looks up UPC/EAN in Supabase -> builds a local bill
"""

import os
import sys
from dotenv import load_dotenv
from supabase import create_client, Client
from datetime import datetime

load_dotenv()

SUPABASE_URL = os.environ.get("SUPABASE_URL", "YOUR_SUPABASE_URL")
SUPABASE_KEY = os.environ.get("SUPABASE_KEY", "YOUR_SUPABASE_KEY")
TABLE_NAME = "products"
ADD_PRODUCT_TRIGGER = "SYS_ADD_PRODUCT"

supabase: Client = create_client(SUPABASE_URL, SUPABASE_KEY)
bill: list[dict] = []


adding_product_mode = False

def insert_product(barcode: str, name: str, mrp: float, main_group: str = "", sub_group: str = "") -> bool:
    try:
        supabase.table(TABLE_NAME).insert({
            "upc_ean_code": barcode,
            "item_name": name,
            "mrp": mrp,
            "main_group": main_group,
            "sub_group": sub_group,
        }).execute()
        print(f"✓ Product added -> {name} (₹{mrp}) [{barcode}]")
        return True
    except Exception as e:
        print(f"✗ Failed to add product: {e}")
        return False

def lookup_barcode(barcode: str) -> dict | None:
    barcode = barcode.strip()
    if not barcode:
        return None

    response = (
        supabase.table(TABLE_NAME)
        .select("id, item_name, mrp, upc_ean_code")
        .eq("upc_ean_code", barcode)
        .limit(1)
        .execute()
    )
    return response.data[0] if response.data else None


def add_to_bill(product: dict) -> None:
    for item in bill:
        if item["upc_ean_code"] == product["upc_ean_code"]:
            item["qty"] += 1
            print(f"✓ Qty updated -> {product['item_name']} x{item['qty']}")
            return

    bill.append({
        "upc_ean_code": product["upc_ean_code"],
        "item_name": product["item_name"],
        "mrp": float(product["mrp"] or 0),
        "qty": 1,
    })
    print(f"✓ Added -> {product['item_name']}  ₹{product['mrp']}")


def print_bill() -> None:
    if not bill:
        print("No items scanned yet.")
        return

    width = 58
    print("\n" + "=" * width)
    print(f"{'RETAIL CONCIERGE BILL':^{width}}")
    print(f"{datetime.now().strftime('%d %b %Y  %I:%M %p'):^{width}}")
    print("-" * width)
    print(f"{'ITEM':<30} {'QTY':>5} {'MRP':>8} {'TOTAL':>10}")
    print("-" * width)

    grand_total = 0.0
    for item in bill:
        total = item["mrp"] * item["qty"]
        grand_total += total
        print(f"{item['item_name'][:30]:<30} {item['qty']:>5} {item['mrp']:>8.2f} {total:>10.2f}")

    print("-" * width)
    print(f"{'GRAND TOTAL':>45} ₹{grand_total:>10.2f}")
    print("=" * width + "\n")


def clear_bill() -> None:
    bill.clear()
    print("Bill cleared.")


def main() -> None:
    if "YOUR_SUPABASE" in SUPABASE_URL or "YOUR_SUPABASE" in SUPABASE_KEY:
        sys.exit("Error: Set SUPABASE_URL and SUPABASE_KEY in your .env file.")

    print("Retail Concierge Barcode Billing")
    print("Scan barcode, or type P=print, C=clear, Q=quit\n")

    while True:
        try:
            raw = input("SCAN > ").strip()
        except (KeyboardInterrupt, EOFError):
            print("\nExiting.")
            break

        if not raw:
            continue

        if raw == ADD_PRODUCT_TRIGGER:
            global adding_product_mode
            adding_product_mode = True
            print("\n>>> ADD PRODUCT MODE — scan the new product's barcode now <<<\n")
            continue

        if adding_product_mode:
            new_barcode = raw
            existing = lookup_barcode(new_barcode)
            if existing:
                print(f"⚠ Barcode already exists as '{existing['item_name']}'. Use edit mode instead.")
                adding_product_mode = False
                continue
            name = input("Product name: ").strip()
            try:
                mrp = float(input("MRP: ").strip())
            except ValueError:
                print("Invalid price, cancelling add.")
                adding_product_mode = False
                continue
            main_group = input("Category (optional): ").strip()
            insert_product(new_barcode, name, mrp, main_group)
            adding_product_mode = False
            continue

        cmd = raw.upper()
        if cmd == "Q":
            print("Exiting.")
            break
        elif cmd == "P":
            print_bill()
        elif cmd == "C":
            clear_bill()
        else:
            product = lookup_barcode(raw)
            if product:
                add_to_bill(product)
            else:
                print(f"No product found for barcode: {raw}")


if __name__ == "__main__":
    main()