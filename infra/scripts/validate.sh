#!/usr/bin/env bash
set -euo pipefail

script_dir="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)"
infra_dir="$(cd -- "${script_dir}/.." && pwd -P)"
repo_root="$(cd -- "${infra_dir}/.." && pwd -P)"
# shellcheck source=/dev/null
source "${infra_dir}/tool-versions.env"
terraform_bin="${TERRAFORM_BIN:-terraform}"
tflint_bin="${TFLINT_BIN:-$(command -v tflint)}"
checkov_bin="${CHECKOV_BIN:-$(command -v checkov)}"
ansible_playbook_bin="${ANSIBLE_PLAYBOOK_BIN:-$(command -v ansible-playbook)}"
ansible_lint_bin="${ANSIBLE_LINT_BIN:-$(command -v ansible-lint)}"
helm_bin="${HELM_BIN:-$(command -v helm)}"
kubeconform_bin="${KUBECONFORM_BIN:-$(command -v kubeconform)}"
conftest_bin="${CONFTEST_BIN:-$(command -v conftest)}"
trivy_bin="${TRIVY_BIN:-$(command -v trivy)}"
helm_repository_config="$(mktemp)"
helm_repository_cache="$(mktemp -d)"
render_dir="$(mktemp -d)"

cleanup() {
  rm -f -- "${helm_repository_config}"
  rm -rf -- "${helm_repository_cache}" "${render_dir}"
}
trap cleanup EXIT

for root in foundation durability bootstrap; do
  "${terraform_bin}" -chdir="${infra_dir}/terraform/${root}" fmt -check -recursive
  "${terraform_bin}" -chdir="${infra_dir}/terraform/${root}" init -backend=false -input=false
  "${terraform_bin}" -chdir="${infra_dir}/terraform/${root}" validate
  "${tflint_bin}" --chdir="${infra_dir}/terraform/${root}" --format=compact
done

"${checkov_bin}" --directory "${infra_dir}/terraform" --framework terraform --quiet --compact

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
  --include-crds > "${render_dir}/platform.yaml"
"${kubeconform_bin}" -strict -summary -ignore-missing-schemas "${render_dir}/platform.yaml"
"${conftest_bin}" test --policy "${infra_dir}/policy" "${render_dir}/platform.yaml"

for values in values.validation.yaml values.initialize.yaml; do
  "${helm_bin}" lint "${infra_dir}/helm/cell" --strict \
    --values "${infra_dir}/helm/cell/${values}"
  "${helm_bin}" template exomem-cell "${infra_dir}/helm/cell" \
    --namespace cell-alpha-test \
    --values "${infra_dir}/helm/cell/${values}" > "${render_dir}/cell-${values}"
  "${kubeconform_bin}" -strict -summary -ignore-missing-schemas "${render_dir}/cell-${values}"
  "${conftest_bin}" test --policy "${infra_dir}/policy" "${render_dir}/cell-${values}"
done

"${script_dir}/validate_sops_ciphertext.py"
"${script_dir}/validate_sops_ciphertext.py" \
  --matrix "${repo_root}/tests/fixtures/hosted-sops/secret-destinations-v1.json" \
  --artifact "${repo_root}/tests/fixtures/hosted-sops/cloudflared-token.v1.sops.json" \
  --require-artifact
TERRAFORM_BIN="${terraform_bin}" \
ANSIBLE_PLAYBOOK_BIN="${ansible_playbook_bin}" \
HELM_BIN="${helm_bin}" \
uv run --frozen pytest -q "${repo_root}"/tests/test_hosted_*.py
uvx --from "ruff==${RUFF_VERSION}" ruff check \
  "${repo_root}"/tests/test_hosted_*.py "${infra_dir}"/scripts/*.py \
  "${infra_dir}/helm/platform/files/scheduler_runtime.py"
uvx --from "mypy==${MYPY_VERSION}" mypy \
  --follow-imports skip --ignore-missing-imports --check-untyped-defs \
  "${infra_dir}"/scripts/*.py
uv run --project "${infra_dir}/provisioner" --frozen pytest -q \
  --confcutdir="${infra_dir}/provisioner" "${infra_dir}/provisioner/tests"
uv run --project "${infra_dir}/provisioner" --frozen ruff check \
  "${infra_dir}/provisioner/src" "${infra_dir}/provisioner/tests"
shellcheck "${script_dir}"/*.sh
"${trivy_bin}" fs --scanners secret --exit-code 1 --no-progress \
  --skip-dirs .git --skip-dirs .venv --skip-dirs .venv-hosted-ci \
  --skip-dirs .terraform --skip-dirs charts "${repo_root}"
