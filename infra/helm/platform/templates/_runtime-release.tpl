{{- define "exomem.hostedDeploymentLockJson" -}}
{{- $raw := required "provisioner.deploymentLockJson is required" .Values.provisioner.deploymentLockJson -}}
{{- $expectedSha := required "provisioner.deploymentLockSha256 is required" .Values.provisioner.deploymentLockSha256 -}}
{{- if not (regexMatch "^[0-9a-f]{64}$" $expectedSha) -}}
{{- fail "deployment lock SHA-256 is invalid" -}}
{{- end -}}
{{- if ne ($raw | sha256sum) $expectedSha -}}
{{- fail "deployment lock SHA-256 mismatch" -}}
{{- end -}}
{{- $lock := mustFromJson $raw -}}
{{- if not (kindIs "map" $lock) -}}
{{- fail "deployment lock must be one JSON object" -}}
{{- end -}}
{{- $lockKeys := list "artifact" "schemaVersion" "admissionMode" "components" "runtimeTarget" "composition" "rollback" -}}
{{- if ne (len $lock) (len $lockKeys) -}}
{{- fail "deployment lock fields are incomplete or unknown" -}}
{{- end -}}
{{- range $key := $lockKeys -}}
{{- if not (hasKey $lock $key) -}}
{{- fail (printf "deployment lock is missing %s" $key) -}}
{{- end -}}
{{- end -}}
{{- if or (ne $lock.artifact "exomem-hosted-deployment-lock") (ne (toJson $lock.schemaVersion) "2") -}}
{{- fail "deployment lock identity is invalid" -}}
{{- end -}}
{{- if not (has $lock.admissionMode (list "expand" "contract")) -}}
{{- fail "deployment lock admission mode is invalid" -}}
{{- end -}}
{{- if or (not (kindIs "map" $lock.components)) (ne (len $lock.components) 2) (not (hasKey $lock.components "runtime")) (not (hasKey $lock.components "provisioner")) -}}
{{- fail "deployment lock components are invalid" -}}
{{- end -}}
{{- $runtime := $lock.components.runtime -}}
{{- $provisioner := $lock.components.provisioner -}}
{{- if or (not (kindIs "map" $runtime)) (ne (len $runtime) 3) (not (regexMatch "^ghcr\\.io/artexis10/exomem@sha256:[0-9a-f]{64}$" $runtime.image)) (not (regexMatch "^[0-9a-f]{40}$" $runtime.sourceCommit)) (not (regexMatch "^[0-9a-f]{64}$" $runtime.candidateSha256)) -}}
{{- fail "deployment lock runtime component is invalid" -}}
{{- end -}}
{{- if or (not (kindIs "map" $provisioner)) (ne (len $provisioner) 4) (not (regexMatch "^ghcr\\.io/artexis10/exomem-provisioner@sha256:[0-9a-f]{64}$" $provisioner.image)) (not (regexMatch "^[0-9a-f]{40}$" $provisioner.sourceCommit)) (not (regexMatch "^[0-9a-f]{64}$" $provisioner.candidateSha256)) (ne $provisioner.wireProtocol "exomem-cell-provisioner.v2") -}}
{{- fail "deployment lock provisioner component is invalid" -}}
{{- end -}}
{{- $target := $lock.runtimeTarget -}}
{{- $targetKeys := list "releaseVersion" "protocolVersion" "agentProfile" "gatewayContractDigest" "commandFingerprint" "schemaDigest" -}}
{{- if or (not (kindIs "map" $target)) (ne (len $target) (len $targetKeys)) -}}
{{- fail "deployment lock runtime target is invalid" -}}
{{- end -}}
{{- range $key := $targetKeys -}}
{{- if not (hasKey $target $key) -}}
{{- fail (printf "deployment lock runtime target is missing %s" $key) -}}
{{- end -}}
{{- end -}}
{{- if or (not (regexMatch "^[0-9a-f]{64}$" $target.gatewayContractDigest)) (not (regexMatch "^[0-9a-f]{64}$" $target.commandFingerprint)) (not (regexMatch "^[0-9a-f]{64}$" $target.schemaDigest)) -}}
{{- fail "deployment lock runtime target digest is invalid" -}}
{{- end -}}
{{- if or (not (kindIs "map" $lock.composition)) (not (kindIs "map" $lock.rollback)) -}}
{{- fail "deployment lock lineage is invalid" -}}
{{- end -}}
{{- $raw -}}
{{- end -}}

{{- define "exomem.hostedDeploymentLock" -}}
{{- include "exomem.hostedDeploymentLockJson" . | mustFromJson | toJson -}}
{{- end -}}

{{- define "exomem.hostedRuntimeImage" -}}
{{- $lock := include "exomem.hostedDeploymentLock" . | mustFromJson -}}
{{- $lock.components.runtime.image -}}
{{- end -}}

{{- define "exomem.hostedProvisionerImage" -}}
{{- $lock := include "exomem.hostedDeploymentLock" . | mustFromJson -}}
{{- $lock.components.provisioner.image -}}
{{- end -}}

{{- define "exomem.hostedDeploymentLockName" -}}
{{- printf "exomem-hosted-deployment-lock-v2-%s" (trunc 16 .Values.provisioner.deploymentLockSha256) -}}
{{- end -}}
