#!/usr/bin/env bash
set -euo pipefail

script_dir="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)"
infra_dir="$(cd -- "${script_dir}/.." && pwd -P)"
repo_root="$(cd -- "${infra_dir}/.." && pwd -P)"
terraform_bin="${TERRAFORM_BIN:-terraform}"

for root in foundation durability bootstrap; do
  "${terraform_bin}" -chdir="${infra_dir}/terraform/${root}" fmt -check -recursive
  "${terraform_bin}" -chdir="${infra_dir}/terraform/${root}" init -backend=false -input=false
  "${terraform_bin}" -chdir="${infra_dir}/terraform/${root}" validate
done

ansible-playbook --syntax-check "${infra_dir}/ansible/site.yml"
ansible-lint "${infra_dir}/ansible"

for chart in platform cell; do
  helm lint "${infra_dir}/helm/${chart}" --strict
  helm template "exomem-${chart}" "${infra_dir}/helm/${chart}" \
    --values "${infra_dir}/helm/${chart}/values.validation.yaml" \
    | kubeconform -strict -summary -ignore-missing-schemas
done

uv run --frozen pytest -q "${repo_root}/tests/test_hosted_infra_scaffold.py"
uvx ruff check "${infra_dir}/scripts/inspect_terraform_plan.py" \
  "${repo_root}/tests/test_hosted_infra_scaffold.py"
shellcheck "${script_dir}"/*.sh
