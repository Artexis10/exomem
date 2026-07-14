#!/usr/bin/env bash
set -euo pipefail

script_dir="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)"
infra_dir="$(cd -- "${script_dir}/.." && pwd -P)"
repo_root="$(cd -- "${infra_dir}/.." && pwd -P)"
terraform_bin="${TERRAFORM_BIN:-terraform}"
ansible_playbook_bin="${ANSIBLE_PLAYBOOK_BIN:-$(command -v ansible-playbook)}"
ansible_lint_bin="${ANSIBLE_LINT_BIN:-$(command -v ansible-lint)}"
helm_bin="${HELM_BIN:-$(command -v helm)}"
kubeconform_bin="${KUBECONFORM_BIN:-$(command -v kubeconform)}"
helm_repository_config="$(mktemp)"
helm_repository_cache="$(mktemp -d)"

cleanup() {
  rm -f -- "${helm_repository_config}"
  rm -rf -- "${helm_repository_cache}"
}
trap cleanup EXIT

for root in foundation durability bootstrap; do
  "${terraform_bin}" -chdir="${infra_dir}/terraform/${root}" fmt -check -recursive
  "${terraform_bin}" -chdir="${infra_dir}/terraform/${root}" init -backend=false -input=false
  "${terraform_bin}" -chdir="${infra_dir}/terraform/${root}" validate
done

"${ansible_playbook_bin}" --syntax-check "${infra_dir}/ansible/site.yml"
"${ansible_lint_bin}" --profile production "${infra_dir}/ansible"

"${helm_bin}" repo add hcloud https://charts.hetzner.cloud \
  --repository-config "${helm_repository_config}" \
  --repository-cache "${helm_repository_cache}"
"${helm_bin}" repo add traefik https://traefik.github.io/charts \
  --repository-config "${helm_repository_config}" \
  --repository-cache "${helm_repository_cache}"
"${helm_bin}" dependency build "${infra_dir}/helm/platform" \
  --repository-config "${helm_repository_config}" \
  --repository-cache "${helm_repository_cache}"
"${helm_bin}" lint "${infra_dir}/helm/platform" --strict \
  --values "${infra_dir}/helm/platform/values.validation.yaml"
"${helm_bin}" template exomem-platform "${infra_dir}/helm/platform" \
  --namespace exomem-platform \
  --values "${infra_dir}/helm/platform/values.validation.yaml" \
  --include-crds \
  | "${kubeconform_bin}" -strict -summary -ignore-missing-schemas

for values in values.validation.yaml values.initialize.yaml; do
  "${helm_bin}" lint "${infra_dir}/helm/cell" --strict \
    --values "${infra_dir}/helm/cell/${values}"
  "${helm_bin}" template exomem-cell "${infra_dir}/helm/cell" \
    --namespace cell-alpha-test \
    --values "${infra_dir}/helm/cell/${values}" \
    | "${kubeconform_bin}" -strict -summary -ignore-missing-schemas
done

TERRAFORM_BIN="${terraform_bin}" \
ANSIBLE_PLAYBOOK_BIN="${ansible_playbook_bin}" \
HELM_BIN="${helm_bin}" \
uv run --frozen pytest -q "${repo_root}"/tests/test_hosted_*.py
uvx ruff check "${repo_root}"/tests/test_hosted_*.py "${infra_dir}"/scripts/*.py
shellcheck "${script_dir}"/*.sh
