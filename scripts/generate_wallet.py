"""Generate or inspect a standard Solana keypair for Botsensai live execution."""

from __future__ import annotations

import json
from pathlib import Path
from solders.keypair import Keypair

DEFAULT_PATH = Path.home() / ".config" / "solana" / "id.json"


def ensure_keypair(path: Path = DEFAULT_PATH) -> Keypair:
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        with open(path) as f:
            data = json.load(f)
            kp = Keypair.from_bytes(bytes(data))
            print(f"Loaded existing Solana keypair from: {path}")
            print(f"Public Key: {kp.pubkey()}")
            return kp

    kp = Keypair()
    with open(path, "w") as f:
        json.dump(list(bytes(kp)), f)
    path.chmod(0o600)
    print(f"Generated new Solana keypair at: {path}")
    print(f"Public Key: {kp.pubkey()}")
    return kp


if __name__ == "__main__":
    ensure_keypair()
