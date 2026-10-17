#!/usr/bin/env bash
# Creates .env from .env.example if missing and fills blank secrets with random values.
# Never overwrites a value that is already set. The Copernicus S3 keys are yours to add.
set -euo pipefail
cd "$(dirname "$0")/.."

[ -f .env ] || cp .env.example .env

rand() { python3 -c "import secrets; print(secrets.token_urlsafe(24))"; }
fernet() { python3 -c "import base64, os; print(base64.urlsafe_b64encode(os.urandom(32)).decode())"; }

ensure() {  # ensure NAME generator: append NAME=<generated> unless NAME already has a value
  local name="$1" gen="$2"
  if grep -qE "^${name}=.+" .env; then return; fi
  sed -i.bak -E "/^${name}=/d" .env && rm -f .env.bak
  printf '%s=%s\n' "$name" "$($gen)" >> .env
  echo "generated ${name}"
}

for v in POSTGRES_PASSWORD STORE_SECRET_KEY GRAFANA_DB_PASSWORD AIRFLOW_DB_PASSWORD AIRFLOW_JWT_SECRET AIRFLOW_API_SECRET_KEY; do
  ensure "$v" rand
done
ensure AIRFLOW_FERNET_KEY fernet
grep -qE '^STORE_ACCESS_KEY=.+' .env || printf 'STORE_ACCESS_KEY=sar\n' >> .env

for v in CDSE_S3_ACCESS_KEY CDSE_S3_SECRET_KEY; do
  grep -qE "^${v}=.+" .env || echo "TODO: set ${v} in .env (see README)"
done
