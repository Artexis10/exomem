#!/usr/bin/env bash
set -euo pipefail

umask 077

script_dir="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)"
infra_dir="$(cd -- "${script_dir}/.." && pwd -P)"
root="${infra_dir}/terraform/bootstrap"
terraform_bin="${TERRAFORM_BIN:-terraform}"
sops_bin="${SOPS_BIN:-sops}"
python_bin="${PYTHON_BIN:-python3}"
state_path="${root}/terraform.tfstate"

usage() {
  echo "usage: $0 plan <saved-plan> <encrypted-state> [terraform plan args...]" >&2
  echo "       $0 apply <saved-plan> <encrypted-state>" >&2
  echo "       $0 seal <encrypted-state>" >&2
  exit 2
}

[[ $# -ge 2 ]] || usage
action="$1"
shift

case "${action}" in
  plan|apply)
    [[ $# -ge 2 ]] || usage
    plan_path="$1"
    escrow_path="$2"
    shift 2
    ;;
  seal)
    [[ $# -eq 1 ]] || usage
    escrow_path="$1"
    plan_path=""
    shift
    ;;
  *) usage ;;
esac

escrow_parent="$(cd -- "$(dirname -- "${escrow_path}")" && pwd -P)"
escrow_path="${escrow_parent}/$(basename -- "${escrow_path}")"
plaintext_safe_to_remove=false
json_path=""

cleanup() {
  if [[ -n "${json_path}" ]]; then
    rm -f -- "${json_path}"
  fi
  if [[ "${plaintext_safe_to_remove}" == true ]]; then
    rm -f -- "${state_path}"
  elif [[ -f "${state_path}" ]]; then
    echo "bootstrap state remains at ${state_path}; seal it before continuing" >&2
  fi
}
trap cleanup EXIT

restore_state() {
  if [[ -f "${escrow_path}" ]]; then
    [[ "$(stat -c "%a" -- "${escrow_path}")" == "600" ]] || {
      echo "encrypted bootstrap state must have mode 0600" >&2
      exit 2
    }
    "${sops_bin}" decrypt --output "${state_path}" "${escrow_path}"
    chmod 0600 -- "${state_path}"
  elif [[ -f "${state_path}" ]]; then
    echo "refusing to overwrite unsealed bootstrap state at ${state_path}" >&2
    exit 2
  fi
  plaintext_safe_to_remove=true
}

seal_state() {
  [[ -f "${state_path}" ]] || {
    echo "bootstrap state does not exist" >&2
    exit 2
  }
  [[ -n "${SOPS_AGE_RECIPIENTS:-}" ]] || {
    echo "SOPS_AGE_RECIPIENTS is required to seal bootstrap state" >&2
    exit 2
  }

  local encrypted_tmp
  encrypted_tmp="$(mktemp "${escrow_parent}/.bootstrap-state.XXXXXX.sops.json")"
  if ! "${sops_bin}" encrypt --age "${SOPS_AGE_RECIPIENTS}" --output "${encrypted_tmp}" "${state_path}"; then
    rm -f -- "${encrypted_tmp}"
    return 1
  fi
  chmod 0600 -- "${encrypted_tmp}"
  "${sops_bin}" decrypt "${encrypted_tmp}" >/dev/null
  mv -f -- "${encrypted_tmp}" "${escrow_path}"
  chmod 0600 -- "${escrow_path}"
  plaintext_safe_to_remove=true
}

if [[ "${action}" == seal ]]; then
  plaintext_safe_to_remove=false
  seal_state
  exit 0
fi

plan_parent="$(cd -- "$(dirname -- "${plan_path}")" && pwd -P)"
plan_path="${plan_parent}/$(basename -- "${plan_path}")"
restore_state

"${terraform_bin}" -chdir="${root}" init -backend=false -input=false
"${terraform_bin}" -chdir="${root}" fmt -check -recursive
"${terraform_bin}" -chdir="${root}" validate

if [[ "${action}" == plan ]]; then
  "${terraform_bin}" -chdir="${root}" plan \
    -input=false \
    -state="${state_path}" \
    -out="${plan_path}" \
    "$@"
  chmod 0600 -- "${plan_path}"
  json_path="$(mktemp "${root}/.bootstrap-plan.XXXXXX.json")"
  "${terraform_bin}" -chdir="${root}" show -json "${plan_path}" >"${json_path}"
  chmod 0600 -- "${json_path}"
  "${python_bin}" "${script_dir}/inspect_terraform_plan.py" "${json_path}"
  echo "bootstrap plan ready for review: ${plan_path}"
  exit 0
fi

[[ $# -eq 0 && -f "${plan_path}" ]] || usage
[[ "$(stat -c "%a" -- "${plan_path}")" == "600" ]] || {
  echo "saved plan must have mode 0600" >&2
  exit 2
}
json_path="$(mktemp "${root}/.bootstrap-apply.XXXXXX.json")"
"${terraform_bin}" -chdir="${root}" show -json "${plan_path}" >"${json_path}"
chmod 0600 -- "${json_path}"
"${python_bin}" "${script_dir}/inspect_terraform_plan.py" "${json_path}"

plaintext_safe_to_remove=false
"${terraform_bin}" -chdir="${root}" apply \
  -input=false \
  -state="${state_path}" \
  -backup=- \
  "${plan_path}"
seal_state
