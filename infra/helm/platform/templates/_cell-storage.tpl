{{/*
move-cloud-cells-to-local-storage: the cell storage cellctl is configured with
(CELLCTL_CELL_STORAGE, read by cellctl's build_storage_config), and the names
the chart's classes and admission use. One place, so they cannot disagree.
*/}}
{{/*
Tasks 5.5 and 5.6: the pods cellctl reads for the host alerts, their labels and
their /metrics ports. Port 8080 is the TopoLVM node plugin's default
--metrics-bind-address (TopoLVM 0.41.1 docs/topolvm-node.md), which the chart
test pins against the rendered DaemonSet. The array exporter's port and labels
are this chart's own: templates/cell-array-exporter.yaml renders them.
*/}}
{{- define "exomem.cellMetrics" -}}
{{- dict "thin_pool" (dict "port" 8080 "labels" (dict
      "app.kubernetes.io/name" "topolvm"
      "app.kubernetes.io/component" "node"
      "app.kubernetes.io/instance" .Release.Name))
    "array" (dict "port" 9100 "labels" (dict "app.kubernetes.io/name" "exomem-cell-array-exporter")) | toJson -}}
{{- end -}}
{{- define "exomem.labelSelector" -}}
{{- $parts := list -}}
{{- range $key, $value := . -}}{{- $parts = append $parts (printf "%s=%s" $key $value) -}}{{- end -}}
{{- join "," $parts -}}
{{- end -}}
{{- define "exomem.cellStorage" -}}
{{- $local := .Values.cellStorage.local -}}
{{- if and (eq .Values.cellStorage.domain "local") (not $local.enabled) -}}
{{- fail "cellStorage.domain local needs cellStorage.local.enabled" -}}
{{- end -}}
{{- $metrics := include "exomem.cellMetrics" . | fromJson -}}
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
    "backup_concurrency_per_node" $local.backupConcurrencyPerNode
    "metrics_namespace" .Release.Namespace
    "thin_pool_selector" (include "exomem.labelSelector" $metrics.thin_pool.labels)
    "thin_pool_port" $metrics.thin_pool.port
    "array_selector" (include "exomem.labelSelector" $metrics.array.labels)
    "array_port" $metrics.array.port -}}
{{- $domain := ternary $config.class_name .Values.cloudStorage.className (eq .Values.cellStorage.domain "local") -}}
{{- if eq .Values.cellStorage.domain "per-cell" -}}
{{- $domain = .Values.cloudStorage.perCellClassName -}}
{{- end -}}
{{- $storage := dict "domain" $domain "per_cell_class" .Values.cloudStorage.perCellClassName -}}
{{- if $local.enabled -}}{{- $_ := set $storage "local" $config -}}{{- end -}}
{{- if ne .Values.cellStorage.maxCellGib nil -}}
{{- $_ := set $storage "max_cell_gib" .Values.cellStorage.maxCellGib -}}
{{- end -}}
{{- if ne $local.maxCellGib nil -}}
{{- if and (ne .Values.cellStorage.maxCellGib nil) (ne (int .Values.cellStorage.maxCellGib) (int $local.maxCellGib)) -}}
{{- fail "cellStorage.maxCellGib and cellStorage.local.maxCellGib must agree" -}}
{{- end -}}
{{- $_ := set $storage "max_cell_gib" $local.maxCellGib -}}
{{- end -}}
{{- $storage | toJson -}}
{{- end -}}
