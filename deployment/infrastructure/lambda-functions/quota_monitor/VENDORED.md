# Vendored dependencies — quota_monitor

`quota_monitor` connects to a PostgreSQL/TimescaleDB instance, which the Lambda
runtime has no driver for. Per `../CLAUDE.md` ("Bundle all dependencies in the
deployment package"), the driver is vendored directly into this function
directory.

`aws cloudformation package` zips this directory verbatim — there is no Lambda
layer, no build step and no `requirements.txt` anywhere in this repo — so the
packages must sit next to `index.py` for `import pg8000.dbapi` to resolve with
no `sys.path` manipulation.

## Why pg8000 and not psycopg2

`pg8000` is **pure Python**. `psycopg2-binary` ships platform wheels, which
would mean fetching a `manylinux2014_x86_64` wheel on an ARM Mac
(`pip install --platform ... --only-binary :all: --target`) and adding a build
step for one query. Pure Python also decouples this function from
`Architectures`, so the template can move to `arm64` later without repackaging.

## Contents

| package | version | license |
|---|---|---|
| `pg8000` | 1.31.5 | BSD-3-Clause |
| `scramp` | 1.4.17 | MIT |
| `asn1crypto` | 1.5.1 | MIT |
| `dateutil` (python-dateutil) | 2.9.0.post0 | Apache-2.0 / BSD-3-Clause |
| `six` | 1.17.0 | MIT |

`scramp`, `asn1crypto`, `dateutil` and `six` are transitive dependencies of
`pg8000`, not direct requirements. The `*.dist-info` directories are kept
deliberately — they carry the license metadata.

## Regenerating

```sh
cd deployment/infrastructure/lambda-functions/quota_monitor
rm -rf pg8000* scramp* asn1crypto* dateutil* python_dateutil* six*
python3 -m pip install --target . --no-compile 'pg8000==1.31.5'
find . -name '__pycache__' -type d -prune -exec rm -rf {} +
rm -rf bin
```

Verify nothing platform-specific slipped in (must print nothing):

```sh
find . \( -name '*.so' -o -name '*.pyd' -o -name '*.dylib' \)
```

## Notes

- `pg8000.dbapi.connect()` has **no** `options=` parameter, so the server-side
  `statement_timeout` is applied as a `SET` statement after connecting
  (see `_db_connect` in `index.py`).
- The import is **lazy** (inside `_db_connect`), not module-scope, because the
  unit tests `exec_module` `index.py` directly and `pg8000` is not a dependency
  of `source/pyproject.toml`. Keep it lazy or the whole test module breaks.
