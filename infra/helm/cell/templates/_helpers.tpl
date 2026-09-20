{{- define "exomem-cell.labels" -}}
app.kubernetes.io/name: exomem-cell
app.kubernetes.io/instance: {{ .Values.resourceName }}
app.kubernetes.io/part-of: exomem-hosted
exomem.io/cell: {{ .Values.resourceName }}
{{- end -}}

{{- define "exomem-cell.providerAnnotations" -}}
exomem.io/tenant-id: {{ .Values.providerIdentity.tenantId | quote }}
exomem.io/cell-id: {{ .Values.providerIdentity.cellId | quote }}
exomem.io/operation-id: {{ .Values.providerIdentity.operationId | quote }}
exomem.io/tenant-digest: {{ .Values.providerIdentity.tenantDigest | quote }}
exomem.io/subject-digest: {{ .Values.providerIdentity.subjectDigest | quote }}
exomem.io/operation-digest: {{ .Values.providerIdentity.operationDigest | quote }}
exomem.io/fence: {{ .Values.providerIdentity.fence | quote }}
{{- end -}}

{{- define "exomem-cell.providerAnnotationsFor" -}}
{{ include "exomem-cell.providerAnnotations" .root }}
exomem.io/recovery-envelope: {{ required (printf "providerRecoveryEnvelopes.%s is required" .key) (index .root.Values.providerRecoveryEnvelopes .key) | quote }}
{{- end -}}

{{- define "exomem-cell.validateProviderRecovery" -}}
{{- if ne .Values.providerIdentity.cellId .Values.cellId -}}
{{- fail "providerIdentity.cellId must equal cellId" -}}
{{- end -}}
{{- $values := list
  .Values.providerRecoveryEnvelopes.namespace
  .Values.providerRecoveryEnvelopes.vaultPvc
  .Values.providerRecoveryEnvelopes.credentialSecret
  .Values.providerRecoveryEnvelopes.authorizationSessionSecret
  .Values.providerRecoveryEnvelopes.serviceAccount
  .Values.providerRecoveryEnvelopes.initRequestConfigMap
  .Values.providerRecoveryEnvelopes.providerOperationConfigMap
  .Values.providerRecoveryEnvelopes.initJob
  .Values.providerRecoveryEnvelopes.defaultDenyNetworkPolicy
  .Values.providerRecoveryEnvelopes.traefikIngressNetworkPolicy
  .Values.providerRecoveryEnvelopes.resourceQuota
  .Values.providerRecoveryEnvelopes.limitRange
  .Values.providerRecoveryEnvelopes.service
  .Values.providerRecoveryEnvelopes.statefulSet
  .Values.providerRecoveryEnvelopes.stripCellMiddleware
  .Values.providerRecoveryEnvelopes.controlIngressRoute
  .Values.providerRecoveryEnvelopes.transferIngressRoute
-}}
{{- $ack := include "exomem-cell.activationAckEnabled" . -}}
{{- $count := 17 -}}
{{- if eq $ack "true" -}}
{{- $values = concat $values (list (required "activationAckTrustConfigMap is required" .Values.providerRecoveryEnvelopes.activationAckTrustConfigMap) (required "activationAckEgressNetworkPolicy is required" .Values.providerRecoveryEnvelopes.activationAckEgressNetworkPolicy)) -}}
{{- $count = 19 -}}
{{- else if or (hasKey .Values.providerRecoveryEnvelopes "activationAckTrustConfigMap") (hasKey .Values.providerRecoveryEnvelopes "activationAckEgressNetworkPolicy") -}}
{{- fail "legacy cells cannot carry activation acknowledgement envelopes" -}}
{{- end -}}
{{- if ne (len (uniq $values)) $count -}}
{{- fail "provider recovery envelopes must be unique per exact object" -}}
{{- end -}}
{{- end -}}

{{- define "exomem-cell.selectorLabels" -}}
app.kubernetes.io/name: exomem-cell
exomem.io/cell: {{ .Values.resourceName }}
{{- end -}}

{{/*
Fail closed on a cell that would serve keyword-only recall.

hosted_runtime.py computes `workers_enabled = worker_count > 0` and SETS
EXOMEM_DISABLE_EMBEDDINGS whenever that is false or the grant is absent. Both
failures are silent: the cell starts, accepts writes, answers queries, and
simply never matches on meaning. A tenant paying for semantic recall would get
a strictly lesser product than the free local runtime with no error anywhere.
Render-time is the last place to catch it, so catch it here.
*/}}
{{- define "exomem-cell.validateProductSurface" -}}
{{- $workers := int .Values.workerLimit -}}
{{- if lt $workers 1 -}}
{{- fail "workerLimit must be greater than zero: a zero worker limit disables embeddings and ships keyword-only recall" -}}
{{- end -}}
{{- $grants := splitList "," (.Values.featureGrants | default "") -}}
{{- if not (has "embeddings" $grants) -}}
{{- fail "featureGrants must include embeddings: without it the cell silently serves keyword-only recall" -}}
{{- end -}}
{{- end -}}

{{- define "exomem-cell.activationAckEnabled" -}}
{{- $ack := .Values.activationAcknowledgement -}}
{{- if or $ack.protocol $ack.platformNamespace $ack.trustBundleSha256 $ack.trustBundlePem -}}
{{- if or (ne $ack.protocol "exomem.hosted-activation-ack/v1") (not $ack.platformNamespace) (not $ack.trustBundleSha256) (not $ack.trustBundlePem) -}}
{{- fail "activationAcknowledgement requires the complete supported binding" -}}
{{- end -}}
{{- if or (ne (sha256sum $ack.trustBundlePem) $ack.trustBundleSha256) (gt (len $ack.trustBundlePem) 65536) (contains "PRIVATE KEY" $ack.trustBundlePem) -}}
{{- fail "activation acknowledgement trust must match its exact bounded public digest" -}}
{{- end -}}
{{- $certs := regexFindAll "-----BEGIN CERTIFICATE-----[A-Za-z0-9+/=\\r\\n]+-----END CERTIFICATE-----" $ack.trustBundlePem -1 -}}
{{- $remainder := $ack.trustBundlePem -}}
{{- range $certs -}}{{- $remainder = replace . "" $remainder -}}{{- end -}}
{{- if or (eq (len $certs) 0) (ne (trim $remainder) "") -}}
{{- fail "activation acknowledgement trust must contain only PEM certificates" -}}
{{- end -}}
true
{{- else -}}
false
{{- end -}}
{{- end -}}

{{- define "exomem-cell.activationAckEnv" -}}
- name: EXOMEM_HOSTED_ACTIVATION_ACK_PROTOCOL
  value: {{ .Values.activationAcknowledgement.protocol | quote }}
- name: EXOMEM_HOSTED_ACTIVATION_ACK_SOCKET
  value: /run/exomem/activation-ack/ack.sock
{{- end -}}
