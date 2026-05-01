#!/usr/bin/env python3
"""
NTT Payload Diagnostic
Dumps raw VAA/NTT fields + on-chain NTT_MGR state so we can verify
parse_ntt_message_fields layout before attempting execute_message.

Usage: python3 ntt_debug.py <sequence>
"""
import base64, os, sys, requests
from pathlib import Path
from Crypto.Hash import keccak as _keccak

# ── Config — set via environment or .env ──────────────────────────────────────
import os as _os
ALGOD_URL       = _os.getenv("ALGOD_URL",             "https://mainnet-api.4160.nodely.dev")
NTT_MGR         = int(_os.getenv("NTT_MGR_APP_ID",         "0"))
TRANSCEIVER_MGR = int(_os.getenv("TRANSCEIVER_MGR_APP_ID", "0"))
WT_APP          = int(_os.getenv("WT_APP_ID",               "0"))
EMITTER         = _os.getenv("BASE_EMITTER", "")  # 32-byte hex, no 0x
CHAIN           = int(_os.getenv("BASE_CHAIN", "30"))
SIG_LEN         = 66

def keccak256(data):
    h = _keccak.new(digest_bits=256); h.update(data); return h.digest()

def parse_vaa(vaa_bytes):
    num_sigs   = vaa_bytes[5]
    body_start = 6 + num_sigs * SIG_LEN
    body       = vaa_bytes[body_start:]
    return {
        "emitter_chain": int.from_bytes(body[8:10],  "big"),
        "emitter_address": body[10:42],
        "sequence":       int.from_bytes(body[42:50], "big"),
        "payload":        body[51:],
        "vaa_digest":     keccak256(keccak256(body)),
        "body_start":     body_start,
        "body":           body,
    }

def hex_block(label, data, indent=2):
    sp = " " * indent
    print(f"{sp}{label} ({len(data)}b): {data.hex()}")
    # Try ASCII decode
    try:
        txt = data.decode("ascii")
        if txt.isprintable():
            print(f"{sp}  → ascii: {txt!r}")
    except Exception:
        pass

def fetch_box(app_id, name):
    from urllib.parse import quote
    b64 = quote(base64.b64encode(name).decode(), safe="")
    r = requests.get(f"{ALGOD_URL}/v2/applications/{app_id}/box?name=b64:{b64}", timeout=10)
    if r.status_code == 200:
        return base64.b64decode(r.json()["value"])
    return None

def main():
    seq = int(sys.argv[1]) if len(sys.argv) > 1 else 4

    # ── Fetch VAA ─────────────────────────────────────────────────────────────
    url = f"https://api.wormholescan.io/api/v1/vaas/{CHAIN}/{EMITTER}/{seq}"
    r = requests.get(url, timeout=15)
    if r.status_code != 200:
        print(f"HTTP {r.status_code}: {r.text[:200]}"); sys.exit(1)
    vaa_b64   = r.json()["data"]["vaa"]
    vaa_bytes = base64.b64decode(vaa_b64)
    parsed    = parse_vaa(vaa_bytes)

    print(f"\n═══ VAA seq={seq} ═══")
    print(f"  emitter_chain : {parsed['emitter_chain']}")
    print(f"  vaa_digest    : {parsed['vaa_digest'].hex()}")

    p = parsed["payload"]
    print(f"\n── Outer payload ({len(p)}b) ──")
    hex_block("full", p[:80])

    if p[0:4] != bytes.fromhex("9945ff10"):
        print("  NOT an NTT VAA (wrong prefix)"); sys.exit(1)

    src_ntt = p[4:36]
    hex_block("[4:36] src_ntt (source NTT mgr 32b)", src_ntt)
    hex_block("[36:68] unknown", p[36:68])
    mplen = int.from_bytes(p[68:70], "big")
    print(f"  [68:70] mplen = {mplen}")

    mp = p[70:70 + mplen]
    print(f"\n── Message Payload mp ({len(mp)}b) ──")
    hex_block("full mp", mp)

    # Assumed layout per ntt_execute.py
    print(f"\n── Assumed mp layout ──")
    hex_block("mp[0:32]   msg_id",           mp[0:32])
    hex_block("mp[32:64]  user_univ",         mp[32:64])
    hex_block("mp[64:96]  source_ntt_univ",   mp[64:96])
    hex_block("mp[96:128] handler_univ",      mp[96:128])
    hex_block("mp[128:]   inner_payload",     mp[128:])

    # ── Inner payload decode ───────────────────────────────────────────────────
    inner = mp[128:]
    print(f"\n── Inner payload decode ──")
    if len(inner) >= 4:
        hex_block("inner[0:4] prefix",  inner[0:4])
    if len(inner) >= 6:
        print(f"  inner[4:5] from_decimals: {inner[4]}")
    if len(inner) >= 14:
        from_amount = int.from_bytes(inner[5:13], "big")
        print(f"  inner[5:13] from_amount (raw): {from_amount}")
    if len(inner) >= 46:
        hex_block("inner[13:45] source_token", inner[13:45])
    if len(inner) >= 78:
        hex_block("inner[45:77] recipient",    inner[45:77])
    if len(inner) >= 80:
        recipient_chain = int.from_bytes(inner[77:79], "big")
        print(f"  inner[77:79] recipient_chain: {recipient_chain}")
        if recipient_chain == 8:
            print(f"  ✓ recipient_chain=8 (Algorand) — correct destination")
        else:
            print(f"  ✗ recipient_chain={recipient_chain} — NOT Algorand (8)!")

    # ── Compute digests ───────────────────────────────────────────────────────
    print(f"\n── Digests ──")
    ntt_digest = keccak256(
        mp[0:32] + mp[32:64]
        + parsed["emitter_chain"].to_bytes(2, "big")
        + src_ntt
        + b"\x00" * 24 + NTT_MGR.to_bytes(8, "big")
        + mp[64:]
    )
    print(f"  ntt_digest (TRANSCEIVER_MGR key): {ntt_digest.hex()}")

    msg_digest = keccak256(
        mp[0:32] + mp[32:64]
        + parsed["emitter_chain"].to_bytes(2, "big")
        + mp[64:96] + mp[96:128] + mp[128:]
    )
    print(f"  msg_digest (execute_message key): {msg_digest.hex()}")

    # ── Expected handler_univ for Algorand NTT_MGR ────────────────────────────
    expected_handler = b"\x00" * 24 + NTT_MGR.to_bytes(8, "big")
    print(f"\n── Handler address check ──")
    print(f"  expected (NTT_MGR bytes32): {expected_handler.hex()}")
    hex_block("  actual  (mp[96:128])",    mp[96:128])
    if mp[96:128] == expected_handler:
        print("  ✓ handler_univ matches NTT_MGR")
    else:
        print("  ✗ handler_univ MISMATCH — execute_message will assert-fail")

    # ── On-chain NTT_MGR peer box ─────────────────────────────────────────────
    print(f"\n── On-chain NTT_MGR peer for chain={parsed['emitter_chain']} ──")
    peer_box = fetch_box(NTT_MGR, b"ntt_manager_peer_" + parsed["emitter_chain"].to_bytes(2, "big"))
    if peer_box:
        hex_block("  peer_box raw", peer_box)
        # Typical layout: peer_contract(32b) + decimals(1b)
        if len(peer_box) >= 33:
            hex_block("  peer_contract", peer_box[:32])
            print(f"  decimals: {peer_box[32]}")
            if peer_box[:32] == src_ntt:
                print("  ✓ src_ntt matches registered peer_contract")
            else:
                print("  ✗ src_ntt MISMATCH vs registered peer_contract")
    else:
        print("  NOT FOUND — peer not registered for this chain")

    # ── TRANSCEIVER_MGR attestation boxes ─────────────────────────────────────
    print(f"\n── TRANSCEIVER_MGR attestation boxes ──")
    num_att = fetch_box(TRANSCEIVER_MGR, b"num_attestations_" + ntt_digest)
    if num_att:
        print(f"  num_attestations: {int.from_bytes(num_att, 'big')}")
    else:
        print("  num_attestations: NOT FOUND (attest step not done yet)")

    att_box = fetch_box(TRANSCEIVER_MGR, b"attestations_" + ntt_digest + WT_APP.to_bytes(8, "big"))
    print(f"  attestations_<digest><WT_APP>: {'EXISTS' if att_box is not None else 'NOT FOUND'}")

    # ── NTT_MGR messages_executed box ─────────────────────────────────────────
    print(f"\n── NTT_MGR execution state ──")
    exec_box = fetch_box(NTT_MGR, b"messages_executed_" + msg_digest)
    print(f"  messages_executed_<msg_digest>: {'EXISTS (already executed)' if exec_box is not None else 'NOT FOUND (not yet executed)'}")

    rate_box = fetch_box(NTT_MGR, b"rate_limit_buckets_" + ntt_digest)
    print(f"  rate_limit_buckets_<ntt_digest>: {'EXISTS' if rate_box is not None else 'NOT FOUND'}")

    print()

if __name__ == "__main__":
    main()
