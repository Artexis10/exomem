#!/bin/sh
# Managed by the k3s role. Creates this node's WireGuard private key once,
# where only root can read it, and prints the public key. The private key
# never leaves the node.
set -eu
key="/etc/wireguard/$1.key"
if [ ! -s "$key" ]; then
  umask 077
  /usr/bin/wg genkey > "$key.new"
  mv "$key.new" "$key"
  echo created >&2
fi
/usr/bin/wg pubkey < "$key"
