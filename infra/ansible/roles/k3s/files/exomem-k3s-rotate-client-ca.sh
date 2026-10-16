#!/bin/sh
# Managed by the k3s role (move-cloud-cells-to-local-storage task 5.8). Run as
# root on the K3s server while K3s runs. Then restart K3s on the server, and at
# once on every agent (docs/runbooks/cloud-dedicated-host.md, Rotate the
# client CA).
#
# It loads a new cluster client CA into K3s's datastore. Once K3s restarts, it
# refuses every client certificate the old CA signed, a lost agent disk's
# kubelet certificate among them. Without an argument it generates the new CA
# here. With --restore DIR it loads the pair an earlier run kept in DIR, which
# undoes that run. The key never leaves this host: K3s keeps it in its
# datastore, as it keeps the CA it replaces.
#
# Without --force, `k3s certificate rotate-ca` accepts a new CA only if it
# chains to the current one, so that certificates from both stay trusted. This
# rotation must end that trust, so it loads an unrelated CA with --force. That
# skips all of K3s's checks, so this checks the pair first.
set -eu
umask 077
server=/var/lib/rancher/k3s/server
live="$server/tls"

usage="usage: $0 [--restore DIR]"
case "${1:-}" in
  "") restore="" ;;
  --restore) restore="${2:?$usage}" ;;
  *) echo "$usage" >&2; exit 2 ;;
esac

fingerprint() {
  openssl x509 -noout -fingerprint -sha256 -in "$1" | cut -d= -f2
}

stage="$(mktemp -d "$server/rotate-client-ca.XXXXXX")"
trap 'rm -rf "$stage"' EXIT
mkdir "$stage/tls"
if [ -z "$restore" ]; then
  # The shape K3s gives the CAs it generates: a self-signed P-256 CA for ten years.
  cat > "$stage/openssl.cnf" <<'EOF'
[req]
distinguished_name = dn
[dn]
[client_ca]
basicConstraints = critical, CA:true
keyUsage = critical, digitalSignature, keyEncipherment, keyCertSign
subjectKeyIdentifier = hash
EOF
  openssl ecparam -name prime256v1 -genkey -noout -out "$stage/tls/client-ca.key"
  openssl req -x509 -new -sha256 -days 3650 -config "$stage/openssl.cnf" -extensions client_ca \
    -subj "/CN=k3s-client-ca@$(date +%s)" -key "$stage/tls/client-ca.key" -out "$stage/tls/client-ca.crt"
else
  # Written afresh, never copied with their times: at start, K3s stops on a
  # file on disk that is newer than its copy in the datastore.
  cat "$restore/client-ca.crt" > "$stage/tls/client-ca.crt"
  cat "$restore/client-ca.key" > "$stage/tls/client-ca.key"
fi

if [ "$(openssl x509 -noout -pubkey -in "$stage/tls/client-ca.crt")" != \
     "$(openssl pkey -pubout -in "$stage/tls/client-ca.key")" ]; then
  echo "refused: the client CA's key does not match its certificate" >&2
  exit 1
fi
old="$(fingerprint "$live/client-ca.crt")"
new="$(fingerprint "$stage/tls/client-ca.crt")"
if [ "$old" = "$new" ]; then
  echo "refused: the cluster already uses this client CA" >&2
  exit 1
fi

# The pair this replaces, kept beside K3s's own data for the undo.
kept="$server/client-ca-before-$(date -u +%Y%m%dT%H%M%SZ)"
mkdir "$kept"
cat "$live/client-ca.crt" > "$kept/client-ca.crt"
cat "$live/client-ca.key" > "$kept/client-ca.key"
if ! k3s certificate rotate-ca --path="$stage" --force; then
  rm -rf "$kept"
  echo "k3s certificate rotate-ca failed, and the cluster keeps its client CA; see the K3s server log" >&2
  exit 1
fi
echo "old client CA: $old"
echo "new client CA: $new"
echo "previous pair kept in: $kept"
echo "now restart K3s on the server, then at once on every agent"
