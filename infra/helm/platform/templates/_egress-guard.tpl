{{/*
Fail the render unless cells.jobEgressExcept denies every private range. A feature
that opens public HTTPS egress calls this, so one list keeps its denial in step with
the artifact broker's. Arguments: dict "root" <chart root> "feature" <name for the message>.
*/}}
{{- define "exomem.requirePrivateRangesDenied" -}}
{{- range $cidr := list "10.0.0.0/8" "172.16.0.0/12" "192.168.0.0/16" "100.64.0.0/10" "169.254.0.0/16" }}
{{- if not (has $cidr $.root.Values.cells.jobEgressExcept) }}
{{- fail (printf "%s requires cells.jobEgressExcept to include %s" $.feature $cidr) }}
{{- end }}
{{- end }}
{{- end -}}
