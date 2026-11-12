"""Live broker: real swaps through Jupiter, signed by the desk's own hot wallet.

Only constructed when TRADING_MODE=live. Not exercised by the test suite with
real funds; do a ~$1 round trip by hand before trusting it with the bankroll.
"""

from __future__ import annotations

import base64
import json
from pathlib import Path

from solders.keypair import Keypair
from solders.message import to_bytes_versioned
from solders.transaction import VersionedTransaction

from desk.brokers.base import BrokerError
from desk.data.http import DataError
from desk.data.jupiter import LAMPORTS_PER_SOL, Jupiter, Order, usd_to_lamports
from desk.data.solana_rpc import SolanaRpc
from desk.domain.models import Fill

# SOL kept back for transaction fees and token-account rent.
FEE_RESERVE_SOL = 0.02


def load_keypair(path: Path) -> Keypair:
    """Read a solana-keygen style JSON array of 64 bytes."""
    try:
        secret = bytes(json.loads(path.read_text()))
    except (OSError, ValueError) as exc:
        raise BrokerError(f"cannot read wallet key at {path}: {exc}") from exc
    return Keypair.from_bytes(secret)


def sign_order(order: Order, keypair: Keypair) -> str:
    """Add our signature to the order's transaction, leaving other signers' slots intact."""
    if not order.transaction:
        raise BrokerError("Jupiter order came back without a transaction to sign")
    transaction = VersionedTransaction.from_bytes(base64.b64decode(order.transaction))
    message = transaction.message
    signers = list(message.account_keys)[: message.header.num_required_signatures]
    try:
        slot = signers.index(keypair.pubkey())
    except ValueError as exc:
        raise BrokerError("desk wallet is not a signer on the order transaction") from exc
    signatures = list(transaction.signatures)
    signatures[slot] = keypair.sign_message(to_bytes_versioned(message))
    signed = VersionedTransaction.populate(message, signatures)
    return base64.b64encode(bytes(signed)).decode()


class JupiterLiveBroker:
    def __init__(self, jupiter: Jupiter, rpc: SolanaRpc, keypair: Keypair) -> None:
        self._jupiter = jupiter
        self._rpc = rpc
        self._keypair = keypair
        self._address = str(keypair.pubkey())

    @property
    def address(self) -> str:
        return self._address

    async def cash_usd(self, sol_price_usd: float) -> float:
        try:
            balance = await self._rpc.sol_balance(self._address)
        except DataError as exc:
            raise BrokerError(f"cannot read wallet balance: {exc}") from exc
        return max(0.0, balance - FEE_RESERVE_SOL) * sol_price_usd

    async def buy(self, mint: str, usd: float, sol_price_usd: float) -> Fill:
        try:
            order = await self._jupiter.buy_quote(
                mint, usd_to_lamports(usd, sol_price_usd), taker=self._address
            )
            execution = await self._jupiter.execute(
                sign_order(order, self._keypair), order.request_id
            )
        except DataError as exc:
            raise BrokerError(f"live buy failed: {exc}") from exc
        return Fill(
            side="buy",
            mint=mint,
            token_amount_raw=execution.total_output_amount,
            usd_value=execution.total_input_amount / LAMPORTS_PER_SOL * sol_price_usd,
            tx_id=execution.signature,
            simulated=False,
        )

    async def sell(self, mint: str, token_amount_raw: int, sol_price_usd: float) -> Fill:
        try:
            order = await self._jupiter.sell_quote(mint, token_amount_raw, taker=self._address)
            execution = await self._jupiter.execute(
                sign_order(order, self._keypair), order.request_id
            )
        except DataError as exc:
            raise BrokerError(f"live sell failed: {exc}") from exc
        return Fill(
            side="sell",
            mint=mint,
            token_amount_raw=execution.total_input_amount,
            usd_value=execution.total_output_amount / LAMPORTS_PER_SOL * sol_price_usd,
            tx_id=execution.signature,
            simulated=False,
        )
