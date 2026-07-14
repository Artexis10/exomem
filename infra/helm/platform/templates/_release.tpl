{{- define "exomem.hostedReleaseJson" -}}
{{- $raw := required "provisioner.releaseManifestJson is required" .Values.provisioner.releaseManifestJson -}}
{{- $release := mustFromJson $raw -}}
{{- if not (kindIs "map" $release) -}}
{{- fail "hosted release manifest must be one JSON object" -}}
{{- end -}}
{{- $keys := list "artifact" "schemaVersion" "sourceRepository" "sourceCommit" "release" "hostedProtocol" "releaseBuildTime" "runtimeImage" "publishedTag" "operatorContractSha256" "gatewayContractSha256" "commandRegistry" -}}
{{- if ne (len $release) (len $keys) -}}
{{- fail "hosted release manifest fields are incomplete or unknown" -}}
{{- end -}}
{{- range $key := $keys -}}
{{- if not (hasKey $release $key) -}}
{{- fail (printf "hosted release manifest is missing %s" $key) -}}
{{- end -}}
{{- end -}}
{{- if ne $release.artifact "exomem-hosted-release" -}}
{{- fail "hosted release manifest artifact is unsupported" -}}
{{- end -}}
{{- if ne (int $release.schemaVersion) 1 -}}
{{- fail "hosted release manifest schema is unsupported" -}}
{{- end -}}
{{- if ne $release.sourceRepository "https://github.com/Artexis10/exomem" -}}
{{- fail "hosted release source repository is unsupported" -}}
{{- end -}}
{{- if not (regexMatch "^[0-9a-f]{40}$" $release.sourceCommit) -}}
{{- fail "hosted release source commit is not exact" -}}
{{- end -}}
{{- if not (regexMatch "^ghcr\\.io/artexis10/exomem@sha256:[0-9a-f]{64}$" $release.runtimeImage) -}}
{{- fail "hosted release runtime image is not immutable" -}}
{{- end -}}
{{- if ne $release.publishedTag (printf "ghcr.io/artexis10/exomem:%s-hosted" $release.sourceCommit) -}}
{{- fail "hosted release publication tag is not bound to its source commit" -}}
{{- end -}}
{{- if not (regexMatch "^[0-9a-f]{64}$" $release.operatorContractSha256) -}}
{{- fail "hosted release operator contract digest is invalid" -}}
{{- end -}}
{{- if not (regexMatch "^[0-9a-f]{64}$" $release.gatewayContractSha256) -}}
{{- fail "hosted release gateway contract digest is invalid" -}}
{{- end -}}
{{- if or (not (kindIs "slice" $release.commandRegistry)) (ne (len $release.commandRegistry) 21) -}}
{{- fail "hosted release command registry is incomplete" -}}
{{- end -}}
{{- $release | toJson -}}
{{- end -}}
