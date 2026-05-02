# Third-Party Notices

This project lists the following third-party packages as runtime dependencies.
These packages are not bundled in this repository — they are installed separately
via `pip`. This file is provided for compliance with Apache 2.0 Section 4(d)
in the event the project is ever distributed in bundled form (Docker image,
wheel, binary).

---

## requests

- **Version**: ≥2.31.0
- **License**: Apache License 2.0
- **Copyright**: Copyright 2019 Kenneth Reitz
- **Source**: https://github.com/psf/requests
- **License text**: https://github.com/psf/requests/blob/main/LICENSE

---

## py-algorand-sdk (algosdk)

- **Version**: ≥2.5.0
- **License**: MIT License
- **Copyright**: Copyright (c) 2019 Algorand
- **Source**: https://github.com/algorand/py-algorand-sdk
- **License text**: https://github.com/algorand/py-algorand-sdk/blob/master/LICENSE

---

## pycryptodome

- **Version**: ≥3.19.0
- **License**: BSD 2-Clause / Public Domain (see package for per-file details)
- **Source**: https://github.com/Legrandin/pycryptodome
- **License text**: https://github.com/Legrandin/pycryptodome/blob/master/LICENSE.rst

---

## python-dotenv

- **Version**: ≥1.0.0
- **License**: BSD 3-Clause License
- **Copyright**: Copyright (c) 2014, Saurabh Kumar
- **Source**: https://github.com/theskumar/python-dotenv
- **License text**: https://github.com/theskumar/python-dotenv/blob/main/LICENSE

---

## Wormhole `vaa_verify.teal`

This project's setup script downloads `vaa_verify.teal` from the Wormhole
Foundation GitHub repository at runtime. It is not bundled in this source repo.

- **License**: Apache License 2.0
- **Copyright**: Copyright 2022 Wormhole Foundation
- **Source**: https://github.com/wormhole-foundation/wormhole
- **License text**: https://github.com/wormhole-foundation/wormhole/blob/main/LICENSE

If `vaa_verify.teal` is included in any bundled distribution, the Apache 2.0
license and copyright notice above must be included.

---

## Folks Finance algorand-ntt-contracts

The NTT message digest formula in `utils/ntt_digest.py` is a clean Python
reimplementation of the protocol wire format specified by the Folks Finance
Algorand NTT contracts. No source code from that repository is included.

- **License**: Apache License 2.0
- **Copyright**: Copyright 2025 Folks Finance Private Foundation
- **Source**: https://github.com/Folks-Finance/algorand-ntt-contracts
- **License text**: https://github.com/Folks-Finance/algorand-ntt-contracts/blob/main/LICENSE
