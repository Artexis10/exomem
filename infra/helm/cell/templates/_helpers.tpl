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

{{- define "exomem-cell.selectorLabels" -}}
app.kubernetes.io/name: exomem-cell
exomem.io/cell: {{ .Values.resourceName }}
{{- end -}}
