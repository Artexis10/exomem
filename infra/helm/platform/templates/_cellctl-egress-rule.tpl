{{- /*
The CEL conditions that pin one runtime-ingress egress rule: a single peer, the
named service pod in the platform namespace, on one TCP port. `index` is the
rule's position in the policy's egress list. It prints no closing parenthesis.
*/ -}}
{{- define "exomem.cellctl.egressRule" -}}
size(variables.np.egress[{{ .index }}].to) == 1 &&
              !has(variables.np.egress[{{ .index }}].to[0].ipBlock) &&
              variables.np.egress[{{ .index }}].to[0].namespaceSelector.matchLabels ==
                {'kubernetes.io/metadata.name': {{ .namespace | quote }}} &&
              !has(variables.np.egress[{{ .index }}].to[0].namespaceSelector.matchExpressions) &&
              variables.np.egress[{{ .index }}].to[0].podSelector.matchLabels ==
                {'app.kubernetes.io/name': '{{ .pod }}'} &&
              !has(variables.np.egress[{{ .index }}].to[0].podSelector.matchExpressions) &&
              size(variables.np.egress[{{ .index }}].ports) == 1 &&
              variables.np.egress[{{ .index }}].ports[0].protocol == 'TCP' &&
              variables.np.egress[{{ .index }}].ports[0].port == {{ .port }} &&
              !has(variables.np.egress[{{ .index }}].ports[0].endPort)
{{- end }}
