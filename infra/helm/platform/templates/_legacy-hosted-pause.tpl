{{- define "exomem.legacyHostedPaused" -}}
{{- if or .Values.legacyHosted.paused .Values.cellctl.enabled .Values.cloudGateway.enabled -}}true{{- else -}}false{{- end -}}
{{- end -}}
