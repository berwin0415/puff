"""Generate variable, reproducible catalogue data without embedding task answers."""

import hashlib
import random


GENERATOR_VERSION = 1


def items_for(seed: int, user_id: str, session_id: str) -> list[dict]:
    if type(seed) is not int or seed < 0:
        raise ValueError("seed must be a nonnegative integer")
    salt = int.from_bytes(hashlib.sha256(f"{user_id}\0{session_id}".encode()).digest()[:8], "big")
    random_source = random.Random(seed ^ salt)
    categories = ("office", "kitchen", "garden", "travel")
    return [
        {
            "item_id": f"item-{seed}-{index:02d}",
            "name": f"{categories[index % len(categories)]} item {index}",
            "category": categories[index % len(categories)],
            "unit_price_cents": random_source.randint(100, 5000),
            "available_quantity": random_source.randint(1, 20),
        }
        for index in range(1, 18 + seed % 9)
    ]
