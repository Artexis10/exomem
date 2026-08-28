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
{{- if ne (len (uniq $values)) 17 -}}
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
