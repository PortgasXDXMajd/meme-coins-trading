"""Create the desk's dedicated hot wallet.

    uv run python scripts/new_wallet.py

Writes the secret key to secrets/wallet.json (gitignored, owner-read-only) and
prints the public address to fund. Never reuse a personal wallet for the desk,
and only send it what you are prepared to lose.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

from solders.keypair import Keypair

KEYFILE = Path("secrets/wallet.json")


def main() -> None:
    if KEYFILE.exists():
        sys.exit(f"{KEYFILE} already exists. Refusing to overwrite a wallet key.")
    KEYFILE.parent.mkdir(parents=True, exist_ok=True)
    keypair = Keypair()
    KEYFILE.touch(mode=0o600)
    KEYFILE.write_text(json.dumps(list(bytes(keypair))))
    print(f"Wallet created. Key saved to {KEYFILE} (back this file up somewhere safe).")
    print(f"Fund this address with SOL: {keypair.pubkey()}")


if __name__ == "__main__":
    main()
