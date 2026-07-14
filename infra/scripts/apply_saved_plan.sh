#!/usr/bin/env bash
set -euo pipefail

umask 077

script_dir="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)"
infra_dir="$(cd -- "${script_dir}/.." && pwd -P)"
terraform_bin="${TERRAFORM_BIN:-terraform}"
python_bin="${PYTHON_BIN:-python3}"
backend_config="${TF_BACKEND_CONFIG_FILE:-}"

usage() {
  echo "usage: $0 <foundation|durability> <saved-plan-path> [--allow-destructive ADDRESS ...]" >&2
  exit 2
}

[[ $# -ge 2 ]] || usage
root_name="$1"
plan_path="$2"
shift 2

case "${root_name}" in
  foundation|durability) ;;
  *) usage ;;
esac

root="${infra_dir}/terraform/${root_name}"
if [[ -z "${backend_config}" || ! -f "${backend_config}" ]]; then
  echo "TF_BACKEND_CONFIG_FILE must name an existing backend config" >&2
  exit 2
fi
if [[ "$(stat -c "%a" -- "${backend_config}")" != "600" ]]; then
  echo "backend config must have mode 0600" >&2
  exit 2
fi
if [[ -z "${AWS_ACCESS_KEY_ID:-}" || -z "${AWS_SECRET_ACCESS_KEY:-}" ]]; then
  echo "AWS_ACCESS_KEY_ID and AWS_SECRET_ACCESS_KEY must contain the prefix-scoped B2 backend identity" >&2
  exit 2
fi

plan_path="$(cd -- "$(dirname -- "${plan_path}")" && pwd -P)/$(basename -- "${plan_path}")"
[[ -f "${plan_path}" ]] || usage
[[ "$(stat -c '%a' "${plan_path}")" == "600" ]] || {
  echo "saved plan must have mode 0600" >&2
  exit 2
}

inspector_args=()
while [[ $# -gt 0 ]]; do
  [[ "$1" == "--allow-destructive" && $# -ge 2 ]] || usage
  inspector_args+=("$1" "$2")
  shift 2
done

json_path="$(mktemp "${root}/.apply-plan.XXXXXX.json")"
cleanup() {
  rm -f -- "${json_path}"
}
trap cleanup EXIT

"${terraform_bin}" -chdir="${root}" init -input=false -backend-config="${backend_config}"
"${terraform_bin}" -chdir="${root}" show -json "${plan_path}" >"${json_path}"
chmod 0600 -- "${json_path}"
"${python_bin}" "${script_dir}/inspect_terraform_plan.py" "${json_path}" "${inspector_args[@]}"
"${terraform_bin}" -chdir="${root}" apply -input=false "${plan_path}"
