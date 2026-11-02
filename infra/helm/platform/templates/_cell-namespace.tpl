{{- /*
The name of a cell namespace: exo-cell-<16 lowercase base32 cell id>. Admission policies
that confine a controller to cell namespaces include this one pattern, so the cellctl scope
and the key custodian scope cannot drift apart. cellctl's renderer (manifests.NAMESPACE_PREFIX)
names namespaces the same way.
*/ -}}
{{- define "exomem.cellNamespacePattern" -}}
^exo-cell-[a-z2-7]{16}$
{{- end -}}
