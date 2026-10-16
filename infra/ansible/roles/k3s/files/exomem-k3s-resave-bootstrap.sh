#!/bin/sh
# Managed by the k3s role (move-cloud-cells-to-local-storage task 5.7). Run as
# root on the K3s server after a server restart that changed agent-token.
#
# K3s saves its bootstrap data to etcd at start only when etcd holds none, so
# etcd keeps the old cred/passwd and the next start stops with "newer than
# datastore". `k3s token rotate` with the current server token writes the data
# again over the old copy and changes no token. An empty or different value
# would rotate the real server token, so this refuses an empty token file and
# checks that the file is unchanged afterwards.
set -eu
token_file=/var/lib/rancher/k3s/server/token
before="$(sha256sum "$token_file")"
token="$(cat "$token_file")"
if [ -z "$token" ]; then
  echo "refused: $token_file is empty" >&2
  exit 1
fi
k3s token rotate --new-token "$token" > /dev/null
if [ "$(sha256sum "$token_file")" != "$before" ]; then
  echo "the server token changed: $token_file no longer matches its escrowed value" >&2
  exit 1
fi
echo "bootstrap data saved again; the server token is unchanged"
