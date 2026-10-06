{{/*
move-cloud-cells-to-local-storage: the cell storage cellctl is configured with
(CELLCTL_CELL_STORAGE, read by cellctl's build_storage_config), and the names
the chart's classes and admission use. One place, so they cannot disagree.
*/}}
{{- define "exomem.cellStorage" -}}
{{- $local := .Values.cellStorage.local -}}
{{- if and (eq .Values.cellStorage.domain "local") (not $local.enabled) -}}
{{- fail "cellStorage.domain local needs cellStorage.local.enabled" -}}
{{- end -}}
{{- $deviceClass := "" -}}
{{- range .Values.topolvm.lvmd.deviceClasses -}}
{{- if .default -}}{{- $deviceClass = .name -}}{{- end -}}
{{- end -}}
{{- $config := dict
    "class_name" "exomem-cloud-local"
    "clone_class" "exomem-cloud-local-clone"
    "snapshot_class" "exomem-cloud-local-snapshot"
    "driver" "topolvm.io"
    "device_class" $deviceClass
    "topology_key" "topology.topolvm.io/node"
    "backup_concurrency_per_node" $local.backupConcurrencyPerNode -}}
{{- $domain := ternary $config.class_name .Values.cloudStorage.className (eq .Values.cellStorage.domain "local") -}}
{{- dict "domain" $domain "local" $config | toJson -}}
{{- end -}}
