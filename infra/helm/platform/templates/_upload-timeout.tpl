{{/*
Fail the render unless Traefik gives the websecure entrypoint time to read a whole upload.
Traefik reads a request body within respondingTimeouts.readTimeout (60s unless set) and
has no per-route value, so the upload route needs the entrypoint's own. The gateway
accepts a 100 MiB body, and 900 seconds covers it at about 1 Mbit/s, which is also how
long the cell keeps an upload route valid. The subchart reads its values only from
traefik.*, so this chart cannot set the timeout itself. It names the missing value instead.
Traefik takes digits (seconds) or a Go duration; this accepts digits and one s, m or h unit.
Argument: the chart root.
*/}}
{{- define "exomem.requireUploadReadTimeout" -}}
{{- $timeout := dig "ports" "websecure" "transport" "respondingTimeouts" "readTimeout" "" .Values.traefik | toString -}}
{{- $seconds := 0 -}}
{{- /* These patterns check the shape of a duration literal, not its meaning. */ -}}
{{- if regexMatch "^[0-9]+s?$" $timeout -}}
{{- $seconds = trimSuffix "s" $timeout | int -}}
{{- else if regexMatch "^[0-9]+m$" $timeout -}}
{{- $seconds = mul (trimSuffix "m" $timeout | int) 60 -}}
{{- else if regexMatch "^[0-9]+h$" $timeout -}}
{{- $seconds = mul (trimSuffix "h" $timeout | int) 3600 -}}
{{- end -}}
{{- if lt $seconds 900 -}}
{{- fail "cloudGateway.uploadCellIds requires traefik.ports.websecure.transport.respondingTimeouts.readTimeout of at least 900s (digits, or one s, m or h value)" -}}
{{- end -}}
{{- end -}}
