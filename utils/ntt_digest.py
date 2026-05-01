#!/usr/bin/env python3
"""
NTT Message Digest Computation for Base → Algorand Relay

Computes the message digest that the Algorand TransceiverManager uses
for the `num_attestations_<digest>` box key.

Derived from the Folks Finance PuyaPy Algorand NTT contract interface:

    def calculate_message_digest(self, message: MessageReceived) -> MessageDigest:
        return MessageDigest.from_bytes(
            op.keccak256(
                message.id.bytes              # 32 bytes — NttManagerMessage.id
                + message.user_address.bytes  # 32 bytes — NttManagerMessage.sender
                + message.source_chain_id.bytes  # 2 bytes — source Wormhole chain ID (UInt16 BE)
                + message.source_address.bytes   # 32 bytes — source NTT manager (from TransceiverMessage)
                + message.handler_address.bytes  # 32 bytes — NTT Manager App ID, zero-padded to 32
                + message.payload.bytes          # var bytes — len_prefix(2) + NativeTokenTransfer
            )
        )

CRITICAL NOTES:
  - Hash: keccak256 (Ethereum-style, NOT NIST SHA3-256)
  - handler_address: App ID padded to 32 bytes, NOT the app address
  - payload: starts at NttManagerPayload[64:] (len_prefix + NativeTokenTransfer)
  - source_chain_id: UInt16 big-endian (2 bytes)
"""

import struct
import hashlib


# ============================================================================
#  Pure Python Keccak-256 (Ethereum-compatible, NOT NIST SHA3-256)
# ============================================================================
# Difference: Keccak uses 0x01 padding suffix; SHA3 uses 0x06.

def _rot64(x, n):
    return ((x << n) | (x >> (64 - n))) & 0xFFFFFFFFFFFFFFFF

_KECCAK_RC = [
    0x0000000000000001, 0x0000000000008082, 0x800000000000808A, 0x8000000080008000,
    0x000000000000808B, 0x0000000080000001, 0x8000000080008081, 0x8000000000008009,
    0x000000000000008A, 0x0000000000000088, 0x0000000080008009, 0x000000008000000A,
    0x000000008000808B, 0x800000000000008B, 0x8000000000008089, 0x8000000000008003,
    0x8000000000008002, 0x8000000000000080, 0x000000000000800A, 0x800000008000000A,
    0x8000000080008081, 0x8000000000008080, 0x0000000080000001, 0x8000000080008008,
]
_KECCAK_ROT = [
    [0,36,3,41,18],[1,44,10,45,2],[62,6,43,15,61],[28,55,25,21,56],[27,20,39,8,14]
]

def _keccak_f(state):
    for rc in _KECCAK_RC:
        C = [state[x][0]^state[x][1]^state[x][2]^state[x][3]^state[x][4] for x in range(5)]
        D = [C[(x-1)%5] ^ _rot64(C[(x+1)%5], 1) for x in range(5)]
        for x in range(5):
            for y in range(5):
                state[x][y] ^= D[x]
        B = [[0]*5 for _ in range(5)]
        for x in range(5):
            for y in range(5):
                B[y][(2*x+3*y)%5] = _rot64(state[x][y], _KECCAK_ROT[x][y])
        for x in range(5):
            for y in range(5):
                state[x][y] = B[x][y] ^ ((~B[(x+1)%5][y]) & B[(x+2)%5][y])
        state[0][0] ^= rc


def keccak256(data: bytes) -> bytes:
    """Keccak-256 (Ethereum-compatible). NOT the same as hashlib.sha3_256."""
    rate = 136  # (1600 - 512) / 8
    state = [[0]*5 for _ in range(5)]

    # Padding: Keccak uses 0x01 suffix (SHA3 uses 0x06)
    padded = bytearray(data)
    padded.append(0x01)
    while len(padded) % rate != 0:
        padded.append(0x00)
    padded[-1] |= 0x80

    # Absorb
    for offset in range(0, len(padded), rate):
        block = padded[offset:offset+rate]
        for i in range(min(len(block) // 8, rate // 8)):
            x = i % 5
            y = i // 5
            state[x][y] ^= struct.unpack_from('<Q', block, i * 8)[0]
        _keccak_f(state)

    # Squeeze (32 bytes)
    out = b''
    for i in range(25):
        x = i % 5
        y = i // 5
        out += struct.pack('<Q', state[x][y])
        if len(out) >= 32:
            return out[:32]
    return out[:32]


# Verify against known test vector
assert keccak256(b"").hex() == \
    "c5d2460186f7233c927e7db2dcc703c0e500b653ca82273b7bfad8045d85a470", \
    "Keccak-256 implementation verification failed"


# ============================================================================
#  NTT Message Digest
# ============================================================================

def compute_ntt_message_digest(
    source_chain_id: int,
    source_ntt_manager: bytes,      # 32 bytes from TransceiverMessage header
    handler_app_id: int,            # Algorand NTT Manager App ID
    ntt_manager_payload: bytes,     # Full NttManagerPayload from TransceiverMessage
) -> bytes:
    """
    Compute the TransceiverManager message digest for a given NTT transfer.

    Args:
        source_chain_id:    Wormhole chain ID of the source (e.g. 30 for Base)
        source_ntt_manager: 32-byte source NTT manager address from TransceiverMessage
        handler_app_id:     Algorand NTT Manager application ID (set via NTT_MGR_APP_ID in .env)
        ntt_manager_payload: Raw NttManagerPayload bytes (145 bytes for standard NTT)

    Returns:
        32-byte keccak256 digest

    NttManagerPayload structure:
        [0:32]   message_id (id)
        [32:64]  message_sender (user_address)
        [64:66]  inner_payload_length
        [66:]    inner_payload (NativeTokenTransfer)

    The digest input is:
        id(32) + user_address(32) + source_chain(2) + source_address(32)
        + handler_address(32) + len_prefix(2) + inner_payload(var)
    """
    if len(ntt_manager_payload) < 66:
        raise ValueError(
            f"NttManagerPayload too short: {len(ntt_manager_payload)} bytes (min 66)"
        )

    message_id = ntt_manager_payload[0:32]
    user_address = ntt_manager_payload[32:64]
    source_chain_bytes = source_chain_id.to_bytes(2, 'big')  # UInt16 BE
    handler_address = b'\x00' * 24 + handler_app_id.to_bytes(8, 'big')  # App ID padded to 32
    payload = ntt_manager_payload[64:]  # len_prefix + NativeTokenTransfer

    preimage = (
        message_id          # 32 bytes
        + user_address      # 32 bytes
        + source_chain_bytes  # 2 bytes
        + source_ntt_manager  # 32 bytes
        + handler_address   # 32 bytes
        + payload           # 2 + inner_len bytes
    )

    return keccak256(preimage)


def parse_transceiver_message(vaa_payload: bytes) -> dict:
    """
    Parse a TransceiverMessage from the VAA payload.

    TransceiverMessage structure (NTT prefix 0x9945FF10):
        [0:4]    prefix (0x9945FF10)
        [4:36]   source_ntt_manager (32 bytes)
        [36:68]  recipient_ntt_manager (32 bytes)
        [68:70]  ntt_manager_payload_length (uint16 BE)
        [70:70+N] ntt_manager_payload (N bytes)
        [70+N:72+N] transceiver_payload_length (uint16 BE)
        [72+N:]  transceiver_payload
    """
    if len(vaa_payload) < 70:
        raise ValueError(f"VAA payload too short: {len(vaa_payload)} bytes")

    prefix = vaa_payload[0:4]
    if prefix != b'\x99\x45\xff\x10':
        raise ValueError(f"Invalid TransceiverMessage prefix: {prefix.hex()}")

    source_ntt_mgr = vaa_payload[4:36]
    recipient_ntt_mgr = vaa_payload[36:68]
    mgr_payload_len = int.from_bytes(vaa_payload[68:70], 'big')
    mgr_payload = vaa_payload[70:70 + mgr_payload_len]

    tc_offset = 70 + mgr_payload_len
    tc_payload_len = int.from_bytes(vaa_payload[tc_offset:tc_offset + 2], 'big')
    tc_payload = vaa_payload[tc_offset + 2:tc_offset + 2 + tc_payload_len]

    return {
        "source_ntt_manager": source_ntt_mgr,
        "recipient_ntt_manager": recipient_ntt_mgr,
        "ntt_manager_payload": mgr_payload,
        "ntt_manager_payload_length": mgr_payload_len,
        "transceiver_payload": tc_payload,
    }


def get_receive_message_box_refs(
    wt_app_id: int,
    tm_app_id: int,
    source_chain_id: int,
    ntt_manager_digest: bytes,
    vaa_digest: bytes,
) -> list:
    """
    Compute all box references needed for WormholeTransceiver.receive_message.

    Returns list of (app_id, box_name) tuples.
    """
    return [
        # WormholeTransceiver boxes
        (wt_app_id, b"wormhole_peer_" + source_chain_id.to_bytes(2, 'big')),
        (wt_app_id, b"vaas_consumed_" + vaa_digest),
        # TransceiverManager box (the one that was failing!)
        (tm_app_id, b"num_attestations_" + ntt_manager_digest),
    ]


# ============================================================================
#  Self-test with known VAA data
# ============================================================================

if __name__ == "__main__":
    # Keccak-256 self-test (standard empty-input test vector)
    assert keccak256(b"").hex() == \
        "c5d2460186f7233c927e7db2dcc703c0e500b653ca82273b7bfad8045d85a470"
    print("keccak256 self-test: OK")

    # Usage example — substitute your own VAA bytes:
    #   vaa_bytes = bytes.fromhex("<hex VAA>")
    #   parsed = parse_transceiver_message(vaa_bytes[body_offset + 51:])
    #   digest = compute_ntt_message_digest(
    #       source_chain_id=30,
    #       source_ntt_manager=parsed["source_ntt_manager"],
    #       handler_app_id=<NTT_MGR_APP_ID>,
    #       ntt_manager_payload=parsed["ntt_manager_payload"],
    #   )
    #   print(f"digest: {digest.hex()}")
