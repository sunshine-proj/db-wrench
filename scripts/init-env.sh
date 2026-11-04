#!/usr/bin/env bash
# Creates .env from .env.example with freshly generated secrets.
set -euo pipefail
cd "$(dirname "$0")/.."
if [ -f .env ]; then echo ".env already exists; not overwriting it." >&2; exit 1; fi
command -v openssl >/dev/null || { echo "openssl is required" >&2; exit 1; }
password="$(openssl rand -hex 24)"
kek="$(openssl rand -base64 32)"
umask 077
sed -e "s|^POSTGRES_PASSWORD=.*|POSTGRES_PASSWORD=${password}|" \
    -e "s|^DBW_ENCRYPTION_KEYS=.*|DBW_ENCRYPTION_KEYS=k1:${kek}|" \
    .env.example > .env
echo "Created .env (mode 600). Back up DBW_ENCRYPTION_KEYS somewhere safe: without it, stored"
echo "connection credentials cannot be decrypted."
