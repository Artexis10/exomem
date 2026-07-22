#!/usr/bin/env bash
set -euo pipefail

umask 077

script_dir="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)"
infra_dir="$(cd -- "${script_dir}/.." && pwd -P)"
terraform_bin="${TERRAFORM_BIN:-terraform}"
python_bin="${PYTHON_BIN:-python3}"

usage() {
  echo "usage: $0 <foundation|durability> <saved-plan-path> [terraform plan args...]" >&2
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
if [[ -z "${TF_CLOUD_ORGANIZATION:-}" ]]; then
  echo "TF_CLOUD_ORGANIZATION must name the approved HCP Terraform organization" >&2
  exit 2
fi
if [[ -z "${TF_TOKEN_app_terraform_io:-}" ]]; then
  echo "TF_TOKEN_app_terraform_io must contain the HCP Terraform user or team token" >&2
  exit 2
fi
workspace="exomem-hosted-${root_name}"
unset TF_WORKSPACE
"${python_bin}" "${script_dir}/verify_hcp_backend.py" preflight --workspace "${workspace}"

plan_parent="$(cd -- "$(dirname -- "${plan_path}")" && pwd -P)"
plan_path="${plan_parent}/$(basename -- "${plan_path}")"
json_path="$(mktemp "${root}/.review-plan.XXXXXX.json")"
cleanup() {
  rm -f -- "${json_path}"
}
trap cleanup EXIT

"${terraform_bin}" -chdir="${root}" init -input=false
"${terraform_bin}" -chdir="${root}" fmt -check -recursive
"${terraform_bin}" -chdir="${root}" validate
"${terraform_bin}" -chdir="${root}" plan -input=false -out="${plan_path}" "$@"
chmod 0600 -- "${plan_path}"
"${terraform_bin}" -chdir="${root}" show -json "${plan_path}" >"${json_path}"
chmod 0600 -- "${json_path}"
"${python_bin}" "${script_dir}/inspect_terraform_plan.py" "${json_path}"

echo "saved plan ready for review: ${plan_path}"
