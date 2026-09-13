{{/* The same binding-only Pod shape is checked at Job admission and controller Pod admission. */}}
{{- define "exomem.storageBindingPodAdmission" -}}
- expression: >-
    !variables.storageBinding ||
    (variables.bindingMeta.labels['app.kubernetes.io/name'] == 'exomem-cell' &&
      variables.bindingMeta.labels['exomem.io/cell'] == request.namespace &&
      variables.bindingMeta.labels['exomem.io/storage-init'] == 'true' &&
      variables.bindingMeta.labels['exomem.io/storage-binding'] == 'true' &&
      variables.bindingMeta.labels.all(key, key in [
        'app.kubernetes.io/name', 'exomem.io/cell', 'exomem.io/storage-init',
        'exomem.io/storage-binding', 'job-name', 'controller-uid',
        'batch.kubernetes.io/job-name', 'batch.kubernetes.io/controller-uid']) &&
      (!has(variables.bindingMeta.annotations) || size(variables.bindingMeta.annotations) == 0) &&
      variables.bindingSpec.runtimeClassName == 'exomem-storage-init' &&
      variables.bindingSpec.serviceAccountName == request.namespace &&
      variables.bindingSpec.automountServiceAccountToken == false &&
      variables.bindingSpec.restartPolicy == 'Never' &&
      variables.bindingSpec.securityContext.seccompProfile.type == 'RuntimeDefault' &&
      !has(variables.bindingSpec.securityContext.fsGroup) &&
      !has(variables.bindingSpec.securityContext.runAsUser) &&
      !has(variables.bindingSpec.securityContext.runAsGroup) &&
      !has(variables.bindingSpec.securityContext.supplementalGroups) &&
      !has(variables.bindingSpec.securityContext.sysctls) &&
      (!has(variables.bindingSpec.hostNetwork) || variables.bindingSpec.hostNetwork == false) &&
      (!has(variables.bindingSpec.hostPID) || variables.bindingSpec.hostPID == false) &&
      (!has(variables.bindingSpec.hostIPC) || variables.bindingSpec.hostIPC == false) &&
      !has(variables.bindingSpec.hostAliases) && !has(variables.bindingSpec.nodeSelector) &&
      !has(variables.bindingSpec.affinity) &&
      (!has(variables.bindingSpec.schedulerName) ||
        variables.bindingSpec.schedulerName == 'default-scheduler') &&
      !has(variables.bindingSpec.topologySpreadConstraints) &&
      (!has(variables.bindingSpec.priorityClassName) || variables.bindingSpec.priorityClassName == '') &&
      (!has(variables.bindingSpec.priority) || variables.bindingSpec.priority == 0) &&
      (!has(variables.bindingSpec.preemptionPolicy) ||
        variables.bindingSpec.preemptionPolicy == 'PreemptLowerPriority') &&
      (!has(variables.bindingSpec.tolerations) ||
        variables.bindingSpec.tolerations.all(t,
          t.key in ['node.kubernetes.io/not-ready', 'node.kubernetes.io/unreachable'] &&
          t.operator == 'Exists' && t.effect == 'NoExecute' &&
          t.tolerationSeconds == 300)) &&
      !has(variables.bindingSpec.imagePullSecrets) &&
      !has(variables.bindingSpec.shareProcessNamespace) &&
      (!has(variables.bindingSpec.initContainers) || size(variables.bindingSpec.initContainers) == 0) &&
      (!has(variables.bindingSpec.ephemeralContainers) || size(variables.bindingSpec.ephemeralContainers) == 0) &&
      size(variables.bindingSpec.containers) == 1 &&
      variables.bindingSpec.containers[0].name == 'exomem' &&
      variables.bindingSpec.containers[0].image in {{ include "exomem.hostedRuntimeImages" . | mustFromJson | toJson }} &&
      variables.bindingSpec.containers[0].imagePullPolicy == 'IfNotPresent' &&
      variables.bindingSpec.containers[0].command == ['/bin/true'] &&
      (!has(variables.bindingSpec.containers[0].args) || size(variables.bindingSpec.containers[0].args) == 0) &&
      (!has(variables.bindingSpec.containers[0].env) || size(variables.bindingSpec.containers[0].env) == 0) &&
      (!has(variables.bindingSpec.containers[0].envFrom) || size(variables.bindingSpec.containers[0].envFrom) == 0) &&
      (!has(variables.bindingSpec.containers[0].volumeMounts) || size(variables.bindingSpec.containers[0].volumeMounts) == 0) &&
      (!has(variables.bindingSpec.containers[0].volumeDevices) || size(variables.bindingSpec.containers[0].volumeDevices) == 0) &&
      (!has(variables.bindingSpec.containers[0].ports) || size(variables.bindingSpec.containers[0].ports) == 0) &&
      !has(variables.bindingSpec.containers[0].lifecycle) &&
      !has(variables.bindingSpec.containers[0].livenessProbe) &&
      !has(variables.bindingSpec.containers[0].readinessProbe) &&
      !has(variables.bindingSpec.containers[0].startupProbe) &&
      variables.bindingSpec.containers[0].terminationMessagePath == '/dev/termination-log' &&
      variables.bindingSpec.containers[0].terminationMessagePolicy == 'File' &&
      variables.bindingSpec.containers[0].securityContext.runAsNonRoot == true &&
      variables.bindingSpec.containers[0].securityContext.runAsUser == 10001 &&
      variables.bindingSpec.containers[0].securityContext.runAsGroup == 10001 &&
      variables.bindingSpec.containers[0].securityContext.allowPrivilegeEscalation == false &&
      variables.bindingSpec.containers[0].securityContext.readOnlyRootFilesystem == true &&
      (!has(variables.bindingSpec.containers[0].securityContext.privileged) ||
        variables.bindingSpec.containers[0].securityContext.privileged == false) &&
      variables.bindingSpec.containers[0].securityContext.capabilities.drop == ['ALL'] &&
      (!has(variables.bindingSpec.containers[0].securityContext.capabilities.add) ||
        size(variables.bindingSpec.containers[0].securityContext.capabilities.add) == 0) &&
      string(dyn(variables.bindingSpec.containers[0].resources).requests['cpu']) == '10m' &&
      string(dyn(variables.bindingSpec.containers[0].resources).requests['memory']) == '16Mi' &&
      string(dyn(variables.bindingSpec.containers[0].resources).limits['cpu']) == '100m' &&
      string(dyn(variables.bindingSpec.containers[0].resources).limits['memory']) == '64Mi' &&
      size(dyn(variables.bindingSpec.containers[0].resources).requests) == 2 &&
      size(dyn(variables.bindingSpec.containers[0].resources).limits) == 2 &&
      size(variables.bindingSpec.volumes) == 1 &&
      variables.bindingSpec.volumes[0].name == 'data' &&
      variables.bindingSpec.volumes[0].persistentVolumeClaim.claimName == request.namespace + '-data' &&
      (!has(variables.bindingSpec.volumes[0].persistentVolumeClaim.readOnly) ||
        variables.bindingSpec.volumes[0].persistentVolumeClaim.readOnly == false))
  message: Storage binding must be the exact inert unmounted first-consumer Pod.
{{- end -}}
