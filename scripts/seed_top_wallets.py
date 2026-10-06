#!/usr/bin/env python3
"""Seed the hot‑wallet with top‑trader mints.
This placeholder script reads the `top_traders.json` schema and prints each mint.
Replace the print loop with actual on‑chain airdrop or funding logic as needed.
"""
import json
import os

def main():
    schema_path = os.path.join(
        os.path.dirname(__file__), "..", "src", "botsensai", "schemas", "top_traders.json"
    )
    with open(schema_path, "r") as f:
        data = json.load(f)
    traders = data.get("traders", [])
    print("Found", len(traders), "traders")
    for trader in traders:
        print(trader.get("mint"))

if __name__ == "__main__":
    main()
