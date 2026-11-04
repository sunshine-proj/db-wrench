#!/usr/bin/env bash
# Creates ./secrets/* for docker-compose.prod.yml. TLS certificate and key are NOT generated:
# place your certificate chain at secrets/tls_cert.pem and the key at secrets/tls_key.pem.
set -euo pipefail
cd "$(dirname "$0")/.."
command -v openssl >/dev/null || { echo "openssl is required" >&2; exit 1; }
mkdir -p secrets && chmod 700 secrets
umask 077
if [ -e secrets/postgres_password ]; then echo "secrets/ already initialised; not overwriting." >&2; exit 1; fi
password="$(openssl rand -hex 24)"
printf '%s' "$password" > secrets/postgres_password
printf 'postgresql+psycopg://dbwrench:%s@db:5432/dbwrench' "$password" > secrets/database_url
printf 'k1:%s' "$(openssl rand -base64 32)" > secrets/encryption_keys
echo "Created secrets/. Add secrets/tls_cert.pem and secrets/tls_key.pem, and back up"
echo "secrets/encryption_keys: losing it makes stored connection credentials unrecoverable."
