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
{{- $values := list
  .Values.providerRecoveryEnvelopes.namespace
  .Values.providerRecoveryEnvelopes.vaultPvc
  .Values.providerRecoveryEnvelopes.credentialSecret
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
{{- if ne (len (uniq $values)) 16 -}}
{{- fail "provider recovery envelopes must be unique per exact object" -}}
{{- end -}}
{{- end -}}

{{- define "exomem-cell.selectorLabels" -}}
app.kubernetes.io/name: exomem-cell
exomem.io/cell: {{ .Values.resourceName }}
{{- end -}}
