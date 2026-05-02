#!/usr/bin/env python3
"""
Base -> Algorand NTT Relay
- No simulate (verifySigs txns are deterministic, always collide in sim)
- Skips payment if vaa_verify lsig already funded
- ATC + ABI receive_message(appl)void
- Step 1 of 2: records attestation in TRANSCEIVER_MGR
- Step 2: run ntt_execute.py <seq> to mint tokens
"""
import base64, hashlib, os, sys, requests, argparse, random
from pathlib import Path
from algosdk.v2client import algod
from algosdk import transaction, encoding, logic, mnemonic as algo_mnemonic, account as algo_account
from algosdk.transaction import (
    ApplicationCallTxn, PaymentTxn, LogicSigTransaction, LogicSigAccount,
    assign_group_id, OnComplete, BoxReference
)
from algosdk.atomic_transaction_composer import (
    AtomicTransactionComposer, TransactionWithSigner, AccountTransactionSigner
)
from algosdk.abi import Method
from Crypto.Hash import keccak as _keccak

# ── .env loading ──────────────────────────────────────────────────────────────
def _load_env(path: str) -> dict:
    env = {}
    p = Path(path).expanduser()
    if not p.exists():
        return env
    for line in p.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, _, v = line.partition("=")
        env[k.strip()] = v.strip().strip('"').strip("'")
    return env

_ENV_FILE = os.environ.get("ENV_FILE", "~/wormhole-relayer/.env")
_ENV      = _load_env(_ENV_FILE)

def _cfg(key: str, default: str = "") -> str:
    return os.environ.get(key, _ENV.get(key, default))

def _cfg_int(key: str, default: int) -> int:
    v = _cfg(key)
    return int(v) if v else default

# ── Constants ─────────────────────────────────────────────────────────────────
WORMHOLE_CORE   = _cfg_int("WORMHOLE_CORE_APP_ID",   842125965)  # public Wormhole constant
WT_APP          = _cfg_int("WT_APP_ID",               0)
NTT_MGR         = _cfg_int("NTT_MGR_APP_ID",          0)
TRANSCEIVER_MGR = _cfg_int("TRANSCEIVER_MGR_APP_ID",  0)
NTT_TOKEN_ASSET = _cfg_int("NTT_TOKEN_ASSET_ID",      0)
ALGOD_URL       = _cfg("ALGOD_URL",   "https://mainnet-api.4160.nodely.dev")
ALGOD_TOKEN     = _cfg("ALGOD_TOKEN", "")

TMPL_BYTECODE = base64.b64decode(
    "BiABAYEASIAASDEQgQYSRDEZIhJEMRiBABJEMSCAABJEMQGBABJEMQkyAxJEMRUyAxJEIg=="
)
TMPL_LABELS = {
    "TMPL_ADDR_IDX":    {"position": 5,  "bytes": False},
    "TMPL_EMITTER_ID":  {"position": 8,  "bytes": True},
    "TMPL_APP_ID":      {"position": 24, "bytes": False},
    "TMPL_APP_ADDRESS": {"position": 30, "bytes": True},
}
MAX_BYTES_PER_KEY = 127
MAX_KEYS          = 15
MAX_BITS          = MAX_BYTES_PER_KEY * 8 * MAX_KEYS
SIGS_PER_TXN      = 9
SIG_LEN           = 66

def keccak256(data):
    h = _keccak.new(digest_bits=256); h.update(data); return h.digest()

def encode_uvarint(val):
    r = b""
    while val >= 128:
        r += bytes([(val & 0xFF) | 128]); val >>= 7
    return r + bytes([val & 0xFF])

def app_addr(app_id):       return logic.get_application_address(app_id)
def app_addr_bytes(app_id): return encoding.decode_address(app_addr(app_id))

def fresh_sp(client):
    sp = client.suggested_params()
    sp.flat_fee = True
    cur = client.status()["last-round"]
    sp.first = cur + 1
    sp.last  = cur + 1000
    return sp

def populate_tmplsig(addr_idx, emitter_id, app_id, app_addr_b):
    contract = list(TMPL_BYTECODE)
    values = {
        "TMPL_ADDR_IDX":    addr_idx,
        "TMPL_EMITTER_ID":  emitter_id.hex(),
        "TMPL_APP_ID":      app_id,
        "TMPL_APP_ADDRESS": app_addr_b.hex(),
    }
    shift = 0
    for k, v in sorted(TMPL_LABELS.items(), key=lambda x: x[1]["position"]):
        pos = v["position"] + shift
        if v["bytes"]:
            val = bytes.fromhex(values[k])
            lb  = encode_uvarint(len(val))
            shift += (len(lb) - 1) + len(val)
            contract[pos:pos+1] = list(lb + val)
        else:
            val = encode_uvarint(values[k])
            shift += len(val) - 1
            contract[pos:pos+1] = list(val)
    return LogicSigAccount(bytes(contract))

def get_tmplsig(addr_idx, emitter_id):
    lsa = populate_tmplsig(addr_idx, emitter_id, WORMHOLE_CORE, app_addr_bytes(WORMHOLE_CORE))
    return lsa.address(), lsa

def parse_vaa(vaa_bytes):
    if len(vaa_bytes) < 6:
        raise ValueError(f"VAA too short: {len(vaa_bytes)} bytes")
    num_sigs   = vaa_bytes[5]
    body_start = 6 + num_sigs * SIG_LEN
    if len(vaa_bytes) < body_start + 51:
        raise ValueError(f"VAA truncated: {len(vaa_bytes)} bytes, need {body_start + 51}")
    body       = vaa_bytes[body_start:]
    return {
        "guardian_set_index": int.from_bytes(vaa_bytes[1:5], "big"),
        "num_sigs":           num_sigs,
        "signatures":         vaa_bytes[6:body_start],
        "body_start":         body_start,
        "body":               body,
        "emitter_chain":      int.from_bytes(body[8:10],  "big"),
        "emitter_address":    body[10:42],
        "sequence":           int.from_bytes(body[42:50], "big"),
        "payload":            body[51:],
        "vaa_digest":         keccak256(keccak256(body)),
        "ntt_prefix":         body[51:55],
        "raw":                vaa_bytes,
    }

def get_guardian_keys(client, guardian_addr):
    ai, vals, empty = client.account_info(guardian_addr), {}, bytes(127)
    for app in ai.get("apps-local-state", []):
        if app["id"] != WORMHOLE_CORE: continue
        for kv in app.get("key-value", []):
            k = base64.b64decode(kv["key"])
            if k == b"meta": continue
            try:
                v = base64.b64decode(kv["value"]["bytes"])
                if v != empty: vals[int.from_bytes(k, "big")] = v
            except: pass
    return b"".join(vals[k] for k in sorted(vals))

def load_vaa_verify(client):
    saved = "/tmp/vaa_verify_program.b64"
    if os.path.exists(saved):
        program = base64.b64decode(open(saved).read().strip())
        # FIX: compute addr even when loading from cache
        ch   = hashlib.new("sha512_256", b"Program" + program).digest()
        addr = encoding.encode_address(ch)
    else:
        teal = "/tmp/wormhole/algorand/teal/vaa_verify.teal"
        if not os.path.exists(teal):
            raise FileNotFoundError(f"Missing {teal}")
        res     = client.compile(open(teal).read())
        program = base64.b64decode(res["result"])
        open(saved, "w").write(res["result"])
        ch   = hashlib.new("sha512_256", b"Program" + program).digest()
        addr = encoding.encode_address(ch)
        for item in client.application_info(WORMHOLE_CORE)["params"].get("global-state", []):
            if base64.b64decode(item["key"]) == b"vphash":
                if ch != base64.b64decode(item["value"]["bytes"]):
                    raise ValueError("vphash mismatch")
                print("  vaa_verify hash OK")
                break
    return program, addr

def ensure_optin(client, sender_addr, sender_key, app_id, addr_idx, emitter_id):
    addr, lsa = get_tmplsig(addr_idx, emitter_id)
    try:
        info = client.account_application_info(addr, app_id)
        if info.get("app-local-state"):
            print(f"  Opted in: {addr[:16]}..."); return addr
    except: pass
    sp = fresh_sp(client)
    try:    bal = client.account_info(addr).get("amount", 0)
    except: bal = 0
    txns = []
    if bal < 1002000:
        fund = PaymentTxn(sender=sender_addr, sp=sp, receiver=addr, amt=1002000 - bal)
        fund.fee = 2000; txns.append(fund)
    optin = ApplicationCallTxn(
        sender=addr, sp=sp, index=app_id,
        on_complete=OnComplete.OptInOC, rekey_to=app_addr(app_id))
    optin.fee = 0; txns.append(optin)
    assign_group_id(txns)
    signed = [LogicSigTransaction(t, lsa) if t.sender == addr else t.sign(sender_key) for t in txns]
    txid = client.send_transactions(signed)
    transaction.wait_for_confirmation(client, txid, 10)
    print(f"  Opted in: {addr[:16]}..."); return addr

def parse_ntt(parsed):
    """
    Extract ntt_digest and recipient from NTT VAA payload.

    Outer payload layout:
      [0:4]   = 9945ff10  (NTT transceiver prefix)
      [4:36]  = src_ntt   (source NTT manager, 32b)
      [36:68] = dst_ntt   (dest NTT manager, 32b)
      [68:70] = mplen
      [70..]  = mp

    mp layout:
      [0:32]  = msg_id
      [32:64] = user_univ
      [64:66] = inner_len
      [66..]  = inner_body (994E5454 prefix + decimals + amount + token + recipient + chain)

    recipient = inner_body[45:77] (32b Algorand pubkey)
    """
    p = parsed["payload"]
    if p[0:4] != bytes.fromhex("9945ff10"): return None, None
    src_ntt = p[4:36]
    mplen   = int.from_bytes(p[68:70], "big")
    mp      = p[70:70 + mplen]
    ntt_digest = keccak256(
        mp[0:32] + mp[32:64]
        + parsed["emitter_chain"].to_bytes(2, "big")
        + src_ntt
        + b"\x00" * 24 + NTT_MGR.to_bytes(8, "big")
        + mp[64:]
    )
    recipient = encoding.encode_address(mp[66 + 45 : 66 + 77])
    return ntt_digest, recipient

def is_consumed(digest):
    from urllib.parse import quote
    name = b"vaas_consumed_" + digest
    b64  = quote(base64.b64encode(name).decode(), safe="")
    try:
        r = requests.get(
            f"{ALGOD_URL}/v2/applications/{WT_APP}/box?name=b64:{b64}",
            timeout=10)
        return r.status_code == 200
    except:
        return False

def build_pre_txns(client, sender_addr, vaa_bytes, parsed,
                   seq_addr, guardian_addr, guardian_keys,
                   vaa_verify_program, vaa_verify_addr,
                   ntt_digest=None, recipient=None):
    sp     = fresh_sp(client)
    accts  = [seq_addr, guardian_addr]
    digest = keccak256(keccak256(vaa_bytes[parsed["body_start"]:]))
    sigs   = parsed["signatures"]
    bsize  = SIGS_PER_TXN * SIG_LEN
    blocks = (len(sigs) + bsize - 1) // bsize
    txns   = []

    bal = client.account_info(vaa_verify_addr).get("amount", 0)
    if bal < 100000:
        amt = 300000 + random.randint(1, 9999)
        pmt = PaymentTxn(sender=sender_addr, sp=sp, receiver=vaa_verify_addr, amt=amt, note=os.urandom(8))
        pmt.fee = 1000; txns.append(pmt)
        print(f"  Funding vaa_verify: {amt} µA")
    else:
        print(f"  vaa_verify funded ({bal} µA), skipping payment")

    for i in range(blocks):
        start = i * bsize; end = min(start + bsize, len(sigs))
        chunk = sigs[start:end]
        keys  = b"".join(
            guardian_keys[1 + sigs[start + j * SIG_LEN] * 20 :
                          1 + (sigs[start + j * SIG_LEN] + 1) * 20]
            for j in range((end - start) // SIG_LEN)
        )
        t = ApplicationCallTxn(
            sender=vaa_verify_addr, sp=sp, index=WORMHOLE_CORE,
            on_complete=OnComplete.NoOpOC,
            app_args=[b"verifySigs", chunk, keys, digest], accounts=accts, note=os.urandom(8))
        t.fee = 0; txns.append(t)

    ntt_mgr_app_addr = encoding.encode_address(
        hashlib.new("sha512_256", b"appID" + NTT_MGR.to_bytes(8, "big")).digest())

    # AVM v7+: resources declared on any txn in a group are accessible group-wide,
    # including to inner txns. Put NTT_MGR app + boxes on verifyVAA to free budget
    # on receive_message while still allowing TRANSCEIVER_MGR inner calls to NTT_MGR.
    vvt_accounts = accts + [ntt_mgr_app_addr]
    if recipient:
        vvt_accounts.append(recipient)
    vvt_boxes = []
    if ntt_digest:
        # app_index=1 → NTT_MGR (first in foreign_apps on this txn)
        vvt_boxes = [
            BoxReference(app_index=1, name=b"ntt_manager_peer_"   + parsed["emitter_chain"].to_bytes(2, "big")),
        ]
    vvt = ApplicationCallTxn(
        sender=sender_addr, sp=sp, index=WORMHOLE_CORE,
        on_complete=OnComplete.NoOpOC,
        app_args=[b"verifyVAA", vaa_bytes], note=os.urandom(8),
        accounts=vvt_accounts,
        foreign_apps=[NTT_MGR],
        boxes=vvt_boxes)
    vvt.fee = 1000 * (1 + blocks); txns.append(vvt)
    return txns, vvt, digest, blocks

def build_atc(client, sender_addr, sender_key,
              pre_txns, verify_vaa_txn, vaa_verify_program,
              receive_apps, receive_assets, receive_boxes, receive_accounts):
    vaa_lsig = LogicSigAccount(vaa_verify_program)
    acct_sig = AccountTransactionSigner(sender_key)

    class LSigSigner:
        def sign_transactions(self, tg, idx):
            return [LogicSigTransaction(tg[i], vaa_lsig) for i in idx]

    sp = fresh_sp(client); sp.fee = 20000
    atc = AtomicTransactionComposer()
    # pre_txns[-1] is verify_vaa_txn — skip it, it's added via method_args below
    for t in pre_txns[:-1]:
        sig = LSigSigner() if (hasattr(t, "app_args") and t.app_args
                               and t.app_args[0] == b"verifySigs") else acct_sig
        atc.add_transaction(TransactionWithSigner(t, sig))
    atc.add_method_call(
        app_id=WT_APP,
        method=Method.from_signature("receive_message(appl)void"),
        sender=sender_addr, sp=sp, signer=acct_sig,
        method_args=[TransactionWithSigner(verify_vaa_txn, acct_sig)],
        foreign_apps=receive_apps,
        foreign_assets=receive_assets,
        boxes=receive_boxes,
        accounts=receive_accounts,
        note=os.urandom(8),  # prevents deterministic txid collision on retry
    )
    return atc

def relay_vaa(vaa_b64, sender_mnemonic, dry_run=False):
    client      = algod.AlgodClient(ALGOD_TOKEN, ALGOD_URL)
    sender_key  = algo_mnemonic.to_private_key(sender_mnemonic)
    sender_addr = algo_account.address_from_private_key(sender_key)
    print(f"Sender: {sender_addr}")

    vaa_bytes = base64.b64decode(vaa_b64)
    parsed    = parse_vaa(vaa_bytes)
    print(f"\n── VAA ──")
    print(f"  chain={parsed['emitter_chain']} seq={parsed['sequence']}")
    print(f"  digest={parsed['vaa_digest'].hex()}")
    print(f"  prefix={parsed['ntt_prefix'].hex()}")

    if is_consumed(parsed["vaa_digest"]):
        print("  Already relayed — skipping"); return

    vaa_verify_program, vaa_verify_addr = load_vaa_verify(client)
    print(f"  vaa_verify: {vaa_verify_addr}")

    guardian_addr, _ = get_tmplsig(parsed["guardian_set_index"], b"guardian")
    seq_page    = parsed["sequence"] // MAX_BITS
    emitter_id  = parsed["emitter_chain"].to_bytes(2, "big") + parsed["emitter_address"]
    seq_addr, _ = get_tmplsig(seq_page, emitter_id)
    print(f"  guardian: {guardian_addr}")
    print(f"  seq:      {seq_addr}")

    try:
        info = client.account_application_info(guardian_addr, WORMHOLE_CORE)
        print(f"  guardian state: {len(info.get('app-local-state',{}).get('key-value',[]))} entries")
    except Exception as e:
        print(f"  guardian missing: {e}"); return

    seq_addr      = ensure_optin(client, sender_addr, sender_key,
                                 WORMHOLE_CORE, seq_page, emitter_id)
    guardian_keys = get_guardian_keys(client, guardian_addr)
    print(f"  keys: {guardian_keys[0] if guardian_keys else 0} guardians, {len(guardian_keys)}b")

    ntt_digest, recipient = parse_ntt(parsed)
    if ntt_digest:
        print(f"  ntt_digest: {ntt_digest.hex()}")
        print(f"  recipient:  {recipient}")

    # app_index mapping for receive_message boxes:
    #   0 = WT_APP (self)
    #   1 = TRANSCEIVER_MGR (first in receive_apps)
    # NTT_MGR is declared on verifyVAA txn (group-wide via AVM v7+)
    receive_boxes = [
        BoxReference(app_index=0, name=b"wormhole_peer_"        + parsed["emitter_chain"].to_bytes(2, "big")),
        BoxReference(app_index=0, name=b"vaas_consumed_"        + parsed["vaa_digest"]),
        BoxReference(app_index=1, name=b"handler_transceivers_" + NTT_MGR.to_bytes(8, "big")),
        BoxReference(app_index=1, name=b"handler_paused_" + NTT_MGR.to_bytes(8, "big")),
    ]
    if ntt_digest:
        receive_boxes.append(BoxReference(app_index=1, name=b"num_attestations_" + ntt_digest))
        receive_boxes.append(BoxReference(app_index=1,
            name=b"attestations_" + ntt_digest + WT_APP.to_bytes(8, "big")))

    def make_atc(pt, vvt):
        return build_atc(
            client, sender_addr, sender_key, pt, vvt, vaa_verify_program,
            receive_apps=[TRANSCEIVER_MGR],  # NTT_MGR on verifyVAA txn, accessible group-wide
            receive_assets=[NTT_TOKEN_ASSET],
            receive_boxes=receive_boxes,
            receive_accounts=[],
        )

    if dry_run:
        print("\n  dry-run: simulate skipped (verifySigs always collides in sim)")
        print("  Run without --dry-run to send.")
        return

    print(f"\n── Send ──")
    try:
        pt, vvt, _, _ = build_pre_txns(
            client, sender_addr, vaa_bytes, parsed,
            seq_addr, guardian_addr, guardian_keys,
            vaa_verify_program, vaa_verify_addr,
            ntt_digest=ntt_digest, recipient=recipient)
        print(f"  {len(pt)} pre-txns + receive_message")
        result = make_atc(pt, vvt).execute(client, wait_rounds=15)
        print(f"  Confirmed: {result.tx_ids}")
        print(f"\n  Base -> Algorand relay complete!")
        print(f"  Now run: python3 ntt_execute.py {parsed['sequence']}")
    except Exception as e:
        print(f"  Send failed: {e}")

if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("sequence", type=int)
    parser.add_argument("--emitter", default=os.environ.get("BASE_EMITTER", ""),
                        help="Source chain emitter (32-byte hex). Set BASE_EMITTER in .env")
    parser.add_argument("--chain",   type=int, default=30)
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    url = f"https://api.wormholescan.io/api/v1/vaas/{args.chain}/{args.emitter}/{args.sequence}"
    r   = requests.get(url, timeout=15)
    if r.status_code != 200:
        print(f"HTTP {r.status_code}: {r.text[:200]}"); sys.exit(1)
    vaa_b64 = r.json()["data"]["vaa"]
    print(f"VAA: {len(base64.b64decode(vaa_b64))} bytes")

    mn = _cfg("ALGO_MNEMONIC")
    if not mn:
        print(f"Set ALGO_MNEMONIC in environment or {_ENV_FILE}"); sys.exit(1)

    relay_vaa(vaa_b64, mn, dry_run=args.dry_run)

