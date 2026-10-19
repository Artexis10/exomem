{{- define "exomem.scheduler.enabledJobs" -}}
{{- $contract := .Files.Get "files/exomem-hosted-schedules-v1.json" | fromJson -}}
{{- $jobs := list -}}
{{- range $job := $contract.jobs -}}
{{- $policy := index $.Values.scheduler.jobs $job.name -}}
{{- if not $policy -}}
{{- fail (printf "scheduler job %s has no chart policy" $job.name) -}}
{{- end -}}
{{- if or (not (hasKey $policy "enabled")) $policy.enabled -}}
{{- if lt (int $job.cadenceSeconds) 1 -}}
{{- fail "scheduler cadenceSeconds must be positive" -}}
{{- end -}}
{{- $jobs = append $jobs $job -}}
{{- end -}}
{{- end -}}
{{- $jobs | toJson -}}
{{- end -}}
