#!/usr/bin/env python3
"""
Inspect Algorand NTT transfer transactions via the indexer.

Usage:
    python inspect_tx.py <TX_ID> [<TX_ID> ...]
"""
import argparse, base64
import requests

INDEXER = "https://mainnet-idx.algonode.cloud"


def inspect_tx(tx_id: str) -> None:
    print(f"\n{'='*60}")
    print(f"TX: {tx_id}")
    resp = requests.get(f"{INDEXER}/v2/transactions/{tx_id}", timeout=15)
    if not resp.ok:
        print(f"  ERROR: {resp.status_code}")
        return

    txn = resp.json().get("transaction", {})
    print(f"  Type: {txn.get('tx-type', '?')}")

    appl = txn.get("application-transaction", {})
    if appl:
        print(f"  App ID:         {appl.get('application-id')}")
        print(f"  Foreign Apps:   {appl.get('foreign-apps', [])}")
        print(f"  Foreign Assets: {appl.get('foreign-assets', [])}")
        print(f"  Accounts:       {appl.get('accounts', [])}")
        decoded_args = []
        for a in appl.get("application-args", []):
            b = base64.b64decode(a)
            decoded_args.append(f"hex={b.hex()} len={len(b)}")
        print(f"  App Args:       {decoded_args}")
        print(f"  On Complete:    {appl.get('on-completion')}")

    pay = txn.get("payment-transaction", {})
    if pay:
        print(f"  Receiver: {pay.get('receiver')}  Amount: {pay.get('amount')}")

    axfer = txn.get("asset-transfer-transaction", {})
    if axfer:
        print(f"  Asset ID: {axfer.get('asset-id')}  Amount: {axfer.get('amount')}")
        print(f"  Receiver: {axfer.get('receiver')}")

    print(f"  Fee: {txn.get('fee', 0)} microALGO")

    inners = txn.get("inner-txns", [])
    if inners:
        print(f"  Inner txns: {len(inners)}")
        for i, inner in enumerate(inners):
            iappl  = inner.get("application-transaction", {})
            ipay   = inner.get("payment-transaction", {})
            iaxfer = inner.get("asset-transfer-transaction", {})
            if iappl:
                print(f"    [{i}] appl → app {iappl.get('application-id')}  "
                      f"foreign_apps={iappl.get('foreign-apps',[])}  "
                      f"accounts={iappl.get('accounts',[])}")
            elif ipay:
                print(f"    [{i}] pay  → {ipay.get('receiver')}  amt={ipay.get('amount')}")
            elif iaxfer:
                print(f"    [{i}] axfer asset={iaxfer.get('asset-id')} "
                      f"amt={iaxfer.get('amount')} → {iaxfer.get('receiver')}")
            else:
                print(f"    [{i}] {inner.get('tx-type', '?')}")


def main():
    ap = argparse.ArgumentParser(description="Inspect Algorand NTT transfer transactions")
    ap.add_argument("tx_ids", nargs="+", metavar="TX_ID",
                    help="Algorand transaction ID(s) to inspect")
    args = ap.parse_args()

    for tx_id in args.tx_ids:
        inspect_tx(tx_id)


if __name__ == "__main__":
    main()
