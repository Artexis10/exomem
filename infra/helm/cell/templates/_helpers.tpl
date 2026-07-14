{{- define "exomem-cell.labels" -}}
app.kubernetes.io/name: exomem-cell
app.kubernetes.io/instance: {{ .Values.resourceName }}
app.kubernetes.io/part-of: exomem-hosted
exomem.io/cell: {{ .Values.resourceName }}
{{- end -}}

{{- define "exomem-cell.selectorLabels" -}}
app.kubernetes.io/name: exomem-cell
exomem.io/cell: {{ .Values.resourceName }}
{{- end -}}
