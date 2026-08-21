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
{{- $v2Keys := list "artifact" "schemaVersion" "admissionMode" "components" "runtimeTarget" "composition" "rollback" -}}
{{- $v3Keys := append $v2Keys "recordsCompatibility" -}}
{{- $lockKeys := ternary $v3Keys $v2Keys (eq (toJson $lock.schemaVersion) "3") -}}
{{- if hasKey $lock "runtimeUpgrade" -}}
{{- $lockKeys = append $lockKeys "runtimeUpgrade" -}}
{{- end -}}
{{- if ne (len $lock) (len $lockKeys) -}}
{{- fail "deployment lock fields are incomplete or unknown" -}}
{{- end -}}
{{- range $key := $lockKeys -}}
{{- if not (hasKey $lock $key) -}}
{{- fail (printf "deployment lock is missing %s" $key) -}}
{{- end -}}
{{- end -}}
{{- if or (ne $lock.artifact "exomem-hosted-deployment-lock") (not (has (toJson $lock.schemaVersion) (list "2" "3"))) -}}
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
{{- if hasKey $lock "runtimeUpgrade" -}}
{{- $upgrade := $lock.runtimeUpgrade -}}
{{- if or (not (kindIs "map" $upgrade)) (ne (len $upgrade) 4) (not (hasKey $upgrade "compatibilityDigest")) (not (hasKey $upgrade "migrationMode")) (not (hasKey $upgrade "substrateConsumerCommit")) (not (hasKey $upgrade "substrateTrustSha256")) (not (regexMatch "^[0-9a-f]{64}$" $upgrade.compatibilityDigest)) (not (has $upgrade.migrationMode (list "none" "binding-v1-to-v2"))) (not (regexMatch "^[0-9a-f]{40}$" $upgrade.substrateConsumerCommit)) (not (regexMatch "^[0-9a-f]{64}$" $upgrade.substrateTrustSha256)) -}}
{{- fail "deployment lock runtime upgrade is invalid" -}}
{{- end -}}
{{- end -}}
{{- $composition := $lock.composition -}}
{{- $compositionKeys := list "commit" "sourceClosure" "forwardContractSha256" "authoritativeLegacyReleaseSetSha256" "legacyCatalog" "legacyReleaseSetSha256" -}}
{{- if or (not (kindIs "map" $composition)) (ne (len $composition) (len $compositionKeys)) -}}
{{- fail "deployment lock composition is invalid" -}}
{{- end -}}
{{- range $key := $compositionKeys -}}
{{- if not (hasKey $composition $key) -}}
{{- fail (printf "deployment lock composition is missing %s" $key) -}}
{{- end -}}
{{- end -}}
{{- if or (not (kindIs "string" $composition.commit)) (not (regexMatch "^[0-9a-f]{40}$" $composition.commit)) (not (kindIs "string" $composition.forwardContractSha256)) (not (regexMatch "^[0-9a-f]{64}$" $composition.forwardContractSha256)) (not (kindIs "string" $composition.authoritativeLegacyReleaseSetSha256)) (not (regexMatch "^[0-9a-f]{64}$" $composition.authoritativeLegacyReleaseSetSha256)) (not (kindIs "string" $composition.legacyReleaseSetSha256)) (not (regexMatch "^[0-9a-f]{64}$" $composition.legacyReleaseSetSha256)) -}}
{{- fail "deployment lock composition identity is invalid" -}}
{{- end -}}
{{- $sourceClosure := $composition.sourceClosure -}}
{{- if or (not (kindIs "map" $sourceClosure)) (ne (len $sourceClosure) 2) (not (hasKey $sourceClosure "runtime")) (not (hasKey $sourceClosure "provisioner")) -}}
{{- fail "deployment lock source closure is invalid" -}}
{{- end -}}
{{- range $kind, $component := dict "runtime" $runtime "provisioner" $provisioner -}}
{{- $closure := index $sourceClosure $kind -}}
{{- $closureAnchor := ternary $component.sourceCommit $composition.commit (eq $kind "runtime") -}}
{{- $closureKeys := list "candidateCommit" "compositionCommit" "paths" -}}
{{- if or (not (kindIs "map" $closure)) (ne (len $closure) (len $closureKeys)) -}}
{{- fail (printf "deployment lock %s source closure is invalid" $kind) -}}
{{- end -}}
{{- range $key := $closureKeys -}}
{{- if not (hasKey $closure $key) -}}
{{- fail (printf "deployment lock %s source closure is missing %s" $kind $key) -}}
{{- end -}}
{{- end -}}
{{- if or (not (kindIs "string" $closure.candidateCommit)) (ne $closure.candidateCommit $component.sourceCommit) (not (kindIs "string" $closure.compositionCommit)) (ne $closure.compositionCommit $closureAnchor) (not (kindIs "slice" $closure.paths)) (lt (len $closure.paths) 1) -}}
{{- fail (printf "deployment lock %s source closure is invalid" $kind) -}}
{{- end -}}
{{- range $path := $closure.paths -}}
{{- if or (not (kindIs "string" $path)) (eq $path "") -}}
{{- fail (printf "deployment lock %s source closure paths are invalid" $kind) -}}
{{- end -}}
{{- end -}}
{{- end -}}
{{- if not (kindIs "slice" $composition.legacyCatalog) -}}
{{- fail "deployment lock legacy catalog is invalid" -}}
{{- end -}}
{{- range $legacy := $composition.legacyCatalog -}}
{{- $legacyKeys := list "releaseVersion" "protocolVersion" "runtimeImage" "sourceCommit" "contractSha256" "contract" -}}
{{- if or (not (kindIs "map" $legacy)) (ne (len $legacy) (len $legacyKeys)) -}}
{{- fail "deployment lock legacy catalog unit is invalid" -}}
{{- end -}}
{{- range $key := $legacyKeys -}}
{{- if not (hasKey $legacy $key) -}}
{{- fail (printf "deployment lock legacy catalog unit is missing %s" $key) -}}
{{- end -}}
{{- end -}}
{{- if or (not (kindIs "string" $legacy.releaseVersion)) (eq $legacy.releaseVersion "") (not (kindIs "string" $legacy.protocolVersion)) (eq $legacy.protocolVersion "") (not (kindIs "string" $legacy.runtimeImage)) (not (regexMatch "^ghcr\\.io/artexis10/exomem@sha256:[0-9a-f]{64}$" $legacy.runtimeImage)) (not (kindIs "string" $legacy.sourceCommit)) (not (regexMatch "^[0-9a-f]{40}$" $legacy.sourceCommit)) (not (kindIs "string" $legacy.contractSha256)) (not (regexMatch "^[0-9a-f]{64}$" $legacy.contractSha256)) -}}
{{- fail "deployment lock legacy catalog unit is invalid" -}}
{{- end -}}
{{- $contract := $legacy.contract -}}
{{- $contractKeys := list "releaseVersion" "protocolVersion" "agentProfile" "gatewayContractDigest" "commandFingerprint" "schemaDigest" "runtimeImage" "sourceCommit" -}}
{{- if or (not (kindIs "map" $contract)) (ne (len $contract) (len $contractKeys)) -}}
{{- fail "deployment lock legacy contract is invalid" -}}
{{- end -}}
{{- range $key := $contractKeys -}}
{{- if not (hasKey $contract $key) -}}
{{- fail (printf "deployment lock legacy contract is missing %s" $key) -}}
{{- end -}}
{{- end -}}
{{- if or (ne $contract.releaseVersion $legacy.releaseVersion) (ne $contract.protocolVersion $legacy.protocolVersion) (ne $contract.runtimeImage $legacy.runtimeImage) (ne $contract.sourceCommit $legacy.sourceCommit) (not (kindIs "string" $contract.agentProfile)) (eq $contract.agentProfile "") (not (kindIs "string" $contract.gatewayContractDigest)) (not (regexMatch "^[0-9a-f]{64}$" $contract.gatewayContractDigest)) (not (kindIs "string" $contract.commandFingerprint)) (not (regexMatch "^[0-9a-f]{64}$" $contract.commandFingerprint)) (not (kindIs "string" $contract.schemaDigest)) (not (regexMatch "^[0-9a-f]{64}$" $contract.schemaDigest)) -}}
{{- fail "deployment lock legacy contract is invalid" -}}
{{- end -}}
{{- end -}}
{{- $rollback := $lock.rollback -}}
{{- $rollbackKeys := list "provisionerImage" "provisionerSourceCommit" "v1CorpusSha256" "legacyManifestSha256" "substrateV1ConsumerCommit" -}}
{{- if or (not (kindIs "map" $rollback)) (ne (len $rollback) (len $rollbackKeys)) -}}
{{- fail "deployment lock rollback is invalid" -}}
{{- end -}}
{{- range $key := $rollbackKeys -}}
{{- if not (hasKey $rollback $key) -}}
{{- fail (printf "deployment lock rollback is missing %s" $key) -}}
{{- end -}}
{{- end -}}
{{- if or (not (kindIs "string" $rollback.provisionerImage)) (not (regexMatch "^ghcr\\.io/artexis10/exomem-provisioner@sha256:[0-9a-f]{64}$" $rollback.provisionerImage)) (not (kindIs "string" $rollback.provisionerSourceCommit)) (not (regexMatch "^[0-9a-f]{40}$" $rollback.provisionerSourceCommit)) (not (kindIs "string" $rollback.v1CorpusSha256)) (not (regexMatch "^[0-9a-f]{64}$" $rollback.v1CorpusSha256)) (not (kindIs "string" $rollback.legacyManifestSha256)) (not (regexMatch "^[0-9a-f]{64}$" $rollback.legacyManifestSha256)) (not (kindIs "string" $rollback.substrateV1ConsumerCommit)) (not (regexMatch "^[0-9a-f]{40}$" $rollback.substrateV1ConsumerCommit)) -}}
{{- fail "deployment lock rollback is invalid" -}}
{{- end -}}
{{- if eq (toJson $lock.schemaVersion) "3" -}}
{{- $records := $lock.recordsCompatibility -}}
{{- $recordKeys := list "minimum_records_reader_version" "activeProfile" "activeLifecycleActionsEnabled" "rollbackProfile" "rollbackLifecycleActionsEnabled" "rollbackRuntime" -}}
{{- if or (not (kindIs "map" $records)) (ne (len $records) (len $recordKeys)) -}}
{{- fail "deployment lock Records compatibility is invalid" -}}
{{- end -}}
{{- range $key := $recordKeys -}}
{{- if not (hasKey $records $key) -}}
{{- fail (printf "deployment lock Records compatibility is missing %s" $key) -}}
{{- end -}}
{{- end -}}
{{- if or (ne (toJson $records.minimum_records_reader_version) "2") (ne $records.activeProfile "hosted-alpha-agent-v2") (ne (toJson $records.activeLifecycleActionsEnabled) "true") (ne $records.rollbackProfile "hosted-alpha-agent-v1") (ne (toJson $records.rollbackLifecycleActionsEnabled) "false") (ne $target.agentProfile $records.activeProfile) -}}
{{- fail "deployment lock Records compatibility is invalid" -}}
{{- end -}}
{{- $rollbackRuntime := $records.rollbackRuntime -}}
{{- if or (not (kindIs "map" $rollbackRuntime)) (ne (len $rollbackRuntime) 6) (not (regexMatch "^ghcr\\.io/artexis10/exomem@sha256:[0-9a-f]{64}$" $rollbackRuntime.image)) (not (regexMatch "^[0-9a-f]{40}$" $rollbackRuntime.sourceCommit)) (not (regexMatch "^[0-9a-f]{64}$" $rollbackRuntime.candidateSha256)) (ne (toJson $rollbackRuntime.recordsReaderVersion) "2") -}}
{{- fail "deployment lock Records rollback runtime is invalid" -}}
{{- end -}}
{{- $rollbackTarget := $rollbackRuntime.runtimeTarget -}}
{{- if or (not (kindIs "map" $rollbackTarget)) (ne (len $rollbackTarget) (len $targetKeys)) -}}
{{- fail "deployment lock Records rollback runtime target is invalid" -}}
{{- end -}}
{{- range $key := $targetKeys -}}
{{- if not (hasKey $rollbackTarget $key) -}}
{{- fail (printf "deployment lock Records rollback runtime target is missing %s" $key) -}}
{{- end -}}
{{- end -}}
{{- if or (not (kindIs "string" $rollbackTarget.releaseVersion)) (eq $rollbackTarget.releaseVersion "") (not (kindIs "string" $rollbackTarget.protocolVersion)) (eq $rollbackTarget.protocolVersion "") (not (kindIs "string" $rollbackTarget.agentProfile)) (ne $rollbackTarget.agentProfile $records.rollbackProfile) (not (kindIs "string" $rollbackTarget.gatewayContractDigest)) (not (regexMatch "^[0-9a-f]{64}$" $rollbackTarget.gatewayContractDigest)) (not (kindIs "string" $rollbackTarget.commandFingerprint)) (not (regexMatch "^[0-9a-f]{64}$" $rollbackTarget.commandFingerprint)) (not (kindIs "string" $rollbackTarget.schemaDigest)) (not (regexMatch "^[0-9a-f]{64}$" $rollbackTarget.schemaDigest)) -}}
{{- fail "deployment lock Records rollback runtime target is invalid" -}}
{{- end -}}
{{- $proof := $rollbackRuntime.readerStatusProof -}}
{{- if or (not (kindIs "map" $proof)) (ne (len $proof) 7) (ne $proof.profile $records.rollbackProfile) (ne (toJson $proof.recordsReaderVersion) "2") (ne (toJson $proof.lifecycleActionsEnabled) "false") (not (regexMatch "^\\d{4}-\\d{2}-\\d{2}T\\d{2}:\\d{2}:\\d{2}Z$" $proof.issuedAt)) (not (regexMatch "^\\d{4}-\\d{2}-\\d{2}T\\d{2}:\\d{2}:\\d{2}Z$" $proof.expiresAt)) (ne $proof.signerWorkflow "Artexis10/exomem/.github/workflows/release-please.yml") (not (regexMatch "^[0-9a-f]{40}$" $proof.signerWorkflowDigest)) -}}
{{- fail "deployment lock Records rollback proof is invalid" -}}
{{- end -}}
{{- end -}}
{{- $raw -}}
{{- end -}}

{{- define "exomem.hostedRuntimeSelection" -}}
{{- $lock := include "exomem.hostedDeploymentLock" . | mustFromJson -}}
{{- $selection := .Values.provisioner.runtimeSelection -}}
{{- if eq (toJson $lock.schemaVersion) "3" -}}
{{- if not (has $selection (list "active" "rollback")) -}}
{{- fail "deployment lock v3 requires runtimeSelection active or rollback" -}}
{{- end -}}
{{- $selection -}}
{{- else -}}
{{- if eq $selection "rollback" -}}
{{- fail "deployment lock v2 does not support rollback runtimeSelection" -}}
{{- end -}}
{{- if not (has $selection (list "" "active")) -}}
{{- fail "deployment lock runtimeSelection is invalid" -}}
{{- end -}}
active
{{- end -}}
{{- end -}}

{{- define "exomem.hostedDeploymentLock" -}}
{{- include "exomem.hostedDeploymentLockJson" . | mustFromJson | toJson -}}
{{- end -}}

{{- define "exomem.hostedRuntimeImage" -}}
{{- $lock := include "exomem.hostedDeploymentLock" . | mustFromJson -}}
{{- if eq (include "exomem.hostedRuntimeSelection" .) "rollback" -}}
{{- $lock.recordsCompatibility.rollbackRuntime.image -}}
{{- else -}}
{{- $lock.components.runtime.image -}}
{{- end -}}
{{- end -}}

{{- define "exomem.hostedRuntimeImages" -}}
{{- $lock := include "exomem.hostedDeploymentLock" . | mustFromJson -}}
{{- $images := list $lock.components.runtime.image -}}
{{- if eq (toJson $lock.schemaVersion) "3" -}}
{{- $images = append $images $lock.recordsCompatibility.rollbackRuntime.image -}}
{{- end -}}
{{- range $legacy := $lock.composition.legacyCatalog -}}
{{- $images = append $images $legacy.runtimeImage -}}
{{- end -}}
{{- $images | uniq | toJson -}}
{{- end -}}

{{- define "exomem.hostedProvisionerImage" -}}
{{- $lock := include "exomem.hostedDeploymentLock" . | mustFromJson -}}
{{- $lock.components.provisioner.image -}}
{{- end -}}

{{- define "exomem.hostedDeploymentLockName" -}}
{{- $lock := include "exomem.hostedDeploymentLock" . | mustFromJson -}}
{{- printf "exomem-hosted-deployment-lock-v%d-%s" ($lock.schemaVersion | int) (trunc 16 .Values.provisioner.deploymentLockSha256) -}}
{{- end -}}

{{- define "exomem.hostedDeploymentLockFileName" -}}
{{- $lock := include "exomem.hostedDeploymentLock" . | mustFromJson -}}
{{- printf "exomem-hosted-deployment-lock-v%d.json" ($lock.schemaVersion | int) -}}
{{- end -}}
