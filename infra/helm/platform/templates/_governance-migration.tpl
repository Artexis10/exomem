{{/* One closed pod contract, evaluated at both provisioner Job and tenant Pod admission. */}}
{{- define "exomem.governanceMigrationAdmission" -}}
{{- $images := include "exomem.hostedRuntimeImages" . | mustFromJson -}}
- expression: >-
    !variables.governanceMigration ||
    (variables.migrationMeta.labels['app.kubernetes.io/name'] == 'exomem-governance-migration' &&
      variables.migrationMeta.labels['exomem.io/cell'] == request.namespace &&
      variables.migrationMeta.labels['exomem.io/governance-migration'] == 'true' &&
      !('exomem.io/storage-init' in variables.migrationMeta.labels) &&
      !('exomem.io/vault-fingerprint' in variables.migrationMeta.labels) &&
      !('exomem.io/restore-candidate' in variables.migrationMeta.labels) &&
      variables.migrationMeta.annotations['exomem.io/governance-migration-phase'] in ['inspect', 'prepare', 'commit'] &&
      variables.migrationMeta.annotations['exomem.io/governance-migration-request'].matches('^[a-f0-9]{64}$') &&
      variables.migrationMeta.annotations['exomem.io/governance-migration-pvc'].matches('^[A-Za-z0-9][A-Za-z0-9._:-]{0,511}$') &&
      variables.migrationMeta.annotations['exomem.io/governance-migration-image'] == variables.migrationSpec.containers[0].image &&
      variables.migrationMeta.annotations['exomem.io/recovery-envelope'].size() > 0 &&
      variables.migrationMeta.annotations['exomem.io/recovery-envelope'].size() <= 32768 &&
      variables.migrationMeta.annotations['exomem.io/fence'].matches('^[1-9][0-9]{0,15}$') &&
      variables.migrationSpec.serviceAccountName == request.namespace &&
      variables.migrationSpec.automountServiceAccountToken == false &&
      variables.migrationSpec.restartPolicy == 'Never' &&
      !has(variables.migrationSpec.runtimeClassName) &&
      (!has(variables.migrationSpec.hostNetwork) || variables.migrationSpec.hostNetwork == false) &&
      (!has(variables.migrationSpec.hostPID) || variables.migrationSpec.hostPID == false) &&
      (!has(variables.migrationSpec.hostIPC) || variables.migrationSpec.hostIPC == false) &&
      !has(variables.migrationSpec.hostAliases) && !has(variables.migrationSpec.ephemeralContainers) &&
      !has(variables.migrationSpec.nodeSelector) && !has(variables.migrationSpec.affinity) &&
      !has(variables.migrationSpec.shareProcessNamespace) &&
      !has(variables.migrationSpec.imagePullSecrets) && !has(variables.migrationSpec.runtimeClassName) &&
      variables.migrationSpec.securityContext.seccompProfile.type == 'RuntimeDefault' &&
      !has(variables.migrationSpec.securityContext.fsGroup) &&
      !has(variables.migrationSpec.securityContext.runAsUser) &&
      !has(variables.migrationSpec.securityContext.runAsGroup) &&
      !has(variables.migrationSpec.securityContext.sysctls) &&
      !has(variables.migrationSpec.securityContext.supplementalGroups) &&
      size(variables.migrationSpec.containers) == 1 && size(variables.migrationSpec.initContainers) == 1)
  message: Governance migration is restricted to the exact tenant identity and offline pod shape.
{{- range $name := list "containers" "initContainers" }}
- expression: >-
    !variables.governanceMigration ||
    variables.migrationSpec.{{ $name }}.all(c,
      c.image in {{ $images | toJson }} && c.image == variables.migrationSpec.containers[0].image &&
      c.imagePullPolicy == 'IfNotPresent' && c.command == ['python'] &&
      c.terminationMessagePath == '/dev/termination-log' && c.terminationMessagePolicy == 'File' &&
      c.securityContext.runAsNonRoot == true && c.securityContext.runAsUser == 10001 &&
      c.securityContext.runAsGroup == 10001 && c.securityContext.allowPrivilegeEscalation == false &&
      c.securityContext.readOnlyRootFilesystem == true && c.securityContext.capabilities.drop == ['ALL'] &&
      (!has(c.securityContext.capabilities.add) || size(c.securityContext.capabilities.add) == 0) &&
      (!has(c.securityContext.privileged) || c.securityContext.privileged == false) &&
      !has(c.securityContext.procMount) && !has(c.securityContext.seLinuxOptions) &&
      !has(c.securityContext.windowsOptions) && !has(c.securityContext.appArmorProfile) &&
      !has(c.securityContext.seccompProfile) && !has(c.lifecycle) && !has(c.livenessProbe) &&
      !has(c.readinessProbe) && !has(c.startupProbe) && !has(c.restartPolicy) &&
      (!has(c.envFrom) || size(c.envFrom) == 0) && (!has(c.ports) || size(c.ports) == 0) &&
      (!has(c.volumeDevices) || size(c.volumeDevices) == 0) && !has(c.resizePolicy) &&
      (!has(c.workingDir) || c.workingDir == '') && (!has(c.stdin) || c.stdin == false) &&
      (!has(c.stdinOnce) || c.stdinOnce == false) && (!has(c.tty) || c.tty == false) &&
      size(dyn(c.resources).requests) == 3 && size(dyn(c.resources).limits) == 3 &&
      c.volumeMounts.all(m, !has(m.subPathExpr) && !has(m.mountPropagation) && !has(m.recursiveReadOnly)))
  message: Governance migration containers cannot add executable, interactive or privileged surfaces.
{{- end }}
- expression: >-
    !variables.governanceMigration ||
    (variables.migrationSpec.containers[0].name == 'exomem' &&
      variables.migrationSpec.containers[0].args == ['-m', 'exomem.hosted_governance_job'] &&
      variables.migrationSpec.initContainers[0].name == 'authorization-session-custody' &&
      variables.migrationSpec.initContainers[0].args == ['-m', 'exomem.governance.authorization_hosted_mount'] &&
      (!has(variables.migrationSpec.initContainers[0].env) || size(variables.migrationSpec.initContainers[0].env) == 0) &&
      size(variables.migrationSpec.containers[0].env) == 10 &&
      variables.migrationSpec.containers[0].env.all(e, has(e.value) && !has(e.valueFrom)) &&
      variables.migrationSpec.containers[0].env[0].name == 'EXOMEM_HOSTED_CELL' &&
      variables.migrationSpec.containers[0].env[0].value == '1' &&
      variables.migrationSpec.containers[0].env[1].name == 'EXOMEM_VAULT_PATH' &&
      variables.migrationSpec.containers[0].env[1].value == '/var/lib/exomem/vault' &&
      variables.migrationSpec.containers[0].env[2].name == 'EXOMEM_HOSTED_STATE_ROOT' &&
      variables.migrationSpec.containers[0].env[2].value == '/var/lib/exomem/state' &&
      variables.migrationSpec.containers[0].env[3].name == 'EXOMEM_STATE_ROOT' &&
      variables.migrationSpec.containers[0].env[3].value == '/var/lib/exomem/state/vault-state' &&
      variables.migrationSpec.containers[0].env[4].name == 'EXOMEM_WRITER_LEASE_STATE_DIR' &&
      variables.migrationSpec.containers[0].env[4].value == '/var/lib/exomem/state' &&
      variables.migrationSpec.containers[0].env[5].name == 'EXOMEM_AUTH_SESSION_KEYRING_FILE' &&
      variables.migrationSpec.containers[0].env[5].value == '/run/exomem/authorization-session/private/keyring.json' &&
      variables.migrationSpec.containers[0].env[6].name == 'EXOMEM_AUTH_SESSION_CONTROL_FILE' &&
      variables.migrationSpec.containers[0].env[6].value == '/run/exomem/authorization-session/private/control.json' &&
      variables.migrationSpec.containers[0].env[7].name == 'EXOMEM_AUTH_SESSION_MEMBERSHIP_FILE' &&
      variables.migrationSpec.containers[0].env[7].value == '/run/exomem/authorization-session/private/serving-membership.json' &&
      variables.migrationSpec.containers[0].env[8].name == 'EXOMEM_AUTH_SESSION_REPLICA_ID' &&
      variables.migrationSpec.containers[0].env[8].value == request.namespace + '-0' &&
      variables.migrationSpec.containers[0].env[9].name == 'EXOMEM_GOVERNANCE_MIGRATION_REQUEST' &&
      variables.migrationSpec.containers[0].env[9].value.size() > 0 &&
      variables.migrationSpec.containers[0].env[9].value.size() <= 8192 &&
      variables.migrationSpec.containers[0].env[9].value.matches('^[ -~]+$') &&
      string(dyn(variables.migrationSpec.containers[0].resources).requests['cpu']) == '100m' &&
      string(dyn(variables.migrationSpec.containers[0].resources).requests['memory']) == '128Mi' &&
      string(dyn(variables.migrationSpec.containers[0].resources).requests['ephemeral-storage']) == '64Mi' &&
      string(dyn(variables.migrationSpec.containers[0].resources).limits['cpu']) == '1' &&
      string(dyn(variables.migrationSpec.containers[0].resources).limits['memory']) == '1Gi' &&
      string(dyn(variables.migrationSpec.containers[0].resources).limits['ephemeral-storage']) == '64Mi' &&
      string(dyn(variables.migrationSpec.initContainers[0].resources).requests['cpu']) == '10m' &&
      string(dyn(variables.migrationSpec.initContainers[0].resources).requests['memory']) == '16Mi' &&
      string(dyn(variables.migrationSpec.initContainers[0].resources).requests['ephemeral-storage']) == '16Mi' &&
      string(dyn(variables.migrationSpec.initContainers[0].resources).limits['cpu']) == '100m' &&
      string(dyn(variables.migrationSpec.initContainers[0].resources).limits['memory']) == '64Mi' &&
      string(dyn(variables.migrationSpec.initContainers[0].resources).limits['ephemeral-storage']) == '16Mi')
  message: Governance migration uses only the pinned runner, custody materializer and bounded literal environment.
- expression: >-
    !variables.governanceMigration ||
    (size(variables.migrationSpec.volumes) == 4 &&
      variables.migrationSpec.volumes[0].name == 'data' &&
      variables.migrationSpec.volumes[0].persistentVolumeClaim.claimName == request.namespace + '-data' &&
      (!has(variables.migrationSpec.volumes[0].persistentVolumeClaim.readOnly) || variables.migrationSpec.volumes[0].persistentVolumeClaim.readOnly == false) &&
      variables.migrationSpec.volumes[1].name == 'authorization-session-source' &&
      variables.migrationSpec.volumes[1].secret.secretName == 'exomem-authorization-session' &&
      variables.migrationSpec.volumes[1].secret.defaultMode == 292 &&
      !has(variables.migrationSpec.volumes[1].secret.items) &&
      (!has(variables.migrationSpec.volumes[1].secret.optional) || variables.migrationSpec.volumes[1].secret.optional == false) &&
      variables.migrationSpec.volumes[2].name == 'authorization-session-custody' &&
      variables.migrationSpec.volumes[2].emptyDir.medium == 'Memory' &&
      string(dyn(variables.migrationSpec.volumes[2].emptyDir).sizeLimit) == '256Ki' &&
      variables.migrationSpec.volumes[3].name == 'tmp' &&
      (!has(variables.migrationSpec.volumes[3].emptyDir.medium) || variables.migrationSpec.volumes[3].emptyDir.medium == '') &&
      string(dyn(variables.migrationSpec.volumes[3].emptyDir).sizeLimit) == '64Mi' &&
      size(variables.migrationSpec.initContainers[0].volumeMounts) == 2 &&
      variables.migrationSpec.initContainers[0].volumeMounts[0].name == 'authorization-session-source' &&
      variables.migrationSpec.initContainers[0].volumeMounts[0].mountPath == '/run/exomem/authorization-session-source' &&
      variables.migrationSpec.initContainers[0].volumeMounts[0].readOnly == true &&
      variables.migrationSpec.initContainers[0].volumeMounts[1].name == 'authorization-session-custody' &&
      variables.migrationSpec.initContainers[0].volumeMounts[1].mountPath == '/run/exomem/authorization-session' &&
      (!has(variables.migrationSpec.initContainers[0].volumeMounts[1].readOnly) || variables.migrationSpec.initContainers[0].volumeMounts[1].readOnly == false) &&
      variables.migrationSpec.initContainers[0].volumeMounts.all(m, !has(m.subPath)) &&
      size(variables.migrationSpec.containers[0].volumeMounts) == 5 &&
      {{ range $i, $path := list "vault" "state" "logs" -}}
      variables.migrationSpec.containers[0].volumeMounts[{{ $i }}].name == 'data' &&
      variables.migrationSpec.containers[0].volumeMounts[{{ $i }}].mountPath == '/var/lib/exomem/{{ $path }}' &&
      variables.migrationSpec.containers[0].volumeMounts[{{ $i }}].subPath == '{{ $path }}' &&
      ('{{ $path }}' == 'logs'
        ? variables.migrationSpec.containers[0].volumeMounts[{{ $i }}].readOnly == true
        : (!has(variables.migrationSpec.containers[0].volumeMounts[{{ $i }}].readOnly) || variables.migrationSpec.containers[0].volumeMounts[{{ $i }}].readOnly == false)) &&
      {{ end -}}
      variables.migrationSpec.containers[0].volumeMounts[3].name == 'authorization-session-custody' &&
      variables.migrationSpec.containers[0].volumeMounts[3].mountPath == '/run/exomem/authorization-session' &&
      variables.migrationSpec.containers[0].volumeMounts[3].readOnly == true &&
      !has(variables.migrationSpec.containers[0].volumeMounts[3].subPath) &&
      variables.migrationSpec.containers[0].volumeMounts[4].name == 'tmp' &&
      variables.migrationSpec.containers[0].volumeMounts[4].mountPath == '/tmp' &&
      (!has(variables.migrationSpec.containers[0].volumeMounts[4].readOnly) || variables.migrationSpec.containers[0].volumeMounts[4].readOnly == false) &&
      !has(variables.migrationSpec.containers[0].volumeMounts[4].subPath))
  message: Governance migration mounts only fixed PVC roots read-write and read-only custody and logs.
{{- end -}}
