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
{{- if ne (toJson $release.schemaVersion) "1" -}}
{{- fail "hosted release manifest schema is unsupported" -}}
{{- end -}}
{{- if ne $release.sourceRepository "https://github.com/Artexis10/exomem" -}}
{{- fail "hosted release source repository is unsupported" -}}
{{- end -}}
{{- if not (regexMatch "^[0-9a-f]{40}$" $release.sourceCommit) -}}
{{- fail "hosted release source commit is not exact" -}}
{{- end -}}
{{- if or (not (kindIs "string" $release.release)) (not (regexMatch "^[0-9]+\\.[0-9]+\\.[0-9]+(?:[-+][0-9A-Za-z.-]+)?$" $release.release)) -}}
{{- fail "hosted release version is invalid" -}}
{{- end -}}
{{- if or (not (kindIs "string" $release.hostedProtocol)) (ne $release.hostedProtocol "1") -}}
{{- fail "hosted release protocol is unsupported" -}}
{{- end -}}
{{- if or (not (kindIs "string" $release.releaseBuildTime)) (not (regexMatch "^[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}Z$" $release.releaseBuildTime)) -}}
{{- fail "hosted release build time is invalid" -}}
{{- end -}}
{{- $buildTime := toDate "2006-01-02T15:04:05Z07:00" $release.releaseBuildTime -}}
{{- if ne (dateInZone "2006-01-02T15:04:05Z" $buildTime "UTC") $release.releaseBuildTime -}}
{{- fail "hosted release build time is invalid" -}}
{{- end -}}
{{- if not (regexMatch "^ghcr\\.io/artexis10/exomem@sha256:[0-9a-f]{64}$" $release.runtimeImage) -}}
{{- fail "hosted release runtime image is not immutable" -}}
{{- end -}}
{{- if ne $release.publishedTag (printf "ghcr.io/artexis10/exomem:%s-hosted" $release.sourceCommit) -}}
{{- fail "hosted release publication tag is not bound to its source commit" -}}
{{- end -}}
{{- range $digest := list $release.operatorContractSha256 $release.gatewayContractSha256 -}}
{{- if not (regexMatch "^[0-9a-f]{64}$" $digest) -}}
{{- fail "hosted release contract digest is invalid" -}}
{{- end -}}
{{- end -}}
{{- $canonicalRegistry := .Files.Get "files/canonical-command-registry-v1.json" | mustFromJson -}}
{{- if not (deepEqual $release.commandRegistry $canonicalRegistry) -}}
{{- fail "hosted release command registry is not canonical" -}}
{{- end -}}
{{- $release | toJson -}}
{{- end -}}

{{- define "exomem.hostedRuntimeImage" -}}
{{- $release := include "exomem.hostedReleaseJson" . | mustFromJson -}}
{{- $release.runtimeImage -}}
{{- end -}}
