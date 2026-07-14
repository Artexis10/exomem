#!/usr/bin/env bash
set -euo pipefail
set +x
umask 077

usage() {
  echo "usage: ansible_with_sops.sh --inventory PATH --vars FILE [--vars FILE ...] [-- ANSIBLE_ARGS ...]" >&2
}

script_dir="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)"
repo_root="$(cd -- "${script_dir}/../.." && pwd -P)"
sops_bin="${SOPS_BIN:-sops}"
ansible_playbook_bin="${ANSIBLE_PLAYBOOK_BIN:-ansible-playbook}"
tmpfs_root="${EXOMEM_SECRET_TMPFS_DIR:-${XDG_RUNTIME_DIR:-}}"
inventory=""
encrypted_vars=()
ansible_args=()

while (($# > 0)); do
  case "$1" in
    --inventory)
      (($# >= 2)) || { usage; exit 2; }
      inventory="$2"
      shift 2
      ;;
    --vars)
      (($# >= 2)) || { usage; exit 2; }
      encrypted_vars+=("$2")
      shift 2
      ;;
    --)
      shift
      ansible_args=("$@")
      break
      ;;
    *)
      usage
      exit 2
      ;;
  esac
done

for argument in "${ansible_args[@]}"; do
  case "${argument}" in
    -e | -e?* | --extra-vars | --extra-vars=*)
      echo "Ansible passthrough must not include extra vars" >&2
      exit 2
      ;;
  esac
done

if [[ -z "${inventory}" || ! -f "${inventory}" || ${#encrypted_vars[@]} -eq 0 ]]; then
  usage
  exit 2
fi
if [[ -z "${tmpfs_root}" || ! -d "${tmpfs_root}" ]]; then
  echo "EXOMEM_SECRET_TMPFS_DIR or XDG_RUNTIME_DIR must name an existing tmpfs" >&2
  exit 2
fi

tmpfs_root="$(cd -- "${tmpfs_root}" && pwd -P)"
filesystem_type="$(findmnt --noheadings --output FSTYPE --target "${tmpfs_root}")"
case "${filesystem_type}" in
  tmpfs | ramfs) ;;
  *)
    echo "secret workspace must be tmpfs or ramfs" >&2
    exit 2
    ;;
esac

secret_dir="$(mktemp -d "${tmpfs_root%/}/exomem-ansible-secrets.XXXXXX")"
cleanup() {
  find "${secret_dir}" -xdev -type f -delete 2>/dev/null || true
  rmdir -- "${secret_dir}" 2>/dev/null || true
}
trap cleanup EXIT HUP INT TERM

extra_vars=()
index=0
for encrypted_file in "${encrypted_vars[@]}"; do
  if [[ ! -f "${encrypted_file}" || -L "${encrypted_file}" || "${encrypted_file}" != *.sops.json ]]; then
    echo "encrypted Ansible vars input is missing or unsafe" >&2
    exit 2
  fi
  decrypted_file="${secret_dir}/vars-${index}.json"
  if ! "${sops_bin}" decrypt \
    --input-type json \
    --output-type json \
    --output "${decrypted_file}" \
    "${encrypted_file}" >/dev/null 2>&1; then
    echo "SOPS Ansible vars decryption failed" >&2
    exit 2
  fi
  chmod 0600 "${decrypted_file}"
  extra_vars+=(--extra-vars "@${decrypted_file}")
  index=$((index + 1))
done

"${ansible_playbook_bin}" \
  --inventory "${inventory}" \
  "${repo_root}/infra/ansible/site.yml" \
  "${extra_vars[@]}" \
  "${ansible_args[@]}"
