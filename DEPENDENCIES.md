# Runtime dependency inventory

Audit snapshot: 2026-07-19 on Windows/Python 3.14. Direct version ranges are
declared in `pyproject.toml`; resolved versions below are the R8 rehearsal set.
All are runtime dependencies. None is bundled in the source distribution or
wheel. Frozen-executable bundling remains deferred behind the focused LGPL
review.

| Package | Resolved | Direct | License | Source |
|---|---:|:---:|---|---|
| paramiko | 5.0.0 | yes | LGPL-2.1 | https://github.com/paramiko/paramiko |
| requests | 2.34.2 | yes | Apache-2.0 | https://github.com/psf/requests |
| starlette | 1.3.1 | yes | BSD-3-Clause | https://github.com/Kludex/starlette |
| uvicorn | 0.49.0 | yes | BSD-3-Clause | https://github.com/encode/uvicorn |
| anyio | 4.14.1 | no | MIT | https://github.com/agronholm/anyio |
| bcrypt | 5.0.0 | no | Apache-2.0 | https://github.com/pyca/bcrypt |
| certifi | 2026.6.17 | no | MPL-2.0 | https://github.com/certifi/python-certifi |
| cffi | 2.1.0 | no | MIT-0 | https://github.com/python-cffi/cffi |
| charset-normalizer | 3.4.7 | no | MIT | https://github.com/jawah/charset_normalizer |
| click | 8.4.2 | no | BSD-3-Clause | https://github.com/pallets/click |
| colorama | 0.4.6 | no | BSD-3-Clause | https://github.com/tartley/colorama |
| cryptography | 49.0.0 | no | Apache-2.0 OR BSD-3-Clause | https://github.com/pyca/cryptography |
| h11 | 0.16.0 | no | MIT | https://github.com/python-hyper/h11 |
| idna | 3.18 | no | BSD-3-Clause | https://github.com/kjd/idna |
| invoke | 3.0.3 | no | BSD-2-Clause | https://github.com/pyinvoke/invoke |
| pycparser | 3.0 | no | BSD-3-Clause | https://github.com/eliben/pycparser |
| PyNaCl | 1.6.2 | no | Apache-2.0 | https://github.com/pyca/pynacl |
| urllib3 | 2.7.0 | no | MIT | https://github.com/urllib3/urllib3 |

Development-only dependency: pytest `>=8,<10` (MIT). Build-only dependency:
setuptools `>=77` (MIT). Re-run and review this inventory whenever dependency
ranges or resolved artifacts change.
