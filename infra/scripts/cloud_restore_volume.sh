#!/bin/sh
# Moves a cell's backed-up paths aside before an in-place restore, or moves
# them back. infra/scripts/cloud_restore.sh and the restore runbook's
# "Recover by hand" run it in the helper pod on the cell's volume, on stdin:
#
#   sh -s move|rollback RUN_ID PATH...
#
# Each PATH is one of cellctl's BACKUP_PATHS, all in one parent directory. The
# prior contents go to .restore-prior-<RUN_ID> in that directory. rollback
# moves back only what that directory holds, so a move that stopped partway is
# undone exactly, and it can run again after a stop.
set -eu
action=$1 id=$2
shift 2
root=${1%/*}
for path in "$@"; do
  [ "${path%/*}" = "$root" ] || { echo "the paths must share one parent; nothing moved" >&2; exit 1; }
done
prior=$root/.restore-prior-$id
case $action in
  move)
    [ ! -e "$prior" ] || { echo "$prior exists; nothing moved" >&2; exit 1; }
    mkdir -m 700 "$prior"
    for path in "$@"; do
      if [ -e "$path" ]; then mv "$path" "$prior/${path##*/}"; fi
    done
    ;;
  rollback)
    [ -d "$prior" ] || { echo "no prior directory; nothing was moved" >&2; exit 0; }
    for path in "$@"; do
      if [ -e "$prior/${path##*/}" ]; then
        rm -rf -- "$path"
        mv "$prior/${path##*/}" "$path"
      fi
    done
    rmdir "$prior" || echo "$prior is not empty; inspect it" >&2
    ;;
  *)
    echo "unknown action $action" >&2
    exit 2
    ;;
esac
