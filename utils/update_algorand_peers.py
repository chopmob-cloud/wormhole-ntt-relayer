#!/usr/bin/env python3
"""
update_algorand_peers.py

Updates Algorand NTT Manager and WormholeTransceiver peer addresses
to point to new Base contracts after redeployment.

This replaces runbook steps 5-6 (the Algorand-side peer configuration).

Requirements (in base_to_algo/venv):
    pip install py-algorand-sdk python-dotenv

Usage:
    source base_to_algo/venv/bin/activate
    python3 utils/update_algorand_peers.py <NEW_NTT_MANAGER> <NEW_WT>

Example:
    python3 update_algorand_peers.py 0x1234...abcd 0x5678...ef01

What it does:
    1. Calls setPeer (d0c3da45) on the Algorand NTT Manager app (NTT_MGR_APP_ID in .env)
       with (chain=30, NEW_NTT padded to bytes32, decimals=6)
    2. Calls setWormholePeer (d6e1167f) on the Algorand WormholeTransceiver app (WT_APP_ID in .env)
       with (chain=30, NEW_WT padded to bytes32)
"""

import os
import sys
import time
import base64
import algosdk
from algosdk import transaction, mnemonic as algo_mnemonic
from algosdk.v2client import algod
from dotenv import load_dotenv

load_dotenv()

# --------------------------------------------------
# CONFIG — must match base_to_algo_relayer.py
# --------------------------------------------------

ALGO_ALGOD_URL   = os.getenv("ALGO_ALGOD_URL",  "https://mainnet-api.algonode.cloud")
ALGO_ALGOD_TOKEN = os.getenv("ALGO_ALGOD_TOKEN", "")
RELAYER_MNEMONIC = os.getenv("ALGO_MNEMONIC") or os.getenv("ALGORAND_MAINNET_ACCOUNT")

NTT_MANAGER_APP_ID          = int(os.getenv("NTT_MGR_APP_ID", "0"))
WORMHOLE_TRANSCEIVER_APP_ID = int(os.getenv("WT_APP_ID", "0"))

BASE_WORMHOLE_CHAIN_ID = 30  # Wormhole chain ID for Base

# ARC-4 method selectors (from NTT_BRIDGE_STATUS.md)
SET_PEER_SELECTOR           = bytes.fromhex("d0c3da45")
SET_WORMHOLE_PEER_SELECTOR  = bytes.fromhex("d6e1167f")

# --------------------------------------------------
# HELPERS
# --------------------------------------------------

def evm_address_to_bytes32(evm_address: str) -> bytes:
    """Convert 0x-prefixed EVM address to 32-byte left-padded bytes."""
    addr = evm_address.lower().replace("0x", "")
    if len(addr) != 40:
        raise ValueError(f"Invalid EVM address length: {len(addr)} (expected 40 hex chars)")
    return bytes.fromhex(addr.rjust(64, "0"))


def get_algod_client():
    return algod.AlgodClient(ALGO_ALGOD_TOKEN, ALGO_ALGOD_URL)


def load_account():
    if not RELAYER_MNEMONIC:
        raise RuntimeError("ALGORAND_MAINNET_ACCOUNT not set in .env")
    private_key = algo_mnemonic.to_private_key(RELAYER_MNEMONIC)
    address = algosdk.account.address_from_private_key(private_key)
    return private_key, address

# --------------------------------------------------
# PEER UPDATE TRANSACTIONS
# --------------------------------------------------

def set_ntt_manager_peer(client, private_key, sender, new_ntt_bytes32: bytes):
    """
    Call setPeer (d0c3da45) on Algorand NTT Manager.
    ARC-4 args: selector + uint16(chain) + bytes32(peer) + uint8(decimals)
    """
    chain_bytes = BASE_WORMHOLE_CHAIN_ID.to_bytes(2, "big")  # uint16
    decimals_byte = (6).to_bytes(1, "big")                    # uint8

    sp = client.suggested_params()
    sp.flat_fee = True
    sp.fee = 2000

    txn = transaction.ApplicationCallTxn(
        sender=sender,
        index=NTT_MANAGER_APP_ID,
        on_complete=transaction.OnComplete.NoOpOC,
        app_args=[
            SET_PEER_SELECTOR,
            chain_bytes,
            new_ntt_bytes32,
            decimals_byte,
        ],
        sp=sp,
    )

    signed = txn.sign(private_key)
    encoded = algosdk.encoding.msgpack_encode(signed)
    raw = base64.b64decode(encoded) if isinstance(encoded, str) else encoded
    raw_b64 = base64.b64encode(raw).decode()

    tx_id = client.send_raw_transaction(raw_b64)
    if not isinstance(tx_id, str):
        tx_id = tx_id.get("txId", "")

    algosdk.transaction.wait_for_confirmation(client, tx_id, 12)
    return tx_id


def set_wormhole_transceiver_peer(client, private_key, sender, new_wt_bytes32: bytes):
    """
    Call setWormholePeer (d6e1167f) on Algorand WormholeTransceiver.
    ARC-4 args: selector + uint16(chain) + bytes32(peer)
    """
    chain_bytes = BASE_WORMHOLE_CHAIN_ID.to_bytes(2, "big")  # uint16

    sp = client.suggested_params()
    sp.flat_fee = True
    sp.fee = 2000

    txn = transaction.ApplicationCallTxn(
        sender=sender,
        index=WORMHOLE_TRANSCEIVER_APP_ID,
        on_complete=transaction.OnComplete.NoOpOC,
        app_args=[
            SET_WORMHOLE_PEER_SELECTOR,
            chain_bytes,
            new_wt_bytes32,
        ],
        sp=sp,
    )

    signed = txn.sign(private_key)
    encoded = algosdk.encoding.msgpack_encode(signed)
    raw = base64.b64decode(encoded) if isinstance(encoded, str) else encoded
    raw_b64 = base64.b64encode(raw).decode()

    tx_id = client.send_raw_transaction(raw_b64)
    if not isinstance(tx_id, str):
        tx_id = tx_id.get("txId", "")

    algosdk.transaction.wait_for_confirmation(client, tx_id, 12)
    return tx_id

# --------------------------------------------------
# MAIN
# --------------------------------------------------

def main():
    if not NTT_MANAGER_APP_ID or not WORMHOLE_TRANSCEIVER_APP_ID:
        print("ERROR: set NTT_MGR_APP_ID and WT_APP_ID in .env before running")
        sys.exit(1)

    if len(sys.argv) != 3:
        print(f"Usage: {sys.argv[0]} <NEW_NTT_MANAGER_ADDRESS> <NEW_WT_ADDRESS>")
        print(f"Example: {sys.argv[0]} 0x1234...abcd 0x5678...ef01")
        sys.exit(1)

    new_ntt_address = sys.argv[1]
    new_wt_address  = sys.argv[2]

    # Validate addresses
    for label, addr in [("NTT Manager", new_ntt_address), ("WT", new_wt_address)]:
        if not addr.startswith("0x") or len(addr) != 42:
            print(f"ERROR: {label} address must be 0x-prefixed and 42 chars (got '{addr}')")
            sys.exit(1)

    new_ntt_bytes32 = evm_address_to_bytes32(new_ntt_address)
    new_wt_bytes32  = evm_address_to_bytes32(new_wt_address)

    print("=" * 50)
    print(" Algorand NTT Peer Update")
    print("=" * 50)
    print(f"  New NTT Manager: {new_ntt_address}")
    print(f"    -> bytes32:    0x{new_ntt_bytes32.hex()}")
    print(f"  New WT:          {new_wt_address}")
    print(f"    -> bytes32:    0x{new_wt_bytes32.hex()}")
    print(f"  Target chain:    {BASE_WORMHOLE_CHAIN_ID} (Base)")
    print(f"  NTT Manager App: {NTT_MANAGER_APP_ID}")
    print(f"  WT App:          {WORMHOLE_TRANSCEIVER_APP_ID}")
    print()

    # Confirm before proceeding — this is a mainnet operation
    confirm = input("Type YES to proceed with mainnet peer update: ").strip()
    if confirm != "YES":
        print("Aborted.")
        sys.exit(0)

    # Load account and connect
    private_key, sender = load_account()
    print(f"\nRelayer address: {sender}")

    client = get_algod_client()
    status = client.status()
    print(f"Algod connected, round: {status['last-round']}")

    # Step 1: Update NTT Manager peer
    print(f"\n[1/2] Setting NTT Manager peer (App {NTT_MANAGER_APP_ID})...")
    print(f"       setPeer(chain={BASE_WORMHOLE_CHAIN_ID}, peer=0x{new_ntt_bytes32.hex()}, decimals=6)")
    try:
        tx_id = set_ntt_manager_peer(client, private_key, sender, new_ntt_bytes32)
        print(f"       ✓ Confirmed: {tx_id}")
    except Exception as e:
        print(f"       ✗ FAILED: {e}")
        print(f"       Fix and re-run. WT peer NOT yet updated.")
        sys.exit(1)

    # Step 2: Update WormholeTransceiver peer
    print(f"\n[2/2] Setting WormholeTransceiver peer (App {WORMHOLE_TRANSCEIVER_APP_ID})...")
    print(f"       setWormholePeer(chain={BASE_WORMHOLE_CHAIN_ID}, peer=0x{new_wt_bytes32.hex()})")
    try:
        tx_id = set_wormhole_transceiver_peer(client, private_key, sender, new_wt_bytes32)
        print(f"       ✓ Confirmed: {tx_id}")
    except Exception as e:
        print(f"       ✗ FAILED: {e}")
        print(f"       NTT Manager peer WAS updated. Fix this and re-run.")
        sys.exit(1)

    print(f"\n{'=' * 50}")
    print(f" Both peers updated successfully")
    print(f"{'=' * 50}")
    print(f"\nVerify on AlgoExplorer:")
    print(f"  NTT Manager: https://allo.info/application/{NTT_MANAGER_APP_ID}")
    print(f"  WT:          https://allo.info/application/{WORMHOLE_TRANSCEIVER_APP_ID}")


if __name__ == "__main__":
    main()
