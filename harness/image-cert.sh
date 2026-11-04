#!/bin/sh
# Throwaway certificate for the image. Nothing in git is this key.
set -eu
cd "$(dirname "$0")/.."
mkdir -p devdata/cert devdata/restore
chmod 777 devdata/restore
if [ -f devdata/cert/tls.crt ] && [ -f devdata/cert/tls.key ]; then
    exit 0
fi
config="$(mktemp)"
trap 'rm -f "$config"' EXIT
cat > "$config" <<'EOF'
[req]
distinguished_name = dn
x509_extensions = v3
prompt = no
[dn]
CN = 127.0.0.1
[v3]
subjectAltName = IP:127.0.0.1,DNS:localhost
basicConstraints = CA:TRUE
EOF
openssl req -x509 -newkey rsa:2048 -sha256 -days 2 -nodes \
    -keyout devdata/cert/tls.key \
    -out devdata/cert/tls.crt \
    -config "$config"
chmod 644 devdata/cert/tls.crt devdata/cert/tls.key
