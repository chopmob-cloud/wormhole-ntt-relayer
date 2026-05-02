#!/usr/bin/env python3
import os
import sqlite3
import time
import base64
import subprocess
import requests
import json
from dotenv import load_dotenv

load_dotenv()

# --------------------------------------------------
# CONFIG
# --------------------------------------------------

CAST_BIN = os.getenv("CAST_BIN", "/home/ubuntu/.foundry/bin/cast")

RPCURL = os.getenv("RPCURL", "https://mainnet.base.org")
PRIVATEKEY = os.getenv("PRIVATEKEY")  # 64 hex chars, NO 0x

CHAIN_ID = "8"  # Algorand Wormhole chain ID
EMITTER_ADDRESS = os.getenv("ALGO_EMITTER_ADDRESS", "")  # Algorand NTT emitter (hex, no 0x)
TRANSCEIVER = os.getenv("BASE_TRANSCEIVER", "")          # Base WormholeTransceiver address

POLL_INTERVAL = 20
DB_PATH = ".relayer.db"

# --------------------------------------------------
# DATABASE
# --------------------------------------------------

def init_db():
    print("init_db: cwd is", os.getcwd(), flush=True)
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    cur.execute("""
        CREATE TABLE IF NOT EXISTS relayed (
            sequence TEXT PRIMARY KEY,
            txHash TEXT,
            relayedAt INTEGER
        )
    """)
    conn.commit()
    return conn

def has_relayed(conn, sequence: str) -> bool:
    cur = conn.cursor()
    cur.execute("SELECT sequence FROM relayed WHERE sequence = ?", (sequence,))
    return cur.fetchone() is not None

def mark_relayed(conn, sequence: str, tx_hash: str):
    cur = conn.cursor()
    cur.execute(
        "INSERT OR REPLACE INTO relayed (sequence, txHash, relayedAt) VALUES (?, ?, ?)",
        (sequence, tx_hash, int(time.time())),
    )
    conn.commit()

# --------------------------------------------------
# WORMHOLESCAN
# --------------------------------------------------

def fetch_recent_vaas():
    """
    Fetch recent VAAs directly from WormholeScan.
    This works reliably for Algorand.
    """
    url = f"https://api.wormholescan.io/api/v1/vaas/{CHAIN_ID}/{EMITTER_ADDRESS}"
    r = requests.get(url, timeout=15)
    r.raise_for_status()
    data = r.json()
    return data.get("data", [])

# --------------------------------------------------
# CAST CALL
# --------------------------------------------------

def cast_redeem(vaa_hex: str) -> str:
    cmd = [
        CAST_BIN,
        "send",
        TRANSCEIVER,
        "receiveMessage(bytes)",
        vaa_hex,
        "--rpc-url",
        RPCURL,
        "--gas-limit",
        "600000",
        "--json",
    ]

    print("Running cast send...", flush=True)

    # Pass key via environment variable — keeps it out of process list (ps aux)
    env = os.environ.copy()
    env["ETH_PRIVATE_KEY"] = "0x" + PRIVATEKEY
    result = subprocess.run(cmd, capture_output=True, text=True, env=env)

    if result.returncode != 0:
        print("cast stdout:", result.stdout, flush=True)
        print("cast stderr:", result.stderr, flush=True)
        raise RuntimeError("cast send failed")

    try:
        data = json.loads(result.stdout)
        tx_hash = data.get("transactionHash") or data.get("hash")
        if not tx_hash:
            raise ValueError("No transactionHash in cast output")
        return tx_hash
    except Exception as e:
        print("Failed to parse cast JSON:", e, flush=True)
        print("Raw output:", result.stdout, flush=True)
        raise

# --------------------------------------------------
# RELAY LOOP
# --------------------------------------------------

def relay_once(conn):
    print("Polling WormholeScan for VAAs...", flush=True)

    try:
        vaas = fetch_recent_vaas()
    except Exception as e:
        print("Failed fetching VAAs:", e, flush=True)
        return

    if not vaas:
        print("No VAAs found.", flush=True)
        return

    for entry in vaas:
        seq = str(entry.get("sequence"))
        if not seq:
            continue

        if has_relayed(conn, seq):
            continue

        vaa_b64 = entry.get("vaa")
        if not vaa_b64:
            continue

        try:
            vaa_hex = "0x" + base64.b64decode(vaa_b64).hex()
        except Exception as e:
            print(f"Sequence {seq}: base64 decode failed -> {e}", flush=True)
            continue

        # VM version check (must be 1)
        try:
            vm_version = int(vaa_hex[2:4], 16)
        except Exception:
            print(f"Sequence {seq}: invalid VAA format", flush=True)
            continue

        if vm_version != 1:
            print(f"Sequence {seq}: VM version {vm_version} not supported, skipping", flush=True)
            continue

        print(f"Sequence {seq}: attempting relay...", flush=True)

        try:
            tx_hash = cast_redeem(vaa_hex)
            print(f"Sequence {seq}: relayed tx {tx_hash}", flush=True)
            mark_relayed(conn, seq, tx_hash)
        except Exception as e:
            print(f"Sequence {seq}: relay failed -> {e}", flush=True)

# --------------------------------------------------
# MAIN
# --------------------------------------------------

def main():
    if not PRIVATEKEY or len(PRIVATEKEY) != 64:
        raise RuntimeError("PRIVATEKEY must be 64 hex chars (no 0x) in .env")
    if not EMITTER_ADDRESS:
        raise RuntimeError("ALGO_EMITTER_ADDRESS not set in .env")
    if not TRANSCEIVER:
        raise RuntimeError("BASE_TRANSCEIVER not set in .env")

    conn = init_db()
    print("Starting Algorand → Base NTT relayer...", flush=True)

    while True:
        try:
            relay_once(conn)
        except Exception as e:
            print("Relay loop error:", e, flush=True)

        time.sleep(POLL_INTERVAL)

if __name__ == "__main__":
    main()
