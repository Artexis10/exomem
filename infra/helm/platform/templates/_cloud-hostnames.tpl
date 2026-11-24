{{/*
The Traefik host matcher that every Cloud IngressRoute rule starts with. With no
cloudGateway.additionalHostnames it is the single Host(`...`) match the chart always
rendered, byte for byte; with some it is a parenthesised alternation, which Traefik v3
accepts in a rule. One helper, so no route can match a different host set than another.
Argument: the chart root.
*/}}
{{- define "exomem.cloudHostMatch" -}}
{{- if .Values.cloudGateway.additionalHostnames -}}
(Host(`{{ .Values.cloudIngress.hostname }}`){{ range .Values.cloudGateway.additionalHostnames }} || Host(`{{ . }}`){{ end }})
{{- else -}}
Host(`{{ .Values.cloudIngress.hostname }}`)
{{- end -}}
{{- end -}}

{{/*
Fail the render when cloudGateway.additionalHostnames repeats cloudGateway.hostname, which
the gateway already serves and offers first. A repeat would give the certificate, the
solver and the accepted resource list a duplicate entry.
Argument: the chart root.
*/}}
{{- define "exomem.requireDistinctAdditionalHostnames" -}}
{{- if has .Values.cloudGateway.hostname .Values.cloudGateway.additionalHostnames -}}
{{- fail "cloudGateway.additionalHostnames must not contain cloudGateway.hostname; the gateway already serves it" -}}
{{- end -}}
{{- end -}}
