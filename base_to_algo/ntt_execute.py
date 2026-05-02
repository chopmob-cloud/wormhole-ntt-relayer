#!/usr/bin/env python3
"""
NTT Execute — Step 2 of Base → Algorand relay
Calls NTT_MGR.execute_message(MessageReceived) after attestation is recorded.

Usage:
  python3 ntt_execute.py <seq>            # relay for real
  python3 ntt_execute.py <seq> --dry-run  # parse + check only
  python3 ntt_execute.py <seq> --debug    # extra field dumps

Reads config from ~/wormhole-relayer/.env
"""
import os, sys, base64, argparse, requests
from pathlib import Path
from algosdk.v2client import algod
from algosdk import mnemonic as algo_mnemonic, account as algo_account, encoding
from algosdk.transaction import BoxReference
from algosdk.atomic_transaction_composer import AtomicTransactionComposer, AccountTransactionSigner
from algosdk.abi import Method
from Crypto.Hash import keccak as _keccak

# ── .env ──────────────────────────────────────────────────────────────────────
def _load_env(path):
    env = {}
    p = Path(path).expanduser()
    if not p.exists(): return env
    for line in p.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line: continue
        k, _, v = line.partition("=")
        env[k.strip()] = v.strip().strip('"').strip("'")
    return env

_ENV  = _load_env(os.environ.get("ENV_FILE", "~/wormhole-relayer/.env"))
def _cfg(k, d=""): return os.environ.get(k, _ENV.get(k, d))
def _int(k, d):    v = _cfg(k); return int(v) if v else d

ALGOD_URL       = _cfg("ALGOD_URL",   "https://mainnet-api.4160.nodely.dev")
ALGOD_TOKEN     = _cfg("ALGOD_TOKEN", "")
WT_APP          = _int("WT_APP_ID",              0)
NTT_MGR         = _int("NTT_MGR_APP_ID",         0)
TRANSCEIVER_MGR = _int("TRANSCEIVER_MGR_APP_ID", 0)
NTT_TOKEN_ASSET = _int("NTT_TOKEN_ASSET_ID",     0)
SIG_LEN         = 66

# ── Helpers ───────────────────────────────────────────────────────────────────
def keccak256(d):
    h = _keccak.new(digest_bits=256); h.update(d); return h.digest()

def fresh_sp(client, fee=6000):
    sp = client.suggested_params(); sp.flat_fee = True
    cur = client.status()["last-round"]
    sp.first = cur+1; sp.last = cur+1000; sp.fee = fee
    return sp

def fetch_box(app_id, name):
    from urllib.parse import quote
    b64 = quote(base64.b64encode(name).decode(), safe="")
    r = requests.get(f"{ALGOD_URL}/v2/applications/{app_id}/box?name=b64:{b64}", timeout=10)
    return base64.b64decode(r.json()["value"]) if r.status_code == 200 else None

# ── VAA parsing ───────────────────────────────────────────────────────────────
def parse_vaa(vaa_bytes):
    num_sigs   = vaa_bytes[5]
    body_start = 6 + num_sigs * SIG_LEN
    body       = vaa_bytes[body_start:]
    return {
        "emitter_chain": int.from_bytes(body[8:10],  "big"),
        "sequence":      int.from_bytes(body[42:50], "big"),
        "payload":       body[51:],
        "vaa_digest":    keccak256(keccak256(body)),
        "body":          body,
        "raw":           vaa_bytes,
    }

# ── NTT payload parsing ───────────────────────────────────────────────────────
def parse_ntt_fields(parsed):
    """
    ACTUAL payload layout (verified from on-chain tx data):

    Outer payload:
      [0:4]   = 9945ff10           NTT transceiver prefix
      [4:36]  = src_ntt            source NTT manager (32b universal address)
      [36:68] = dst_ntt            dest NTT manager (32b universal address = NTT_MGR padded)
      [68:70] = mplen              length of message payload
      [70..]  = mp                 message payload

    mp layout:
      [0:32]  = msg_id             MessageId (bytes32, usually sequence padded)
      [32:64] = user_univ          sender universal address
      [64:66] = inner_len          uint16 length of inner transfer payload
      [66..]  = inner_body         NTT transfer payload (994E5454 prefix + amount + recipient...)

    inner_body layout:
      [0:4]   = 994E5454           NTT transfer prefix
      [4]     = from_decimals
      [5:13]  = from_amount        uint64
      [13:45] = source_token       32b
      [45:77] = recipient          32b Algorand address pubkey
      [77:79] = recipient_chain    uint16 (8 = Algorand)

    MessageReceived for execute_message:
      msg_id         = mp[0:32]
      user_univ      = mp[32:64]
      source_chain   = emitter_chain
      source_address = p[4:36]   (src_ntt from OUTER payload)
      handler_address= p[36:68]  (dst_ntt from OUTER payload)
      payload        = mp[64:]   (inner incl. uint16 length prefix)
    """
    p = parsed["payload"]
    if p[0:4] != bytes.fromhex("9945ff10"):
        return None

    src_ntt = p[4:36]
    dst_ntt = p[36:68]
    mplen   = int.from_bytes(p[68:70], "big")
    mp      = p[70:70+mplen]

    if len(mp) < 66:
        return None

    msg_id    = mp[0:32]
    user_univ = mp[32:64]
    inner     = mp[64:]          # includes uint16 length prefix

    # Decode inner for recipient info
    inner_len  = int.from_bytes(mp[64:66], "big")
    if 66 + inner_len > len(mp):
        return None
    inner_body = mp[66:66+inner_len]

    from_amount     = int.from_bytes(inner_body[5:13],  "big") if len(inner_body) >= 13 else None
    recipient_raw   = inner_body[45:77] if len(inner_body) >= 77 else None
    recipient_chain = int.from_bytes(inner_body[77:79], "big") if len(inner_body) >= 79 else None

    recipient_addr = None
    if recipient_raw and len(recipient_raw) == 32:
        try:
            recipient_addr = encoding.encode_address(recipient_raw)
        except Exception:
            pass

    # ntt_digest — must match relay.py exactly (used for TRANSCEIVER_MGR boxes)
    ntt_digest = keccak256(
        mp[0:32] + mp[32:64]
        + parsed["emitter_chain"].to_bytes(2, "big")
        + src_ntt
        + b"\x00"*24 + NTT_MGR.to_bytes(8, "big")
        + mp[64:]
    )

    # msg_digest — used for NTT_MGR messages_executed box
    msg_digest = keccak256(
        msg_id + user_univ
        + parsed["emitter_chain"].to_bytes(2, "big")
        + src_ntt + dst_ntt + inner
    )

    return {
        "src_ntt":         src_ntt,
        "dst_ntt":         dst_ntt,
        "msg_id":          msg_id,
        "user_univ":       user_univ,
        "inner":           inner,
        "from_amount":     from_amount,
        "recipient_raw":   recipient_raw,
        "recipient_chain": recipient_chain,
        "recipient_addr":  recipient_addr,
        "ntt_digest":      ntt_digest,
        "msg_digest":      msg_digest,
    }

# ── Pre-flight checks ─────────────────────────────────────────────────────────
def preflight(parsed, fields, debug=False):
    ok = True

    # 1. handler (dst_ntt) must be this NTT_MGR
    expected = b"\x00"*24 + NTT_MGR.to_bytes(8, "big")
    if fields["dst_ntt"] != expected:
        print(f"  ✗ dst_ntt mismatch — VAA not for this NTT_MGR")
        print(f"    expected: {expected.hex()}")
        print(f"    got:      {fields['dst_ntt'].hex()}")
        ok = False
    else:
        print(f"  ✓ dst_ntt matches NTT_MGR")

    # 2. recipient chain must be Algorand (8)
    if fields["recipient_chain"] != 8:
        print(f"  ✗ recipient_chain={fields['recipient_chain']} (expected 8)")
        ok = False
    else:
        print(f"  ✓ recipient_chain=8 (Algorand)")

    # 3. src_ntt vs registered peer
    peer_box = fetch_box(NTT_MGR, b"ntt_manager_peer_" + parsed["emitter_chain"].to_bytes(2, "big"))
    if peer_box and len(peer_box) >= 32:
        if peer_box[:32] == fields["src_ntt"]:
            print(f"  ✓ src_ntt matches registered peer")
        else:
            print(f"  ✗ src_ntt vs peer mismatch")
            print(f"    registered: {peer_box[:32].hex()}")
            print(f"    VAA src:    {fields['src_ntt'].hex()}")
            ok = False
    else:
        print(f"  ✗ peer not registered for chain {parsed['emitter_chain']}")
        ok = False

    # 4. attestation recorded?
    num_att = fetch_box(TRANSCEIVER_MGR, b"num_attestations_" + fields["ntt_digest"])
    if num_att:
        print(f"  ✓ num_attestations={int.from_bytes(num_att,'big')}")
    else:
        print(f"  ✗ num_attestations NOT FOUND — run relay.py first")
        ok = False

    # 5. not already executed?
    if fetch_box(NTT_MGR, b"messages_executed_" + fields["msg_digest"]) is not None:
        print(f"  ✗ Already executed")
        ok = False
    else:
        print(f"  ✓ Not yet executed")

    if debug:
        print(f"\n── Debug ──")
        print(f"  src_ntt:        {fields['src_ntt'].hex()}")
        print(f"  dst_ntt:        {fields['dst_ntt'].hex()}")
        print(f"  msg_id:         {fields['msg_id'].hex()}")
        print(f"  user_univ:      {fields['user_univ'].hex()}")
        print(f"  ntt_digest:     {fields['ntt_digest'].hex()}")
        print(f"  msg_digest:     {fields['msg_digest'].hex()}")
        print(f"  from_amount:    {fields['from_amount']}")
        print(f"  recipient_addr: {fields['recipient_addr']}")
        print(f"  inner ({len(fields['inner'])}b): {fields['inner'].hex()}")

    return ok

# ── Build ATC ─────────────────────────────────────────────────────────────────
def build_execute_atc(client, sender_addr, sender_key, parsed, fields):
    sp     = fresh_sp(client, fee=6000)
    signer = AccountTransactionSigner(sender_key)
    atc    = AtomicTransactionComposer()

    # MessageReceived = (byte[32], byte[32], uint16, byte[32], byte[32], byte[])
    method = Method.from_signature(
        "execute_message((byte[32],byte[32],uint16,byte[32],byte[32],byte[]))void"
    )

    message_received = [
        fields["msg_id"],           # id
        fields["user_univ"],        # user_address
        parsed["emitter_chain"],    # source_chain_id
        fields["src_ntt"],          # source_address (outer p[4:36])
        fields["dst_ntt"],          # handler_address (outer p[36:68])
        fields["inner"],            # payload (mp[64:] incl. length prefix)
    ]

    # app_index 0 = NTT_MGR, app_index 1 = TRANSCEIVER_MGR
    boxes = [
        BoxReference(app_index=0, name=b"messages_executed_"    + fields["msg_digest"]),
        BoxReference(app_index=0, name=b"ntt_manager_peer_"     + parsed["emitter_chain"].to_bytes(2,"big")),
        BoxReference(app_index=1, name=b"num_attestations_"     + fields["ntt_digest"]),
        BoxReference(app_index=1, name=b"attestations_"         + fields["ntt_digest"] + WT_APP.to_bytes(8,"big")),
        BoxReference(app_index=1, name=b"handler_transceivers_" + NTT_MGR.to_bytes(8,"big")),
        BoxReference(app_index=1, name=b"handler_paused_" + NTT_MGR.to_bytes(8, "big")),
    ]

    accounts = [fields["recipient_addr"]] if fields["recipient_addr"] else []

    atc.add_method_call(
        app_id=NTT_MGR,
        method=method,
        sender=sender_addr,
        sp=sp,
        signer=signer,
        method_args=[message_received],
        foreign_apps=[TRANSCEIVER_MGR],
        foreign_assets=[NTT_TOKEN_ASSET],
        boxes=boxes,
        accounts=accounts,
        note=os.urandom(8),
    )
    return atc

# ── Main ──────────────────────────────────────────────────────────────────────
def execute_ntt(vaa_b64, sender_mnemonic, dry_run=False, debug=False):
    client      = algod.AlgodClient(ALGOD_TOKEN, ALGOD_URL)
    sender_key  = algo_mnemonic.to_private_key(sender_mnemonic)
    sender_addr = algo_account.address_from_private_key(sender_key)
    print(f"Sender: {sender_addr}")

    vaa_bytes = base64.b64decode(vaa_b64)
    parsed    = parse_vaa(vaa_bytes)
    print(f"\n── VAA ──")
    print(f"  chain={parsed['emitter_chain']} seq={parsed['sequence']}")
    print(f"  vaa_digest={parsed['vaa_digest'].hex()}")

    fields = parse_ntt_fields(parsed)
    if not fields:
        print("  Not an NTT VAA — aborting"); return

    print(f"  ntt_digest:  {fields['ntt_digest'].hex()}")
    print(f"  msg_digest:  {fields['msg_digest'].hex()}")
    print(f"  recipient:   {fields['recipient_addr']}")
    print(f"  amount:      {fields['from_amount']}")

    print(f"\n── Pre-flight ──")
    if not preflight(parsed, fields, debug=debug):
        print("\n  Pre-flight failed — aborting"); return

    if dry_run:
        print("\n  --dry-run: not sending"); return

    print(f"\n── Send execute_message ──")
    try:
        atc    = build_execute_atc(client, sender_addr, sender_key, parsed, fields)
        result = atc.execute(client, wait_rounds=15)
        print(f"  Confirmed: {result.tx_ids}")
        print(f"\n  ✓ execute_message complete — tokens should now be minted!")
    except Exception as e:
        print(f"  Send failed: {e}")

if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("sequence", type=int)
    parser.add_argument("--emitter", default=os.environ.get("BASE_EMITTER", ""),
                        help="Source chain emitter (32-byte hex). Set BASE_EMITTER in .env")
    parser.add_argument("--chain",   type=int, default=30)
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--debug",   action="store_true")
    args = parser.parse_args()

    url = f"https://api.wormholescan.io/api/v1/vaas/{args.chain}/{args.emitter}/{args.sequence}"
    r   = requests.get(url, timeout=15)
    if r.status_code != 200:
        print(f"HTTP {r.status_code}: {r.text[:200]}"); sys.exit(1)
    vaa_b64 = r.json()["data"]["vaa"]
    print(f"VAA: {len(base64.b64decode(vaa_b64))} bytes")

    mn = _cfg("ALGO_MNEMONIC")
    if not mn:
        print("Set ALGO_MNEMONIC in .env"); sys.exit(1)

    execute_ntt(vaa_b64, mn, dry_run=args.dry_run, debug=args.debug)

