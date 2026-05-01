#!/usr/bin/env python3
"""
Base -> Algorand NTT Relay Service
Polls Wormholescan for new VAAs and runs both steps:
  Step 1: base-relayer.py — attest (records attestation in TRANSCEIVER_MGR)
  Step 2: ntt_execute.py  — execute (mints tokens via NTT_MGR)

Uses SQLite to track processed sequences and prevent double-processing.
"""
import os, sys, time, json, signal, logging, sqlite3
from pathlib import Path

# ── Config ────────────────────────────────────────────────────────────────────
RELAY_DIR     = Path(__file__).parent
ENV_FILE      = os.environ.get("ENV_FILE",   str(RELAY_DIR / ".env"))
DB_FILE       = os.environ.get("DB_FILE",    str(RELAY_DIR / ".relay.db"))
POLL_INTERVAL = int(os.environ.get("POLL_INTERVAL", "30"))
EMITTER       = os.environ.get("BASE_EMITTER", "")  # Base WormholeTransceiver emitter (32-byte hex)
CHAIN         = int(os.environ.get("BASE_CHAIN", "30"))

# ── Logging ───────────────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%dT%H:%M:%S",
    handlers=[logging.StreamHandler(sys.stdout)],
)
log = logging.getLogger("base-relay")

# ── Graceful shutdown ─────────────────────────────────────────────────────────
_running = True
def _stop(sig, frame):
    global _running
    log.info(f"Signal {sig} — shutting down")
    _running = False
signal.signal(signal.SIGTERM, _stop)
signal.signal(signal.SIGINT,  _stop)

# ── SQLite DB ─────────────────────────────────────────────────────────────────
def get_db() -> sqlite3.Connection:
    db = sqlite3.connect(DB_FILE)
    db.row_factory = sqlite3.Row
    db.execute("""
        CREATE TABLE IF NOT EXISTS sequences (
            sequence    INTEGER PRIMARY KEY,
            status      TEXT NOT NULL DEFAULT 'pending',
            -- pending | attested | complete | failed
            vaa_digest  TEXT,
            ntt_digest  TEXT,
            recipient   TEXT,
            amount      INTEGER,
            attest_txid TEXT,
            execute_txid TEXT,
            attempts    INTEGER NOT NULL DEFAULT 0,
            last_error  TEXT,
            created_at  TEXT NOT NULL DEFAULT (datetime('now')),
            updated_at  TEXT NOT NULL DEFAULT (datetime('now'))
        )
    """)
    db.commit()
    return db

def db_get(seq: int) -> dict | None:
    with get_db() as db:
        row = db.execute("SELECT * FROM sequences WHERE sequence=?", (seq,)).fetchone()
        return dict(row) if row else None

def db_upsert(seq: int, **kwargs):
    kwargs["updated_at"] = "datetime('now')"
    with get_db() as db:
        existing = db.execute("SELECT sequence FROM sequences WHERE sequence=?", (seq,)).fetchone()
        if existing:
            sets = ", ".join(
                f"{k}=datetime('now')" if v == "datetime('now')" else f"{k}=?"
                for k, v in kwargs.items()
            )
            vals = [v for v in kwargs.values() if v != "datetime('now')"]
            db.execute(f"UPDATE sequences SET {sets} WHERE sequence=?", vals + [seq])
        else:
            kwargs.setdefault("status", "pending")
            cols = ["sequence"] + list(kwargs.keys())
            placeholders = ["?"] + [
                "datetime('now')" if v == "datetime('now')" else "?"
                for v in kwargs.values()
            ]
            vals = [seq] + [v for v in kwargs.values() if v != "datetime('now')"]
            db.execute(
                f"INSERT INTO sequences ({','.join(cols)}) VALUES ({','.join(placeholders)})",
                vals
            )
        db.commit()

def db_last_complete() -> int:
    with get_db() as db:
        row = db.execute(
            "SELECT COALESCE(MAX(sequence), -1) as s FROM sequences WHERE status='complete'"
        ).fetchone()
        return row["s"]

def db_increment_attempts(seq: int, error: str = None):
    with get_db() as db:
        db.execute("""
            UPDATE sequences
            SET attempts=attempts+1, last_error=?, updated_at=datetime('now')
            WHERE sequence=?
        """, (error, seq))
        db.commit()

# ── Wormholescan ──────────────────────────────────────────────────────────────
def fetch_new_vaas(after_seq: int) -> list:
    import requests
    url = f"https://api.wormholescan.io/api/v1/vaas/{CHAIN}/{EMITTER}"
    try:
        r = requests.get(url, params={"pageSize": 50}, timeout=15)
        r.raise_for_status()
        vaas = r.json().get("data", [])
        new  = [v for v in vaas if v["sequence"] > after_seq]
        return sorted(new, key=lambda v: v["sequence"])
    except Exception as e:
        log.warning(f"Wormholescan fetch failed: {e}")
        return []

def fetch_vaa_b64(seq: int):
    import requests
    url = f"https://api.wormholescan.io/api/v1/vaas/{CHAIN}/{EMITTER}/{seq}"
    try:
        r = requests.get(url, timeout=15)
        if r.status_code == 200:
            return r.json()["data"]["vaa"]
    except Exception as e:
        log.error(f"Seq {seq}: VAA fetch failed: {e}")
    return None

# ── Step 1: Attest ────────────────────────────────────────────────────────────
def step1_attest(seq: int, vaa_b64: str) -> bool:
    try:
        import importlib.util
        spec = importlib.util.spec_from_file_location("base_relayer", RELAY_DIR / "base-relayer.py")
        mod  = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        mn = mod._cfg("ALGO_MNEMONIC")
        if not mn:
            log.error("ALGO_MNEMONIC not set"); return False
        mod.relay_vaa(vaa_b64, mn, dry_run=False)
        return True
    except Exception as e:
        err = str(e)
        if "already in ledger" in err:
            log.info(f"Seq {seq}: already in ledger — attest succeeded previously")
            return True
        log.error(f"Seq {seq}: attest failed: {e}")
        db_increment_attempts(seq, str(e))
        return False

# ── Step 2: Execute ───────────────────────────────────────────────────────────
def step2_execute(seq: int, vaa_b64: str) -> bool:
    try:
        import importlib.util
        spec = importlib.util.spec_from_file_location("ntt_execute", RELAY_DIR / "ntt_execute.py")
        mod  = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        mn = mod._cfg("ALGO_MNEMONIC")
        if not mn:
            log.error("ALGO_MNEMONIC not set"); return False
        mod.execute_ntt(vaa_b64, mn, dry_run=False)
        return True
    except Exception as e:
        log.error(f"Seq {seq}: execute failed: {e}")
        db_increment_attempts(seq, str(e))
        return False

# ── Process one sequence ──────────────────────────────────────────────────────
def process_sequence(seq: int) -> bool:
    # Check DB — skip if already complete
    row = db_get(seq)
    if row and row["status"] == "complete":
        log.info(f"Seq {seq}: already complete — skipping")
        return True

    # Don't retry more than 5 times
    if row and row["attempts"] >= 5:
        log.warning(f"Seq {seq}: max attempts reached — marking failed")
        db_upsert(seq, status="failed")
        return True  # advance past it

    log.info(f"── Seq {seq}: fetching VAA ──")
    vaa_b64 = fetch_vaa_b64(seq)
    if not vaa_b64:
        log.error(f"Seq {seq}: VAA not available"); return False

    import base64
    vaa_bytes = base64.b64decode(vaa_b64)
    log.info(f"Seq {seq}: {len(vaa_bytes)} bytes")

    # Record in DB if new
    if not row:
        db_upsert(seq, status="pending")

    # Step 1: Attest (skip if already attested)
    current = db_get(seq)
    if current and current["status"] in ("attested", "complete"):
        log.info(f"Seq {seq}: Step 1 already done — skipping attest")
    else:
        log.info(f"Seq {seq}: Step 1 — attest")
        if not step1_attest(seq, vaa_b64):
            return False
        db_upsert(seq, status="attested")
        log.info(f"Seq {seq}: Step 1 ✓")
        time.sleep(3)  # let on-chain state settle

    # Step 2: Execute
    log.info(f"Seq {seq}: Step 2 — execute")
    if not step2_execute(seq, vaa_b64):
        return False
    db_upsert(seq, status="complete")
    log.info(f"Seq {seq}: Step 2 ✓ — tokens minted!")
    return True

# ── Main loop ─────────────────────────────────────────────────────────────────
def main():
    log.info("Base → Algorand NTT relay service starting")
    log.info(f"  Dir:           {RELAY_DIR}")
    log.info(f"  DB:            {DB_FILE}")
    log.info(f"  Poll interval: {POLL_INTERVAL}s")

    if not EMITTER:
        log.error("BASE_EMITTER not set — set it in .env or environment")
        sys.exit(1)

    # Init DB
    get_db()
    last = db_last_complete()
    log.info(f"  Last complete sequence: {last}")

    while _running:
        try:
            # Find highest complete seq to poll from
            last = db_last_complete()

            # Also check for any attested-but-not-executed sequences
            with get_db() as db:
                pending = db.execute("""
                    SELECT sequence FROM sequences
                    WHERE status IN ('pending','attested')
                    AND attempts < 5
                    ORDER BY sequence ASC
                """).fetchall()

            retry_seqs = [r["sequence"] for r in pending]
            if retry_seqs:
                log.info(f"Retrying incomplete sequences: {retry_seqs}")

            # Fetch new VAAs beyond last complete
            new_vaas = fetch_new_vaas(last)
            new_seqs = [v["sequence"] for v in new_vaas
                       if v["sequence"] not in retry_seqs]

            all_seqs = sorted(set(retry_seqs + new_seqs))

            if all_seqs:
                log.info(f"Processing sequences: {all_seqs}")

            for seq in all_seqs:
                if not _running:
                    break
                ok = process_sequence(seq)
                if not ok:
                    log.error(f"Seq {seq}: failed — will retry next poll")
                    break  # don't skip ahead on hard failure

        except Exception as e:
            log.error(f"Poll error: {e}", exc_info=True)

        for _ in range(POLL_INTERVAL):
            if not _running: break
            time.sleep(1)

    log.info("Service stopped")


if __name__ == "__main__":
    main()

