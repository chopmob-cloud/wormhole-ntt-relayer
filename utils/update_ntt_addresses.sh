#!/bin/bash
set -e

# Set these before running:
OLD_NTT="${OLD_NTT:-}"  # old Base NTT Manager address (0x-prefixed)
OLD_WT="${OLD_WT:-}"    # old Base WormholeTransceiver address (0x-prefixed)
NEW_NTT="${NEW_NTT:-}"  # new Base NTT Manager address (0x-prefixed)
NEW_WT="${NEW_WT:-}"    # new Base WormholeTransceiver address (0x-prefixed)

if [ -z "$OLD_NTT" ] || [ -z "$OLD_WT" ] || [ -z "$NEW_NTT" ] || [ -z "$NEW_WT" ]; then
  echo "Error: set OLD_NTT, OLD_WT, NEW_NTT, NEW_WT before running"
  exit 1
fi

OLD_NTT_LC="${OLD_NTT,,}"
OLD_WT_LC="${OLD_WT,,}"
NEW_NTT_LC="${NEW_NTT,,}"
NEW_WT_LC="${NEW_WT,,}"

RELAYER_DIR="$HOME/wormhole-relayer"

echo "============================================"
echo " NTT Bridge Address Update"
echo "============================================"
echo "Old NTT Manager: $OLD_NTT"
echo "New NTT Manager: $NEW_NTT"
echo "Old WT:          $OLD_WT"
echo "New WT:          $NEW_WT"
echo ""

# ── 1. Update Python relayer files ────────────────────────────────────────────
echo "[1/4] Updating relayer Python files..."

RELAYER_FILES=(
    "$RELAYER_DIR/base_to_algo_relayer.py"
    "$RELAYER_DIR/cast_relayer.py"
    "$RELAYER_DIR/relay.py"
    "$RELAYER_DIR/relay_seq_2.py"
    "$RELAYER_DIR/cast_relayer.py"
)

for f in "${RELAYER_FILES[@]}"; do
    if [ -f "$f" ]; then
        # Case-insensitive replacement of both checksummed and lowercase variants
        sed -i "s|$OLD_NTT|$NEW_NTT|gI" "$f"
        sed -i "s|$OLD_WT|$NEW_WT|gI" "$f"
        sed -i "s|${OLD_NTT:2}|${NEW_NTT:2}|gI" "$f"
        sed -i "s|${OLD_WT:2}|${NEW_WT:2}|gI" "$f"
        echo "  Updated: $f"
    else
        echo "  Skipped (not found): $f"
    fi
done

# ── 2. Update .env files ───────────────────────────────────────────────────────
echo ""
echo "[2/4] Updating .env files..."

ENV_FILES=(
    "$RELAYER_DIR/.env"
    "$HOME/.env"
)

for f in "${ENV_FILES[@]}"; do
    if [ -f "$f" ]; then
        sed -i "s|$OLD_NTT|$NEW_NTT|gI" "$f"
        sed -i "s|$OLD_WT|$NEW_WT|gI" "$f"
        sed -i "s|b4254f5515c87dbb14d816324462b71244b3356c|${NEW_NTT:2}|gI" "$f"
        sed -i "s|5865af9692d8e763da3ca42677770698e54d2e26|${NEW_WT:2}|gI" "$f"
        echo "  Updated: $f"
    fi
done

# ── 3. Scan for any remaining references ──────────────────────────────────────
echo ""
echo "[3/4] Scanning for remaining old address references..."

OLD_NTT_SHORT="${OLD_NTT:2:6}"
OLD_WT_SHORT="${OLD_WT:2:6}"
REMAINING=$(grep -rl "$OLD_NTT_SHORT\|$OLD_WT_SHORT" "$RELAYER_DIR"/ 2>/dev/null \
    | grep -v ".pyc\|__pycache__\|broadcast\|cache\|\.git" || true)

if [ -n "$REMAINING" ]; then
    echo "  WARNING: Old addresses still found in:"
    echo "$REMAINING" | sed 's/^/    /'
else
    echo "  OK: No remaining references to old addresses"
fi

# ── 4. Verify new addresses present ──────────────────────────────────────────
echo ""
echo "[4/4] Verifying new addresses in relayer files..."

NEW_NTT_SHORT="${NEW_NTT:2:6}"
NEW_WT_SHORT="${NEW_WT:2:6}"
for f in "$RELAYER_DIR/base_to_algo_relayer.py" "$RELAYER_DIR/cast_relayer.py"; do
    if [ -f "$f" ]; then
        NTT_COUNT=$(grep -ic "$NEW_NTT_SHORT" "$f" 2>/dev/null || echo 0)
        WT_COUNT=$(grep -ic "$NEW_WT_SHORT" "$f" 2>/dev/null || echo 0)
        echo "  $(basename $f): new_ntt=$NTT_COUNT refs, new_wt=$WT_COUNT refs"
    fi
done

echo ""
echo "============================================"
echo " Summary"
echo "============================================"
echo "New NTT Manager: $NEW_NTT"
echo "  https://basescan.org/address/$NEW_NTT"
echo "New WormholeTransceiver: $NEW_WT"
echo "  https://basescan.org/address/$NEW_WT"
echo ""
echo "Next steps:"
echo "  1. Update frontend .env with new contract addresses"
echo "  2. Restart base_to_algo_relayer service"
echo "  3. Restart cast_relayer (algo_to_base) service"
echo "  4. Test with small transfer in both directions"
echo ""
echo "IMPORTANT: Re-approve token spending on new NTT Manager if required:"
echo "  cast send <TOKEN_CONTRACT> \\"
echo "    'approve(address,uint256)' \\"
echo "    $NEW_NTT \\"
echo "    115792089237316195423570985008687907853269984665640564039457584007913129639935 \\"
echo "    --rpc-url \$RPC_URL --private-key \$PRIVATE_KEY --legacy"
