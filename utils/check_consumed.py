#!/usr/bin/env python3
"""
Check whether a Base->Algorand NTT VAA has been consumed on Algorand.

Usage:
    python check_consumed.py <sequence>
    python check_consumed.py <sequence> --chain 30 --emitter <32-byte-hex>
    BASE_EMITTER=<hex> python check_consumed.py <sequence>
"""
import argparse, base64, os, sys
from urllib.parse import quote

import requests
from Crypto.Hash import keccak

WORMHOLESCAN = "https://api.wormholescan.io/api/v1/vaas"
ALGOD = "https://mainnet-api.algonode.cloud"

WT_APP = int(os.environ.get("WT_APP_ID", "0"))           # WormholeTransceiver
TRANSCEIVER_MGR = int(os.environ.get("TRANSCEIVER_MGR_APP_ID", "0"))  # TransceiverManager

DEFAULT_CHAIN = 30
DEFAULT_EMITTER = os.environ.get("BASE_EMITTER", "")  # 32-byte hex; set BASE_EMITTER in env


def keccak256(data: bytes) -> bytes:
    k = keccak.new(digest_bits=256)
    k.update(data)
    return k.digest()


def box_exists(app_id: int, name_bytes: bytes) -> bool:
    b64 = quote(base64.b64encode(name_bytes).decode(), safe="")
    r = requests.get(f"{ALGOD}/v2/applications/{app_id}/box?name=b64:{b64}", timeout=10)
    return r.status_code == 200


def fetch_vaa(chain: int, emitter: str, sequence: int) -> bytes:
    url = f"{WORMHOLESCAN}/{chain}/{emitter}/{sequence}"
    r = requests.get(url, timeout=15)
    r.raise_for_status()
    return base64.b64decode(r.json()["data"]["vaa"])


def parse_vaa_body(vaa: bytes) -> bytes:
    """Return the VAA body (timestamp onward) — keccak256 of this is the vaa_digest."""
    # version(1) + guardian_set_index(4) + num_sigs(1) + sigs(num_sigs * 66)
    num_sigs = vaa[5]
    return vaa[6 + num_sigs * 66:]


def main():
    ap = argparse.ArgumentParser(description="Check Base->Algorand NTT VAA consumption status")
    ap.add_argument("sequence", type=int, help="Wormhole sequence number")
    ap.add_argument("--chain", type=int, default=DEFAULT_CHAIN,
                    help=f"Source chain ID (default {DEFAULT_CHAIN} = Base)")
    ap.add_argument("--emitter", default=DEFAULT_EMITTER,
                    help="Emitter address (32-byte hex, no 0x)")
    args = ap.parse_args()

    print(f"Sequence : {args.sequence}")
    print(f"Chain    : {args.chain}")
    print(f"Emitter  : {args.emitter}")

    print("\nFetching VAA from WormholeScan...", end=" ", flush=True)
    try:
        vaa = fetch_vaa(args.chain, args.emitter, args.sequence)
    except Exception as e:
        print(f"FAILED\n  {e}")
        sys.exit(1)
    print(f"OK ({len(vaa)} bytes)")

    body = parse_vaa_body(vaa)
    vaa_digest = keccak256(body)
    print(f"VAA digest: {vaa_digest.hex()}")

    emitter_chain = int.from_bytes(body[8:10], "big")
    emitter_addr  = body[10:42]
    sequence_body = int.from_bytes(body[42:50], "big")
    print(f"\nVAA body fields:")
    print(f"  emitter_chain = {emitter_chain}")
    print(f"  emitter_addr  = {emitter_addr.hex()}")
    print(f"  sequence      = {sequence_body}")

    print("\nChecking on-chain state:")

    consumed_name = b"vaas_consumed_" + vaa_digest
    consumed = box_exists(WT_APP, consumed_name)
    print(f"  [{WT_APP}] vaas_consumed_  : "
          f"{'YES — Step 1 already relayed' if consumed else 'NO  — not yet relayed'}")

    chain_bytes = args.chain.to_bytes(2, "big")
    emitter_bytes = bytes.fromhex(args.emitter)
    peer_name = b"wormhole_peer_" + chain_bytes + emitter_bytes
    peer = box_exists(WT_APP, peer_name)
    print(f"  [{WT_APP}] wormhole_peer_  : "
          f"{'registered' if peer else 'MISSING — peer not configured!'}")

    if not consumed:
        print("\nResult: VAA not yet consumed on Algorand. Relay Step 1 via base-relayer.py.")
    else:
        print("\nResult: VAA consumed by WT. Check ntt_execute.py output for Step 2 status.")


if __name__ == "__main__":
    main()
