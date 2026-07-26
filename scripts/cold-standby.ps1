<#
.SYNOPSIS
  Operates Exomem's opt-in manual cold-standby profile.

.DESCRIPTION
  The profile is inactive until Configure is explicitly run with a host-owned
  configuration file outside the repository. Status is always read-only.

  Forced activation requires both -Force and the exact acknowledgement:
  I ACKNOWLEDGE SPLIT-BRAIN AND DATA-LOSS RISK

  Syncthing and the desired-host marker are advisory evidence, not a lock or a
  consensus protocol. An unreachable peer is never silently treated as stopped.

  -AdapterPath is a documented operational simulation seam. A complete adapter
  may replace service, HTTP, Syncthing, Cloudflare, scheduled-task, filesystem,
  and clock surfaces. Without it, this command uses native Windows, HTTP,
  Syncthing, cloudflared, and Cloudflare DNS API surfaces and fails closed.
#>
[CmdletBinding(SupportsShouldProcess)]
param(
    [Parameter(Mandatory, Position = 0)]
    [ValidateSet('Configure', 'Status', 'Activate', 'Handoff')]
    [string]$Action,
    [Parameter(Mandatory)]
    [string]$ConfigPath,
    [switch]$Json,
    [switch]$IfUnserved,
    [switch]$Force,
    [string]$Acknowledge,
    [string]$AdapterPath
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'
$ForceAcknowledgement = 'I ACKNOWLEDGE SPLIT-BRAIN AND DATA-LOSS RISK'
$script:ActiveAdapter = $null

function Get-OptionalValue {
    param([hashtable]$Value, [string]$Name, [object]$Default = $null)
    if ($null -ne $Value -and $Value.ContainsKey($Name)) { return $Value[$Name] }
    return $Default
}

function Write-Terminal {
    param([hashtable]$Terminal, [int]$ExitCode = 0)
    if (-not $Terminal.ContainsKey('action')) { $Terminal.action = $Action }
    if (-not $Terminal.ContainsKey('trace')) {
        $traceItems = if ($null -ne $script:ActiveAdapter) { @(Invoke-Adapter $script:ActiveAdapter 'GetTrace') } else { @() }
        $Terminal.trace = @($traceItems)
    }
    if ($Json) {
        $Terminal | ConvertTo-Json -Depth 20 -Compress
    } else {
        "[$($Terminal.action)] $($Terminal.terminal)"
        if ($Terminal.ContainsKey('active_host')) { "Active host: $($Terminal.active_host)" }
        if ($Terminal.ContainsKey('reasons') -and @($Terminal.reasons).Count) { "Reasons: $(@($Terminal.reasons) -join '; ')" }
        if ($Terminal.ContainsKey('next_action')) { "Next safe action: $($Terminal.next_action)" }
        if ($Terminal.ContainsKey('bypassed_guards') -and @($Terminal.bypassed_guards).Count) { "Bypassed guards: $(@($Terminal.bypassed_guards) -join '; ')" }
        if ($Terminal.ContainsKey('evidence')) { "Evidence: $($Terminal.evidence | ConvertTo-Json -Depth 8 -Compress)" }
    }
    exit $ExitCode
}

function Invoke-Adapter {
    param([hashtable]$Adapter, [string]$Name, [object[]]$Arguments = @())
    if (-not $Adapter.ContainsKey($Name) -or $null -eq $Adapter[$Name]) { throw "Adapter does not provide required operation '$Name'." }
    & $Adapter[$Name] @Arguments
}

function ConvertTo-NormalizedOrigin {
    param([string]$Value)
    $uri = [Uri]$Value.Trim()
    if (-not $uri.IsAbsoluteUri -or $uri.Scheme -ne 'https' -or -not [string]::IsNullOrEmpty($uri.UserInfo) -or -not [string]::IsNullOrEmpty($uri.Query) -or -not [string]::IsNullOrEmpty($uri.Fragment) -or $uri.AbsolutePath -notin @('', '/')) {
        throw 'The shared base URL and OAuth issuer must be a bare HTTPS origin with no path, query, fragment, or user information.'
    }
    return $uri.AbsoluteUri.TrimEnd('/').ToLowerInvariant()
}

function Get-UrlOrigin {
    param([string]$Value)
    $uri = [Uri]$Value
    $builder = [UriBuilder]$uri
    $builder.Path = ''
    $builder.Query = ''
    $builder.Fragment = ''
    $builder.Uri.AbsoluteUri.TrimEnd('/').ToLowerInvariant()
}

function Get-Sha256 {
    param([string]$Value)
    $bytes = [Text.Encoding]::UTF8.GetBytes($Value)
    [Convert]::ToHexString([Security.Cryptography.SHA256]::HashData($bytes)).ToLowerInvariant()
}

function Assert-RequiredConfig {
    param([hashtable]$Config, [string]$Field)
    $value = $Config
    foreach ($segment in $Field.Split('.')) {
        if ($value -isnot [hashtable] -or -not $value.ContainsKey($segment)) { throw "Configuration requires '$Field'." }
        $value = $value[$segment]
    }
    if ([string]::IsNullOrWhiteSpace([string]$value)) { throw "Configuration requires '$Field'." }
}

function Get-Config {
    param([string]$Path)
    if (-not (Test-Path -LiteralPath $Path -PathType Leaf)) { throw "Configuration file was not found: $Path" }
    $resolvedConfigPath = [IO.Path]::GetFullPath((Resolve-Path -LiteralPath $Path).Path)
    $repositoryRoot = [IO.Path]::GetFullPath((Split-Path -Parent $PSScriptRoot)).TrimEnd('\', '/')
    if ($resolvedConfigPath.StartsWith("$repositoryRoot$([IO.Path]::DirectorySeparatorChar)", [StringComparison]::OrdinalIgnoreCase)) {
        throw 'Operational cold-standby configuration must be copied to a host-owned path outside the repository.'
    }
    try { $config = Get-Content -Raw -LiteralPath $Path | ConvertFrom-Json -AsHashtable } catch { throw 'Configuration is not valid JSON.' }
    if ($config -isnot [hashtable]) { throw 'Configuration root must be a JSON object.' }

    $required = @(
        'schema_version', 'host.id', 'host.role', 'host.peer_id',
        'health.local_url', 'health.peer_url', 'health.stable_url', 'health.oauth_discovery_url',
        'services.exomem', 'services.cloudflared',
        'tunnels.local_name', 'tunnels.local_id', 'tunnels.peer_name', 'tunnels.peer_id',
        'tunnels.stable_hostname', 'tunnels.local_operational_hostname', 'tunnels.peer_operational_hostname', 'tunnels.config_path', 'tunnels.origin_service_url',
        'cloudflare.api_token_environment', 'cloudflare.zone_id_environment',
        'syncthing.api_url', 'syncthing.api_key_environment', 'syncthing.folder_id', 'syncthing.folder_path', 'syncthing.peer_device_id', 'syncthing.intent_relative_path',
        'state.journal_path', 'state.intent_path', 'state.manifest_path', 'state.peer_manifest_path',
        'identity_environment.dotenv_path', 'identity_environment.base_url', 'identity_environment.github_client_id', 'identity_environment.github_client_secret',
        'identity_environment.github_username', 'identity_environment.github_user_id', 'identity_environment.jwt_signing_key', 'identity_environment.instance_id',
        'operations.probe_attempts', 'operations.probe_delay_seconds', 'operations.desktop_logon_delay_seconds'
    )
    foreach ($field in $required) { Assert-RequiredConfig $config $field }

    $configDirectory = Split-Path -Parent $resolvedConfigPath
    foreach ($pathRef in @(
        @{ container = $config.tunnels; field = 'config_path' },
        @{ container = $config.state; field = 'journal_path' },
        @{ container = $config.state; field = 'intent_path' },
        @{ container = $config.state; field = 'manifest_path' },
        @{ container = $config.state; field = 'peer_manifest_path' },
        @{ container = $config.syncthing; field = 'folder_path' },
        @{ container = $config.identity_environment; field = 'dotenv_path' }
    )) {
        $container = $pathRef.container
        $field = [string]$pathRef.field
        if (-not [IO.Path]::IsPathRooted([string]$container[$field])) { $container[$field] = [IO.Path]::GetFullPath((Join-Path $configDirectory ([string]$container[$field]))) }
    }

    if ([int]$config.schema_version -ne 1) { throw 'Configuration schema_version must be 1.' }
    foreach ($identity in @($config.host.id, $config.host.peer_id)) {
        if ([string]$identity -notmatch '^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$') { throw 'host identities must be public-safe identifiers.' }
    }
    if ($config.host.role -notin @('desktop', 'laptop')) { throw 'host.role must be desktop or laptop.' }
    if ($config.host.id -eq $config.host.peer_id) { throw 'host and peer identities must be distinct.' }
    if ($config.tunnels.local_name -eq $config.tunnels.peer_name) { throw 'local and peer tunnel names must be distinct.' }
    if ($config.tunnels.local_id -eq $config.tunnels.peer_id) { throw 'local and peer tunnel IDs must be distinct.' }
    foreach ($tunnelId in @($config.tunnels.local_id, $config.tunnels.peer_id)) {
        $parsed = [guid]::Empty
        if (-not [guid]::TryParse([string]$tunnelId, [ref]$parsed)) { throw 'tunnel IDs must be Cloudflare UUIDs.' }
    }
    if ($config.health.local_url -eq $config.health.peer_url) { throw 'local and peer health URLs must be distinct.' }
    foreach ($url in @($config.health.local_url, $config.health.peer_url, $config.health.stable_url, $config.health.oauth_discovery_url, $config.syncthing.api_url, $config.tunnels.origin_service_url)) {
        $uri = [Uri][string]$url
        if (-not $uri.IsAbsoluteUri -or $uri.Scheme -notin @('http', 'https')) { throw "Invalid absolute HTTP(S) URL: $url" }
    }
    $stableUri = [Uri]$config.health.stable_url
    $oauthUri = [Uri]$config.health.oauth_discovery_url
    $localUri = [Uri]$config.health.local_url
    if ($stableUri.Host -ne $config.tunnels.stable_hostname -or $oauthUri.Host -ne $config.tunnels.stable_hostname) { throw 'stable readiness, OAuth discovery, and stable hostname must share one host.' }
    if ($localUri.Host -ne $config.tunnels.local_operational_hostname) { throw 'local readiness URL must use local_operational_hostname.' }
    $peerUri = [Uri]$config.health.peer_url
    if ($peerUri.Host -ne $config.tunnels.peer_operational_hostname) { throw 'peer readiness URL must use peer_operational_hostname.' }
    if (@(@($localUri.Host, $peerUri.Host, $stableUri.Host) | Select-Object -Unique).Count -ne 3) { throw 'local, peer, and stable readiness origins must be distinct.' }
    if ($peerUri.Scheme -ne 'https' -or $stableUri.Scheme -ne 'https' -or $oauthUri.Scheme -ne 'https') { throw 'peer, stable, and OAuth evidence URLs must use HTTPS.' }
    if ($stableUri.AbsolutePath.TrimEnd('/') -ne '/health/ready' -or $localUri.AbsolutePath.TrimEnd('/') -ne '/health/ready' -or $peerUri.AbsolutePath.TrimEnd('/') -ne '/health/ready') { throw 'local, peer, and stable readiness URLs must end with /health/ready.' }
    if ($oauthUri.AbsolutePath.TrimEnd('/') -ne '/.well-known/oauth-authorization-server') { throw 'OAuth discovery URL must use /.well-known/oauth-authorization-server.' }

    $paths = @($config.state.journal_path, $config.state.intent_path, $config.state.manifest_path, $config.state.peer_manifest_path)
    if (@($paths | Select-Object -Unique).Count -ne $paths.Count) { throw 'journal, intent, and manifest paths must be distinct.' }
    if ($config.state.intent_path -match '(?i)(knowledge[ _-]*base|\\notes\\|/notes/)') { throw 'The advisory intent path must be outside Knowledge Base.' }
    $repositoryPrefix = "$repositoryRoot$([IO.Path]::DirectorySeparatorChar)"
    foreach ($operationalPath in @($config.tunnels.config_path, $config.state.journal_path, $config.state.intent_path, $config.state.manifest_path, $config.state.peer_manifest_path, $config.syncthing.folder_path)) {
        if ([IO.Path]::GetFullPath([string]$operationalPath).StartsWith($repositoryPrefix, [StringComparison]::OrdinalIgnoreCase)) { throw 'Tunnel, Syncthing, journal, intent, and manifest paths must remain outside the repository.' }
    }
    $syncRoot = [IO.Path]::GetFullPath([string]$config.syncthing.folder_path).TrimEnd('\', '/')
    $syncPrefix = "$syncRoot$([IO.Path]::DirectorySeparatorChar)"
    foreach ($replicatedPath in @($config.state.intent_path, $config.state.manifest_path, $config.state.peer_manifest_path)) {
        if (-not [IO.Path]::GetFullPath([string]$replicatedPath).StartsWith($syncPrefix, [StringComparison]::OrdinalIgnoreCase)) { throw 'Intent and both identity manifests must be inside the configured Syncthing folder.' }
    }
    if ([IO.Path]::GetFullPath([string]$config.state.journal_path).StartsWith($syncPrefix, [StringComparison]::OrdinalIgnoreCase)) { throw 'The local operation journal must remain outside the Syncthing folder.' }
    $expectedIntentPath = [IO.Path]::GetFullPath((Join-Path $syncRoot ([string]$config.syncthing.intent_relative_path).Replace('/', [IO.Path]::DirectorySeparatorChar)))
    if ($expectedIntentPath -ne [IO.Path]::GetFullPath([string]$config.state.intent_path)) { throw 'state.intent_path must match syncthing.folder_path plus syncthing.intent_relative_path.' }
    if ([int]$config.operations.probe_attempts -lt 2 -or [int]$config.operations.probe_attempts -gt 10) { throw 'operations.probe_attempts must be between 2 and 10.' }
    if ([int]$config.operations.probe_delay_seconds -lt 0 -or [int]$config.operations.probe_delay_seconds -gt 30) { throw 'operations.probe_delay_seconds must be between 0 and 30.' }
    if ([int]$config.operations.desktop_logon_delay_seconds -lt 10 -or [int]$config.operations.desktop_logon_delay_seconds -gt 600) { throw 'operations.desktop_logon_delay_seconds must be between 10 and 600.' }
    return $config
}

function Read-DotenvMap {
    param([string]$Path)
    if (-not (Test-Path -LiteralPath $Path -PathType Leaf)) { throw "Effective service environment file was not found: $Path" }
    $values = @{}
    foreach ($line in Get-Content -LiteralPath $Path) {
        if ($line -notmatch '^\s*(?:export\s+)?([A-Za-z_][A-Za-z0-9_]*)\s*=\s*(.*)$') { continue }
        $name = $Matches[1]
        $value = $Matches[2].Trim()
        if ($value.Length -ge 2 -and (($value.StartsWith('"') -and $value.EndsWith('"')) -or ($value.StartsWith("'") -and $value.EndsWith("'")))) { $value = $value.Substring(1, $value.Length - 2) }
        $values[$name] = $value
    }
    return $values
}

function Get-IdentityContract {
    param([hashtable]$Config, [hashtable]$Adapter, [switch]$AllowUnavailable)
    $names = $Config.identity_environment
    $raw = @{}
    $missing = [System.Collections.Generic.List[string]]::new()
    try { $dotenv = Read-DotenvMap ([string]$names.dotenv_path) } catch {
        if ($AllowUnavailable) { return @{ state = 'unavailable'; missing = @('dotenv_path'); manifest = $null; expected_base_url = $null } }
        throw
    }
    try { $serviceEnvironment = Invoke-Adapter $Adapter 'GetServiceEnvironment' @($Config.services.exomem) } catch { $serviceEnvironment = @{ state = 'unknown'; values = @{} } }
    if ($serviceEnvironment.state -ne 'known' -or $serviceEnvironment.values -isnot [hashtable]) {
        if ($AllowUnavailable) { return @{ state = 'unavailable'; missing = @('service_environment'); manifest = $null; expected_base_url = $null } }
        throw 'The effective NSSM AppEnvironmentExtra could not be read.'
    }
    $drift = [System.Collections.Generic.List[string]]::new()
    foreach ($name in @('base_url', 'github_client_id', 'github_client_secret', 'github_username', 'github_user_id', 'jwt_signing_key', 'instance_id')) {
        $environmentName = [string]$names[$name]
        $dotenvValue = if ($dotenv.ContainsKey($environmentName)) { [string]$dotenv[$environmentName] } else { $null }
        $value = if ($serviceEnvironment.values.ContainsKey($environmentName)) { [string]$serviceEnvironment.values[$environmentName] } else { $null }
        if ($dotenvValue -ne $value) { $drift.Add($name) }
        if ([string]::IsNullOrWhiteSpace($value)) { $missing.Add($name) } else { $raw[$name] = $value.Trim() }
    }
    if ($missing.Count) {
        if ($AllowUnavailable) { return @{ state = 'unavailable'; missing = @($missing); manifest = $null; expected_base_url = $null } }
        throw "Effective identity configuration is unavailable for: $($missing -join ', ')."
    }
    if ($drift.Count) {
        if ($AllowUnavailable) { return @{ state = 'drift'; missing = @($drift); manifest = $null; expected_base_url = $null } }
        throw "NSSM AppEnvironmentExtra differs from the configured dotenv for: $($drift -join ', '). Rerun install-service.ps1, then Configure."
    }
    if ($raw.instance_id -ne $Config.host.id) {
        if ($AllowUnavailable) { return @{ state = 'drift'; missing = @('instance_id'); manifest = $null; expected_base_url = $null } }
        throw 'Effective EXOMEM_INSTANCE_ID does not match configured host.id.'
    }
    try { $baseUrl = ConvertTo-NormalizedOrigin $raw.base_url } catch {
        if ($AllowUnavailable) { return @{ state = 'drift'; missing = @('base_url'); manifest = $null; expected_base_url = $null } }
        throw
    }
    if ($baseUrl -ne (Get-UrlOrigin $Config.health.stable_url)) {
        if ($AllowUnavailable) { return @{ state = 'drift'; missing = @('base_url'); manifest = $null; expected_base_url = $null } }
        throw 'Effective EXOMEM_BASE_URL must equal the stable readiness origin.'
    }
    $callback = "$baseUrl/auth/callback"
    $manifest = @{
        schema_version = 1
        host_id = $Config.host.id
        fingerprints = @{
            stable_base_url = Get-Sha256 $baseUrl
            github_oauth_client_id = Get-Sha256 $raw.github_client_id
            github_oauth_client_secret = Get-Sha256 $raw.github_client_secret
            github_oauth_callback = Get-Sha256 $callback
            allowed_github_username = Get-Sha256 $raw.github_username.ToLowerInvariant()
            allowed_github_user_id = Get-Sha256 $raw.github_user_id
            jwt_signing_key = Get-Sha256 $raw.jwt_signing_key
        }
    }
    @{ state = 'available'; manifest = $manifest; expected_base_url = $baseUrl }
}

function Read-JsonEvidence {
    param([hashtable]$Adapter, [string]$Path)
    try {
        $value = Invoke-Adapter $Adapter 'ReadJson' @($Path)
        if ($null -eq $value) { return @{ state = 'absent'; value = $null } }
        if ($value -isnot [hashtable]) { return @{ state = 'invalid'; value = $null } }
        return @{ state = 'present'; value = $value }
    } catch { return @{ state = 'unknown'; value = $null } }
}

function Test-Intent {
    param([hashtable]$IntentEvidence, [hashtable]$Config)
    if ($IntentEvidence.state -ne 'present') { return $IntentEvidence.state }
    $intent = $IntentEvidence.value
    if (-not $intent.ContainsKey('desired_host') -or $intent.desired_host -notin @($Config.host.id, $Config.host.peer_id)) { return 'invalid' }
    $generation = [int64]0
    if (-not $intent.ContainsKey('generation') -or -not [int64]::TryParse([string]$intent.generation, [ref]$generation) -or $generation -lt 1) { return 'invalid' }
    return 'present'
}

function Get-ManifestMismatchReasons {
    param([hashtable]$Identity, [hashtable]$LocalEvidence, [hashtable]$PeerEvidence, [hashtable]$Config)
    $reasons = [System.Collections.Generic.List[string]]::new()
    if ($Identity.state -ne 'available') { $reasons.Add('effective shared configuration is unavailable'); return @($reasons) }
    foreach ($entry in @(@{ label = 'local'; evidence = $LocalEvidence }, @{ label = 'peer'; evidence = $PeerEvidence })) {
        $schemaVersion = [int]0
        $validSchema = $entry.evidence.state -eq 'present' -and [int]::TryParse([string](Get-OptionalValue $entry.evidence.value 'schema_version' 0), [ref]$schemaVersion) -and $schemaVersion -eq 1
        if ($entry.evidence.state -ne 'present' -or
            -not $validSchema -or
            -not $entry.evidence.value.ContainsKey('fingerprints') -or
            $entry.evidence.value.fingerprints -isnot [hashtable]) {
            $reasons.Add("shared configuration $($entry.label) manifest is unavailable")
            continue
        }
        $expectedHost = if ($entry.label -eq 'local') { $Config.host.id } else { $Config.host.peer_id }
        if ((Get-OptionalValue $entry.evidence.value 'host_id') -ne $expectedHost) {
            $reasons.Add("shared configuration $($entry.label) manifest identifies the wrong host")
        }
        foreach ($field in @('stable_base_url', 'github_oauth_client_id', 'github_oauth_client_secret', 'github_oauth_callback', 'allowed_github_username', 'allowed_github_user_id', 'jwt_signing_key')) {
            $actual = Get-OptionalValue $entry.evidence.value.fingerprints $field
            $expected = $Identity.manifest.fingerprints[$field]
            if ($actual -ne $expected) { $reasons.Add("shared configuration fingerprint mismatch: $field") }
        }
    }
    @($reasons | Select-Object -Unique)
}

function Invoke-BoundedProbe {
    param([hashtable]$Adapter, [string]$Url, [int]$Attempts, [int]$DelaySeconds)
    $results = [System.Collections.Generic.List[hashtable]]::new()
    for ($index = 0; $index -lt $Attempts; $index++) {
        try {
            $result = Invoke-Adapter $Adapter 'Probe' @($Url)
            if ($result -isnot [hashtable] -or -not $result.ContainsKey('state')) { $result = @{ state = 'unknown' } }
        } catch { $result = @{ state = 'unknown' } }
        $results.Add($result)
        if ($index -lt ($Attempts - 1) -and $DelaySeconds -gt 0) { Invoke-Adapter $Adapter 'Sleep' @($DelaySeconds) | Out-Null }
    }
    $states = @($results | ForEach-Object { [string]$_.state } | Select-Object -Unique)
    $summary = @{
        state = if ($states.Count -eq 1) { $states[0] } else { 'ambiguous' }
        samples = $Attempts
        observed_ready = ('ready' -in $states)
        observed_inactive = ('inactive' -in $states)
        observed_unserved = ('unserved' -in $states)
    }
    $last = $results[$results.Count - 1]
    foreach ($field in @('instance_id', 'status_code')) { if ($last.ContainsKey($field)) { $summary[$field] = $last[$field] } }
    return $summary
}

function Get-Evidence {
    param([hashtable]$Config, [hashtable]$Adapter)
    $attempts = [int]$Config.operations.probe_attempts
    $delay = [int]$Config.operations.probe_delay_seconds
    try { $service = Invoke-Adapter $Adapter 'GetService' @($Config.services.exomem) } catch { $service = @{ state = 'unknown' } }
    try { $sync = Invoke-Adapter $Adapter 'GetSyncthing' @($Config.syncthing) } catch { $sync = @{ api = 'unavailable' } }
    try { $localTunnel = Invoke-Adapter $Adapter 'GetTunnel' @($Config.tunnels.local_name) } catch { $localTunnel = @{ state = 'unknown' } }
    try { $peerTunnel = Invoke-Adapter $Adapter 'GetTunnel' @($Config.tunnels.peer_name) } catch { $peerTunnel = @{ state = 'unknown' } }
    try { $route = Invoke-Adapter $Adapter 'GetRoute' @($Config.cloudflare, $Config.tunnels) } catch { $route = @{ state = 'unknown'; tunnel_id = $null; target = $null } }
    try { $oauth = Invoke-Adapter $Adapter 'ProbeOAuth' @($Config.health.oauth_discovery_url) } catch { $oauth = @{ state = 'unknown' } }
    $identity = Get-IdentityContract $Config $Adapter -AllowUnavailable
    $intent = Read-JsonEvidence $Adapter $Config.state.intent_path
    $intent.state = Test-Intent $intent $Config
    $journal = Read-JsonEvidence $Adapter $Config.state.journal_path
    $localManifest = Read-JsonEvidence $Adapter $Config.state.manifest_path
    $peerManifest = Read-JsonEvidence $Adapter $Config.state.peer_manifest_path
    @{
        service = $service
        local_health = Invoke-BoundedProbe $Adapter $Config.health.local_url $attempts $delay
        peer_health = Invoke-BoundedProbe $Adapter $Config.health.peer_url $attempts $delay
        stable_health = Invoke-BoundedProbe $Adapter $Config.health.stable_url $attempts $delay
        stable_oauth = $oauth
        syncthing = $sync
        local_tunnel = $localTunnel
        peer_tunnel = $peerTunnel
        route = $route
        intent = $intent
        journal = $journal
        identity = $identity
        local_manifest = $localManifest
        peer_manifest = $peerManifest
    }
}

function Test-SyncthingConverged {
    param([hashtable]$Sync)
    return (Get-OptionalValue $Sync 'api') -eq 'reachable' -and
        (Get-OptionalValue $Sync 'folder_state') -eq 'idle' -and
        [int64](Get-OptionalValue $Sync 'pending_items' -1) -eq 0 -and
        [int64](Get-OptionalValue $Sync 'pending_bytes' -1) -eq 0 -and
        [bool](Get-OptionalValue $Sync 'peer_complete' $false) -and
        [double](Get-OptionalValue $Sync 'peer_completion' -1) -ge 100
}

function Get-StatusReasons {
    param([hashtable]$Evidence, [hashtable]$Config)
    $reasons = [System.Collections.Generic.List[string]]::new()
    if ($Evidence.local_health.observed_ready -and $Evidence.peer_health.observed_ready) { $reasons.Add('both origins report ready') }
    foreach ($probe in @(@{ name = 'local'; value = $Evidence.local_health }, @{ name = 'peer'; value = $Evidence.peer_health }, @{ name = 'stable endpoint'; value = $Evidence.stable_health })) {
        if ($probe.value.state -in @('unknown', 'unreachable', 'ambiguous')) { $reasons.Add("$($probe.name) health is unknown") }
    }
    if ($Evidence.local_health.state -eq 'unserved' -and $Evidence.local_tunnel.state -ne 'connected') { $reasons.Add('local origin is unserved but tunnel state is not connected') }
    if ($Evidence.peer_health.state -eq 'unserved' -and $Evidence.peer_tunnel.state -ne 'connected') { $reasons.Add('peer origin is unserved but tunnel state is not connected') }
    if ($Evidence.stable_health.state -eq 'unserved' -and $Evidence.route.state -ne 'known') { $reasons.Add('stable endpoint is unserved and its route is unknown') }
    if ((Get-OptionalValue $Evidence.syncthing 'api') -ne 'reachable') { $reasons.Add('Syncthing status is unknown') }
    elseif (-not (Test-SyncthingConverged $Evidence.syncthing)) { $reasons.Add('Syncthing is not converged') }
    if ($Evidence.local_tunnel.state -notin @('connected', 'disconnected')) { $reasons.Add('local tunnel state is unknown') }
    if ($Evidence.peer_tunnel.state -notin @('connected', 'disconnected')) { $reasons.Add('peer tunnel state is unknown') }
    if ($Evidence.route.state -ne 'known') { $reasons.Add('stable DNS route is unknown') }
    if ($Evidence.intent.state -in @('unknown', 'invalid')) { $reasons.Add('desired-host intent is unknown') }
    foreach ($reason in @(Get-ManifestMismatchReasons $Evidence.identity $Evidence.local_manifest $Evidence.peer_manifest $Config)) { $reasons.Add([string]$reason) }
    @($reasons | Select-Object -Unique)
}

function Get-ActivationReasons {
    param([hashtable]$Evidence, [hashtable]$Config, [switch]$GuardedBoot)
    $reasons = [System.Collections.Generic.List[string]]::new()
    if ($Evidence.service.state -ne 'stopped') { $reasons.Add('local Exomem service is not conclusively stopped') }
    if ($Evidence.local_health.state -eq 'ready') { $reasons.Add('local origin is unexpectedly ready before activation') }
    if ($Evidence.local_health.state -in @('unknown', 'ambiguous')) { $reasons.Add('local pre-activation health is unknown') }
    if ($Evidence.local_tunnel.state -ne 'connected') { $reasons.Add('local named tunnel is not connected') }
    if ((Get-OptionalValue $Evidence.syncthing 'api') -ne 'reachable') { $reasons.Add('Syncthing API is unavailable') }
    elseif (-not (Test-SyncthingConverged $Evidence.syncthing)) { $reasons.Add('Syncthing is not converged') }
    $plannedPeerUnserved = $Evidence.peer_health.state -eq 'unserved' -and $Evidence.peer_tunnel.state -eq 'connected'
    if ($Evidence.peer_health.observed_ready) { $reasons.Add('peer is ready') }
    elseif ($Evidence.peer_health.state -ne 'inactive' -and -not $plannedPeerUnserved) { $reasons.Add('peer inactivity is unproven') }
    if ($Evidence.stable_health.observed_ready) { $reasons.Add('stable endpoint is already ready') }
    else {
        $routeIsConnected = ((Test-ExpectedRoute $Evidence.route $Config.tunnels.local_id) -and $Evidence.local_tunnel.state -eq 'connected') -or
            ((Test-ExpectedRoute $Evidence.route $Config.tunnels.peer_id) -and $Evidence.peer_tunnel.state -eq 'connected')
        $plannedStableUnserved = $Evidence.stable_health.state -eq 'unserved' -and $routeIsConnected
        if ($Evidence.stable_health.state -ne 'inactive' -and -not $plannedStableUnserved) { $reasons.Add('stable endpoint state is ambiguous') }
    }
    if ($Evidence.intent.state -in @('unknown', 'invalid')) { $reasons.Add('desired-host intent is unavailable or invalid') }
    elseif ($Evidence.intent.state -eq 'present' -and $Evidence.intent.value.desired_host -eq $Config.host.peer_id) { $reasons.Add('desired-host intent names peer') }
    foreach ($reason in @(Get-ManifestMismatchReasons $Evidence.identity $Evidence.local_manifest $Evidence.peer_manifest $Config)) { $reasons.Add([string]$reason) }
    if ($GuardedBoot) {
        if ($Config.host.role -ne 'desktop') { $reasons.Add('activate-if-unserved is desktop-only') }
        if ($Evidence.intent.state -ne 'present' -or $Evidence.intent.value.desired_host -ne $Config.host.id) { $reasons.Add('activate-if-unserved requires intent naming desktop') }
    }
    @($reasons | Select-Object -Unique)
}

function Get-HandoffReasons {
    param([hashtable]$Evidence, [hashtable]$Config)
    $reasons = [System.Collections.Generic.List[string]]::new()
    if ($Evidence.service.state -ne 'running') { $reasons.Add('source service is not running') }
    if ($Evidence.local_health.state -ne 'ready' -or (Get-OptionalValue $Evidence.local_health 'instance_id') -ne $Config.host.id) { $reasons.Add('source runtime readiness is not proven') }
    $targetInactive = -not $Evidence.peer_health.observed_ready -and ($Evidence.peer_health.state -eq 'inactive' -or ($Evidence.peer_health.state -eq 'unserved' -and $Evidence.peer_tunnel.state -eq 'connected'))
    if (-not $targetInactive) { $reasons.Add('target inactivity is not proven') }
    if ($Evidence.stable_health.state -ne 'ready' -or (Get-OptionalValue $Evidence.stable_health 'instance_id') -ne $Config.host.id) { $reasons.Add('stable endpoint does not identify the source host') }
    if ($Evidence.route.state -ne 'known' -or $Evidence.route.tunnel_id -ne $Config.tunnels.local_id) { $reasons.Add('stable route does not select the source tunnel') }
    if ($Evidence.local_tunnel.state -ne 'connected' -or $Evidence.peer_tunnel.state -ne 'connected') { $reasons.Add('both named tunnels must be connected') }
    if ((Get-OptionalValue $Evidence.syncthing 'api') -ne 'reachable' -or -not (Test-SyncthingConverged $Evidence.syncthing)) { $reasons.Add('source and peer replication completion are required') }
    if ($Evidence.intent.state -in @('unknown', 'invalid')) { $reasons.Add('desired-host intent is unavailable or invalid') }
    elseif ($Evidence.intent.state -eq 'present' -and $Evidence.intent.value.desired_host -ne $Config.host.id) { $reasons.Add('desired-host intent must name the active source') }
    foreach ($reason in @(Get-ManifestMismatchReasons $Evidence.identity $Evidence.local_manifest $Evidence.peer_manifest $Config)) { $reasons.Add([string]$reason) }
    if ($Evidence.identity.state -eq 'available') {
        $oauthIssuer = Get-OptionalValue $Evidence.stable_oauth 'issuer'
        if ($Evidence.stable_oauth.state -ne 'ready' -or [string]::IsNullOrWhiteSpace([string]$oauthIssuer) -or (ConvertTo-NormalizedOrigin ([string]$oauthIssuer)) -ne $Evidence.identity.expected_base_url) { $reasons.Add('stable OAuth discovery does not match the shared base URL') }
    }
    @($reasons | Select-Object -Unique)
}

function Write-OperationJson {
    param([hashtable]$Adapter, [string]$Path, [hashtable]$Value)
    Invoke-Adapter $Adapter 'WriteJson' @($Path, $Value) | Out-Null
}

function Get-NextGeneration {
    param([hashtable]$Adapter, [hashtable]$IntentEvidence)
    $now = [int64](Invoke-Adapter $Adapter 'Now')
    $previous = if ($IntentEvidence.state -eq 'present') { [int64]$IntentEvidence.value.generation } else { 0 }
    [Math]::Max([int64]($previous + 1), [int64]$now)
}

function Get-TunnelPolicy {
    param([hashtable]$Config)
    @{
        config_path = $Config.tunnels.config_path
        stable_hostname = $Config.tunnels.stable_hostname
        operational_hostname = $Config.tunnels.local_operational_hostname
        origin_service_url = $Config.tunnels.origin_service_url
        local_name = $Config.tunnels.local_name
        local_id = $Config.tunnels.local_id
        stable = 'forward-all'
        direct = @('/health', '/health/ready')
        direct_fallback = 'http_status:404'
    }
}

function Test-ExpectedRoute {
    param([hashtable]$Route, [string]$TunnelId)
    if ($Route.state -ne 'known' -or $Route.tunnel_id -ne $TunnelId) { return $false }
    $target = ([string](Get-OptionalValue $Route 'target' '')).TrimEnd('.').ToLowerInvariant()
    return $target -eq "$($TunnelId.ToLowerInvariant()).cfargotunnel.com"
}

function Test-PublicHost {
    param([hashtable]$Adapter, [hashtable]$Config, [hashtable]$Identity, [string]$ExpectedHost)
    $readiness = Invoke-BoundedProbe $Adapter $Config.health.stable_url ([int]$Config.operations.probe_attempts) ([int]$Config.operations.probe_delay_seconds)
    try { $oauth = Invoke-Adapter $Adapter 'ProbeOAuth' @($Config.health.oauth_discovery_url) } catch { $oauth = @{ state = 'unknown' } }
    $oauthMatches = $false
    if ($oauth.state -eq 'ready' -and $oauth.ContainsKey('issuer')) {
        try { $oauthMatches = (ConvertTo-NormalizedOrigin ([string]$oauth.issuer)) -eq $Identity.expected_base_url } catch { $oauthMatches = $false }
    }
    @{
        ready = ($readiness.state -eq 'ready' -and (Get-OptionalValue $readiness 'instance_id') -eq $ExpectedHost -and $oauthMatches)
        readiness = $readiness
        oauth = @{ state = $oauth.state; issuer_matches = $oauthMatches }
    }
}

function Confirm-ExactIntentDelivery {
    param([hashtable]$Adapter, [hashtable]$Config, [hashtable]$Journal)
    $intent = Read-JsonEvidence $Adapter $Config.state.intent_path
    $intent.state = Test-Intent $intent $Config
    if ($intent.state -ne 'present' -or [int64]$intent.value.generation -ne [int64]$Journal.generation -or $intent.value.desired_host -ne $Journal.desired_host) {
        return @{ delivered = $false; reason = 'the current desired-host marker does not match the journaled handoff' }
    }
    try { $marker = Invoke-Adapter $Adapter 'GetMarkerVersion' @($Config.state.intent_path) } catch { return @{ delivered = $false; reason = 'the current desired-host marker could not be versioned' } }
    if ([int64]$marker.generation -ne [int64]$Journal.generation -or [string]::IsNullOrWhiteSpace([string]$marker.version)) {
        return @{ delivered = $false; reason = 'the current desired-host marker version does not match its generation' }
    }
    if (-not [string]::IsNullOrWhiteSpace([string](Get-OptionalValue $Journal 'marker_version')) -and $Journal.marker_version -ne $marker.version) {
        return @{ delivered = $false; reason = 'the current desired-host marker was replaced after the operation was journaled' }
    }
    $Journal.marker_version = $marker.version
    $delivery = $null
    for ($index = 0; $index -lt [int]$Config.operations.probe_attempts; $index++) {
        try { $delivery = Invoke-Adapter $Adapter 'GetIntentDelivery' @($Config.syncthing, [int64]$Journal.generation, $Config.state.intent_path) } catch { $delivery = @{ state = 'unknown'; generation = $null; marker_version = $null } }
        if ($delivery.state -eq 'delivered' -and [int64]$delivery.generation -eq [int64]$Journal.generation -and $delivery.marker_version -eq $marker.version) { break }
        if ($index -lt ([int]$Config.operations.probe_attempts - 1) -and [int]$Config.operations.probe_delay_seconds -gt 0) { Invoke-Adapter $Adapter 'Sleep' @([int]$Config.operations.probe_delay_seconds) | Out-Null }
    }
    if ($delivery.state -ne 'delivered' -or [int64]$delivery.generation -ne [int64]$Journal.generation -or $delivery.marker_version -ne $marker.version) {
        return @{ delivered = $false; reason = 'the target did not prove receipt of the exact new desired-host generation' }
    }
    if ($delivery.ContainsKey('syncthing_version')) { $Journal.syncthing_version = $delivery.syncthing_version }
    return @{ delivered = $true; reason = $null }
}

function Save-Journal {
    param([hashtable]$Adapter, [hashtable]$Config, [hashtable]$Journal)
    $Journal.schema_version = 1
    Write-OperationJson $Adapter $Config.state.journal_path $Journal
}

function Invoke-ActivationCompensation {
    param([hashtable]$Adapter, [hashtable]$Config, [hashtable]$Journal, [string]$Failure)
    $restored = $false
    if (-not [string]::IsNullOrWhiteSpace([string]$Journal.previous_route)) {
        try {
            $restoredRoute = Invoke-Adapter $Adapter 'GetRoute' @($Config.cloudflare, $Config.tunnels)
            if (-not (Test-ExpectedRoute $restoredRoute ([string]$Journal.previous_route))) {
                Invoke-Adapter $Adapter 'SetRoute' @([string]$Journal.previous_route, $Config.tunnels.stable_hostname) | Out-Null
                $restoredRoute = Invoke-Adapter $Adapter 'GetRoute' @($Config.cloudflare, $Config.tunnels)
            }
            $restored = Test-ExpectedRoute $restoredRoute ([string]$Journal.previous_route)
        } catch { $restored = $false }
    }
    if ([bool](Get-OptionalValue $Journal 'started_service' $false)) {
        try {
            $service = Invoke-Adapter $Adapter 'GetService' @($Config.services.exomem)
            if ($service.state -eq 'running') { Invoke-Adapter $Adapter 'StopService' @($Config.services.exomem) | Out-Null }
            $service = Invoke-Adapter $Adapter 'GetService' @($Config.services.exomem)
            if ($service.state -ne 'stopped') { $restored = $false }
        } catch { $restored = $false }
    }
    $terminal = if ($restored) { 'rolled_back' } else { 'operator_recovery_required' }
    $Journal.phase = 'terminal'
    $Journal.terminal = $terminal
    $Journal.failure = $Failure
    Save-Journal $Adapter $Config $Journal
    Write-Terminal @{
        terminal = $terminal
        reasons = @($Failure)
        next_action = if ($restored) { 'verify the previous host before retrying' } else { 'keep both services stopped; inspect DNS target and both direct readiness endpoints' }
        bypassed_guards = @((Get-OptionalValue $Journal 'bypassed_guards' @()))
    } 2
}

function Invoke-ActivationReplay {
    param([hashtable]$Adapter, [hashtable]$Config, [hashtable]$Evidence)
    if ($Evidence.journal.state -ne 'present') { return }
    $journal = $Evidence.journal.value
    if ($journal.action -ne 'Activate' -or $journal.desired_host -ne $Config.host.id) { return }
    if ($journal.terminal -eq 'activated') {
        $route = Invoke-Adapter $Adapter 'GetRoute' @($Config.cloudflare, $Config.tunnels)
        $public = Test-PublicHost $Adapter $Config $Evidence.identity $Config.host.id
        if ((Test-ExpectedRoute $route $Config.tunnels.local_id) -and $public.ready) {
            Write-Terminal @{ terminal = 'already_activated'; route_target = $route.target; next_action = 'none: local host is already serving'; bypassed_guards = @($journal.bypassed_guards) }
        }
        return
    }
    if ($journal.phase -in @('intent_written', 'starting_service', 'service_started', 'routing', 'route_changed', 'public_verified')) {
        $route = Invoke-Adapter $Adapter 'GetRoute' @($Config.cloudflare, $Config.tunnels)
        if (Test-ExpectedRoute $route $Config.tunnels.local_id) {
            if ([string]::IsNullOrWhiteSpace([string]$journal.previous_route)) { Invoke-ActivationCompensation $Adapter $Config $journal 'interrupted activation has no known previous route' }
            $public = Test-PublicHost $Adapter $Config $Evidence.identity $Config.host.id
            if ($public.ready) {
                $journal.phase = 'terminal'
                $journal.terminal = 'activated'
                Save-Journal $Adapter $Config $journal
                Write-Terminal @{ terminal = 'recovered_activation'; route_target = $route.target; next_action = 'none: interrupted activation was verified and committed'; bypassed_guards = @((Get-OptionalValue $journal 'bypassed_guards' @())) }
            }
            Invoke-ActivationCompensation $Adapter $Config $journal 'interrupted activation route points local but public identity is not proven'
        }
        if (-not [string]::IsNullOrWhiteSpace([string]$journal.previous_route) -and (Test-ExpectedRoute $route ([string]$journal.previous_route))) {
            $journal.started_service = $true
            Invoke-ActivationCompensation $Adapter $Config $journal 'interrupted activation was observed before route movement'
        }
        Invoke-ActivationCompensation $Adapter $Config $journal 'interrupted activation route state is unknown or inconsistent'
    }
    return
}

function Invoke-HandoffCompensation {
    param([hashtable]$Adapter, [hashtable]$Config, [hashtable]$Journal, [string]$Failure)
    $attempts = [int]$Config.operations.probe_attempts
    $delay = [int]$Config.operations.probe_delay_seconds
    $target = Invoke-BoundedProbe $Adapter $Config.health.peer_url $attempts $delay
    try { $targetTunnel = Invoke-Adapter $Adapter 'GetTunnel' @($Config.tunnels.peer_name) } catch { $targetTunnel = @{ state = 'unknown' } }
    $targetInactive = -not $target.observed_ready -and ($target.state -eq 'inactive' -or ($target.state -eq 'unserved' -and $targetTunnel.state -eq 'connected'))
    if (-not $targetInactive) {
        $Journal.phase = 'terminal'
        $Journal.terminal = 'operator_recovery_required'
        $Journal.failure = $Failure
        Save-Journal $Adapter $Config $Journal
        Write-Terminal @{
            terminal = 'operator_recovery_required'
            reasons = @($Failure, 'target inactivity is not conclusively proven; source remains stopped')
            next_action = 'do not start the source; determine which host is active and inspect the stable route'
        } 2
    }

    # Cancel the target's replicated activation permission before restarting the
    # source. Without this second delivered generation, a target-local guard could
    # race compensation and start while the source is coming back.
    $cancelGeneration = [Math]::Max([int64]$Journal.generation + 1, [int64](Invoke-Adapter $Adapter 'Now'))
    $cancelIntent = @{ schema_version = 1; desired_host = $Config.host.id; generation = $cancelGeneration; advisory = $true }
    Write-OperationJson $Adapter $Config.state.intent_path $cancelIntent
    $cancelMarker = Invoke-Adapter $Adapter 'GetMarkerVersion' @($Config.state.intent_path)
    $Journal.phase = 'compensation_intent_written'
    $Journal.generation = $cancelGeneration
    $Journal.desired_host = $Config.host.id
    $Journal.marker_version = $cancelMarker.version
    Save-Journal $Adapter $Config $Journal
    $cancelDelivery = $null
    for ($index = 0; $index -lt $attempts; $index++) {
        try { $cancelDelivery = Invoke-Adapter $Adapter 'GetIntentDelivery' @($Config.syncthing, $cancelGeneration, $Config.state.intent_path) } catch { $cancelDelivery = @{ state = 'unknown'; generation = $null; marker_version = $null } }
        if ($cancelDelivery.state -eq 'delivered' -and [int64]$cancelDelivery.generation -eq $cancelGeneration -and $cancelDelivery.marker_version -eq $cancelMarker.version) { break }
        if ($index -lt ($attempts - 1) -and $delay -gt 0) { Invoke-Adapter $Adapter 'Sleep' @($delay) | Out-Null }
    }
    if ($cancelDelivery.state -ne 'delivered' -or [int64]$cancelDelivery.generation -ne $cancelGeneration -or $cancelDelivery.marker_version -ne $cancelMarker.version) {
        $Journal.phase = 'terminal'; $Journal.terminal = 'operator_recovery_required'; $Journal.failure = $Failure; Save-Journal $Adapter $Config $Journal
        Write-Terminal @{ terminal = 'operator_recovery_required'; reasons = @($Failure, 'source restart cancellation intent did not reach the target'); next_action = 'keep the source stopped; establish the desired host and exact generation on both machines' } 2
    }
    $target = Invoke-BoundedProbe $Adapter $Config.health.peer_url $attempts $delay
    try { $targetTunnel = Invoke-Adapter $Adapter 'GetTunnel' @($Config.tunnels.peer_name) } catch { $targetTunnel = @{ state = 'unknown' } }
    $targetInactive = -not $target.observed_ready -and ($target.state -eq 'inactive' -or ($target.state -eq 'unserved' -and $targetTunnel.state -eq 'connected'))
    if (-not $targetInactive) {
        $Journal.phase = 'terminal'; $Journal.terminal = 'operator_recovery_required'; $Journal.failure = $Failure; Save-Journal $Adapter $Config $Journal
        Write-Terminal @{ terminal = 'operator_recovery_required'; reasons = @($Failure, 'target became active or ambiguous after cancellation delivery'); next_action = 'keep the source stopped and determine which host is serving' } 2
    }

    $restored = $false
    $sourceStarted = $false
    try {
        Invoke-Adapter $Adapter 'SetRoute' @([string]$Journal.previous_route, $Config.tunnels.stable_hostname) | Out-Null
        $route = Invoke-Adapter $Adapter 'GetRoute' @($Config.cloudflare, $Config.tunnels)
        if (Test-ExpectedRoute $route ([string]$Journal.previous_route)) {
            Invoke-Adapter $Adapter 'StartService' @($Config.services.exomem) | Out-Null
            $sourceStarted = $true
            $local = Invoke-BoundedProbe $Adapter $Config.health.local_url $attempts $delay
            if ($local.state -eq 'ready' -and (Get-OptionalValue $local 'instance_id') -eq $Config.host.id) {
                $public = Test-PublicHost $Adapter $Config $Journal.identity $Config.host.id
                $restored = $public.ready
            }
        }
    } catch { $restored = $false }
    if ($sourceStarted -and -not $restored) {
        try { Invoke-Adapter $Adapter 'StopService' @($Config.services.exomem) | Out-Null } catch {}
    }
    $Journal.phase = 'terminal'
    $Journal.terminal = if ($restored) { 'rolled_back' } else { 'operator_recovery_required' }
    $Journal.failure = $Failure
    Save-Journal $Adapter $Config $Journal
    Write-Terminal @{
        terminal = $Journal.terminal
        reasons = @($Failure)
        next_action = if ($restored) { 'source was restored and verified; inspect intent before retrying' } else { 'keep the peer stopped; inspect route, source readiness, and journal before recovery' }
    } 2
}

function Invoke-HandoffTransition {
    param([hashtable]$Adapter, [hashtable]$Config, [hashtable]$Journal)
    $public = @{ state = 'unknown' }
    try {
        $service = Invoke-Adapter $Adapter 'GetService' @($Config.services.exomem)
        if ($service.state -eq 'running') {
            $Journal.phase = 'stopping_source'
            Save-Journal $Adapter $Config $Journal
            Invoke-Adapter $Adapter 'StopService' @($Config.services.exomem) | Out-Null
        } elseif ($service.state -ne 'stopped') {
            throw 'source service state is unknown during handoff replay'
        }
        $service = Invoke-Adapter $Adapter 'GetService' @($Config.services.exomem)
        if ($service.state -ne 'stopped') { throw 'source service did not stop conclusively' }
        $Journal.phase = 'source_stopped'
        Save-Journal $Adapter $Config $Journal

        $route = Invoke-Adapter $Adapter 'GetRoute' @($Config.cloudflare, $Config.tunnels)
        if (-not (Test-ExpectedRoute $route $Config.tunnels.peer_id)) {
            $Journal.phase = 'routing'
            Save-Journal $Adapter $Config $Journal
            Invoke-Adapter $Adapter 'SetRoute' @($Config.tunnels.peer_name, $Config.tunnels.stable_hostname) | Out-Null
        }
        $Journal.phase = 'route_changed'
        Save-Journal $Adapter $Config $Journal
        $route = Invoke-Adapter $Adapter 'GetRoute' @($Config.cloudflare, $Config.tunnels)
        if (-not (Test-ExpectedRoute $route $Config.tunnels.peer_id)) { throw 'Cloudflare DNS does not target the configured target tunnel ID' }
        $public = Invoke-BoundedProbe $Adapter $Config.health.stable_url ([int]$Config.operations.probe_attempts) ([int]$Config.operations.probe_delay_seconds)
        if ($public.state -eq 'ready' -and (Get-OptionalValue $public 'instance_id') -ne $Config.host.peer_id) { throw 'public readiness still identifies the previous source after route movement' }
        if ($public.state -in @('ambiguous', 'unknown')) { throw 'public route state is ambiguous after handoff' }
    } catch { Invoke-HandoffCompensation $Adapter $Config $Journal $_.Exception.Message }
    $Journal.phase = 'terminal'
    $Journal.terminal = if ($public.state -eq 'ready' -and (Get-OptionalValue $public 'instance_id') -eq $Config.host.peer_id) { 'handoff_complete' } else { 'handoff_pending_activation' }
    $Journal.Remove('identity')
    Save-Journal $Adapter $Config $Journal
    Write-Terminal @{
        terminal = $Journal.terminal
        outage = if ($Journal.terminal -eq 'handoff_pending_activation') { 'bounded until target-local guarded Activate succeeds' } else { 'target already verified ready' }
        next_action = 'activate the target locally; this command never starts a remote service'
    }
}

function Invoke-HandoffReplay {
    param([hashtable]$Adapter, [hashtable]$Config, [hashtable]$Evidence)
    if ($Evidence.journal.state -ne 'present') { return }
    $journal = $Evidence.journal.value
    if ($journal.action -ne 'Handoff') { return }
    if ($journal.desired_host -ne $Config.host.peer_id -and $journal.terminal -notin @('rolled_back')) {
        Write-Terminal @{ terminal = 'operator_recovery_required'; reasons = @('handoff journal target does not match this host peer'); next_action = 'keep both services stopped until intent and journal ownership are reconciled' } 2
    }
    if ($journal.terminal -in @('handoff_complete', 'handoff_pending_activation')) {
        $service = Invoke-Adapter $Adapter 'GetService' @($Config.services.exomem)
        $route = Invoke-Adapter $Adapter 'GetRoute' @($Config.cloudflare, $Config.tunnels)
        if ($service.state -eq 'stopped' -and (Test-ExpectedRoute $route $Config.tunnels.peer_id)) {
            Write-Terminal @{ terminal = 'already_handed_off'; route_target = $route.target; next_action = 'activate the target locally if it is not already ready' }
        }
        Write-Terminal @{ terminal = 'operator_recovery_required'; reasons = @('completed handoff journal disagrees with the observed service or route'); next_action = 'do not start either service until the stable route and both direct endpoints are known' } 2
    }
    if ($journal.terminal -in @('rolled_back', 'operator_recovery_required')) { return }

    $replayable = @('intent_writing', 'intent_written', 'intent_delivered', 'stopping_source', 'source_stopped', 'routing', 'route_changed')
    if ($journal.phase -notin $replayable -and $journal.terminal -ne 'intent_delivery_unproven') { return }
    if ($journal.phase -eq 'intent_writing') {
        $intent = Read-JsonEvidence $Adapter $Config.state.intent_path
        $intent.state = Test-Intent $intent $Config
        if ($intent.state -ne 'present' -or [int64]$intent.value.generation -ne [int64]$journal.generation -or $intent.value.desired_host -ne $journal.desired_host) {
            $journal.phase = 'terminal'; $journal.terminal = 'interrupted_before_intent'; Save-Journal $Adapter $Config $journal
            Write-Terminal @{ terminal = 'interrupted_before_intent'; reasons = @('handoff stopped before a matching target intent was durably written'); next_action = 'rerun Handoff; the source and route were not intentionally changed' } 2
        }
        $marker = Invoke-Adapter $Adapter 'GetMarkerVersion' @($Config.state.intent_path)
        $journal.marker_version = $marker.version
        $journal.phase = 'intent_written'
        $journal.terminal = $null
        Save-Journal $Adapter $Config $journal
    }
    $delivery = Confirm-ExactIntentDelivery $Adapter $Config $journal
    if (-not $delivery.delivered) {
        $journal.phase = 'intent_written'; $journal.terminal = 'intent_delivery_unproven'; Save-Journal $Adapter $Config $journal
        Write-Terminal @{ terminal = 'intent_delivery_unproven'; reasons = @($delivery.reason); next_action = 'leave the source serving; restore replication, then rerun Handoff to retry this exact generation' } 2
    }
    $journal.phase = 'intent_delivered'
    $journal.terminal = $null
    Save-Journal $Adapter $Config $journal

    $service = Invoke-Adapter $Adapter 'GetService' @($Config.services.exomem)
    $route = Invoke-Adapter $Adapter 'GetRoute' @($Config.cloudflare, $Config.tunnels)
    if ($service.state -eq 'running' -and (Test-ExpectedRoute $route $Config.tunnels.peer_id)) {
        $journal.phase = 'terminal'; $journal.terminal = 'operator_recovery_required'; Save-Journal $Adapter $Config $journal
        Write-Terminal @{ terminal = 'operator_recovery_required'; reasons = @('source is running while the stable route selects the target'); next_action = 'do not start the target; inspect both direct endpoints and choose one source of truth' } 2
    }
    if ($service.state -notin @('running', 'stopped') -or $route.state -ne 'known') {
        $journal.phase = 'terminal'; $journal.terminal = 'operator_recovery_required'; Save-Journal $Adapter $Config $journal
        Write-Terminal @{ terminal = 'operator_recovery_required'; reasons = @('interrupted handoff service or route state is unknown'); next_action = 'keep both services stopped until the route and direct endpoints are known' } 2
    }
    Invoke-HandoffTransition $Adapter $Config $journal
}

function Invoke-NativeCommand {
    param([string]$Executable, [object[]]$Arguments)
    $startInfo = [Diagnostics.ProcessStartInfo]::new()
    $startInfo.FileName = $Executable
    $startInfo.UseShellExecute = $false
    $startInfo.CreateNoWindow = $true
    $startInfo.RedirectStandardOutput = $true
    $startInfo.RedirectStandardError = $true
    foreach ($argument in $Arguments) { [void]$startInfo.ArgumentList.Add([string]$argument) }
    $process = [Diagnostics.Process]::new()
    $process.StartInfo = $startInfo
    if (-not $process.Start()) { throw "$Executable could not be started." }
    if (-not $process.WaitForExit(15000)) {
        try { $process.Kill($true) } catch {}
        throw "$Executable timed out after 15 seconds."
    }
    $stdout = $process.StandardOutput.ReadToEnd()
    $stderr = $process.StandardError.ReadToEnd()
    if ($process.ExitCode -ne 0) { throw "$Executable failed with exit code $($process.ExitCode)." }
    if ([string]::IsNullOrEmpty($stdout)) { return @() }
    return @($stdout)
}

function Get-ManagedTunnelText {
    param([string]$Existing, [hashtable]$Policy)
    $begin = '  # exomem-cold-standby:begin'
    $end = '  # exomem-cold-standby:end'
    $without = [regex]::Replace($Existing, "(?ms)^\s*# exomem-cold-standby:begin\r?\n.*?^\s*# exomem-cold-standby:end\r?\n?", '')
    $hostnamePattern = [regex]::Escape([string]$Policy.operational_hostname)
    $stablePattern = [regex]::Escape([string]$Policy.stable_hostname)
    if ($without -match "(?m)^\s*-\s*hostname:\s*(?:$hostnamePattern|$stablePattern)\s*$") { throw 'Existing unmanaged ingress already claims a cold-standby hostname; reconcile it manually.' }
    $catchall = [regex]::Match($without, '(?m)^\s*-\s*service:\s*http_status:404\s*$')
    if (-not $catchall.Success) { throw 'cloudflared config requires a final http_status:404 catch-all.' }
    $block = @"
$begin
  - hostname: $($Policy.stable_hostname)
    service: $($Policy.origin_service_url)
  - hostname: $($Policy.operational_hostname)
    path: ^/health(?:/ready)?$
    service: $($Policy.origin_service_url)
  - hostname: $($Policy.operational_hostname)
    service: http_status:404
$end
"@
    return $without.Insert($catchall.Index, "$block`r`n")
}

function Assert-TunnelPolicyRouting {
    param([string]$ConfigFile, [hashtable]$Policy)
    $checks = @(
        @{ url = "https://$($Policy.operational_hostname)/health"; expected = "service: $($Policy.origin_service_url)" },
        @{ url = "https://$($Policy.operational_hostname)/health/ready"; expected = "service: $($Policy.origin_service_url)" },
        @{ url = "https://$($Policy.operational_hostname)/mcp"; expected = 'service: http_status:404' },
        @{ url = "https://$($Policy.operational_hostname)/.well-known/oauth-authorization-server"; expected = 'service: http_status:404' },
        @{ url = "https://$($Policy.operational_hostname)/api/write"; expected = 'service: http_status:404' },
        @{ url = "https://$($Policy.operational_hostname)/artifacts/item"; expected = 'service: http_status:404' },
        @{ url = "https://$($Policy.stable_hostname)/mcp"; expected = "service: $($Policy.origin_service_url)" }
    )
    foreach ($check in $checks) {
        $matched = (Invoke-NativeCommand 'cloudflared' @('tunnel', '--config', $ConfigFile, 'ingress', 'rule', $check.url)) -join "`n"
        if ($matched -notmatch [regex]::Escape([string]$check.expected)) { throw "cloudflared ingress policy does not safely route $($check.url)." }
    }
    return $true
}

function Get-NativeMarkerVersion {
    param([string]$IntentPath)
    $bytes = [IO.File]::ReadAllBytes($IntentPath)
    $intent = [Text.Encoding]::UTF8.GetString($bytes) | ConvertFrom-Json -AsHashtable
    $file = Get-Item -LiteralPath $IntentPath -ErrorAction Stop
    $unixTicks = $file.LastWriteTimeUtc.Ticks - [DateTime]::UnixEpoch.Ticks
    $seconds = [int64][Math]::Floor($unixTicks / [TimeSpan]::TicksPerSecond)
    $nanoseconds = [int64](($unixTicks % [TimeSpan]::TicksPerSecond) * 100)
    $hash = [Convert]::ToHexString([Security.Cryptography.SHA256]::HashData($bytes)).ToLowerInvariant()
    @{ generation = [int64]$intent.generation; version = "$($intent.generation):$seconds`:$nanoseconds`:$($file.Length)`:$hash"; modified_s = $seconds; modified_ns = $nanoseconds; size = [int64]$file.Length }
}

function Get-NativeAdapter {
    @{
        Now = { [DateTimeOffset]::UtcNow.ToUnixTimeMilliseconds() }
        Sleep = { param($Seconds) if ([int]$Seconds -gt 0) { Start-Sleep -Seconds ([int]$Seconds) } }
        GetService = { param($Name) try { $service = Get-Service -Name $Name -ErrorAction Stop; @{ state = $service.Status.ToString().ToLowerInvariant() } } catch { @{ state = 'unknown' } } }
        GetServiceEnvironment = {
            param($Name)
            try {
                $parameters = Get-ItemProperty -Path "HKLM:\SYSTEM\CurrentControlSet\Services\$Name\Parameters" -ErrorAction Stop
                if (-not ($parameters.PSObject.Properties.Name -contains 'AppEnvironmentExtra')) { return @{ state = 'unknown'; values = @{} } }
                $values = @{}
                foreach ($entry in @($parameters.AppEnvironmentExtra)) {
                    $parts = ([string]$entry) -split '=', 2
                    if ($parts.Count -eq 2 -and $parts[0]) { $values[$parts[0]] = $parts[1] }
                }
                @{ state = 'known'; values = $values }
            } catch { @{ state = 'unknown'; values = @{} } }
        }
        StartService = { param($Name) Start-Service -Name $Name -ErrorAction Stop; $true }
        StopService = { param($Name) Stop-Service -Name $Name -ErrorAction Stop; $true }
        SetDemandStart = { param($Name) Set-Service -Name $Name -StartupType Manual -ErrorAction Stop; $true }
        InspectDesktopTask = {
            param($Command, $DelaySeconds)
            try {
                $task = Get-ScheduledTask -TaskName 'Exomem Cold Standby Recovery' -TaskPath '\Exomem\' -ErrorAction Stop
                $principalName = if ($env:USERDOMAIN) { "$env:USERDOMAIN\$env:USERNAME" } else { $env:USERNAME }
                $taskAction = @($task.Actions)[0]
                $trigger = @($task.Triggers)[0]
                $triggerClass = [string]$trigger.CimClass.CimClassName
                $correct = @($task.Actions).Count -eq 1 -and @($task.Triggers).Count -eq 1 -and
                    ([IO.Path]::GetFileName([string]$taskAction.Execute) -in @('pwsh', 'pwsh.exe')) -and
                    [string]$taskAction.Arguments -eq $Command -and
                    $triggerClass -eq 'MSFT_TaskLogonTrigger' -and
                    [string]$trigger.UserId -eq $principalName -and
                    [string]$trigger.Delay -eq "PT$([int]$DelaySeconds)S" -and
                    [string]$task.Principal.UserId -eq $principalName -and
                    [string]$task.Principal.RunLevel -eq 'Limited' -and
                    [bool]$task.Settings.RunOnlyIfNetworkAvailable
                @{ state = if ($correct) { 'ready' } else { 'needs_update' } }
            } catch { @{ state = 'needs_update' } }
        }
        RegisterDesktopTask = {
            param($Command, $DelaySeconds)
            $taskAction = New-ScheduledTaskAction -Execute 'pwsh.exe' -Argument $Command
            $principalName = if ($env:USERDOMAIN) { "$env:USERDOMAIN\$env:USERNAME" } else { $env:USERNAME }
            $trigger = New-ScheduledTaskTrigger -AtLogOn -User $principalName
            $trigger.Delay = "PT$([int]$DelaySeconds)S"
            $settings = New-ScheduledTaskSettingsSet -RunOnlyIfNetworkAvailable -StartWhenAvailable
            Register-ScheduledTask -TaskName 'Exomem Cold Standby Recovery' -TaskPath '\Exomem\' -Action $taskAction -Trigger $trigger -Settings $settings -User $principalName -RunLevel Limited -Force | Out-Null
            $true
        }
        InspectTunnelConfig = {
            param($Policy)
            try {
                if (-not (Test-Path -LiteralPath $Policy.config_path -PathType Leaf)) { return @{ state = 'unavailable' } }
                $existing = Get-Content -Raw -LiteralPath $Policy.config_path
                $expected = Get-ManagedTunnelText $existing $Policy
                if ($expected -ceq $existing) { Assert-TunnelPolicyRouting $Policy.config_path $Policy | Out-Null; return @{ state = 'ready' } }
                @{ state = 'needs_update' }
            } catch { @{ state = 'invalid'; remediation = $_.Exception.Message } }
        }
        InspectTunnelServiceBinding = {
            param($Name, $Policy)
            try {
                $service = Get-CimInstance -ClassName Win32_Service -Filter "Name='$($Name.Replace("'", "''"))'" -ErrorAction Stop
                $commandLine = [string]$service.PathName
                $tokens = @([regex]::Matches($commandLine, '"[^"]*"|\S+') | ForEach-Object { $_.Value.Trim('"') })
                $configuredPath = $null
                for ($index = 0; $index -lt $tokens.Count; $index++) {
                    if ($tokens[$index] -eq '--config' -and ($index + 1) -lt $tokens.Count) { $configuredPath = $tokens[$index + 1]; break }
                    if ($tokens[$index] -like '--config=*') { $configuredPath = $tokens[$index].Substring(9); break }
                }
                if ([string]::IsNullOrWhiteSpace($configuredPath)) { return @{ state = 'wrong_config'; command_fingerprint = Get-Sha256 $commandLine } }
                $configuredPath = [IO.Path]::GetFullPath([Environment]::ExpandEnvironmentVariables($configuredPath))
                if ($configuredPath -ne [IO.Path]::GetFullPath([string]$Policy.config_path)) { return @{ state = 'wrong_config'; command_fingerprint = Get-Sha256 $commandLine } }
                $tunnelIndex = [Array]::IndexOf($tokens, 'tunnel')
                $runIndex = [Array]::IndexOf($tokens, 'run')
                if ($tunnelIndex -lt 0 -or $runIndex -lt 0 -or $runIndex -lt $tunnelIndex) { return @{ state = 'wrong_command'; command_fingerprint = Get-Sha256 $commandLine } }
                $selectedByCommand = @($tokens | Where-Object { $_ -eq [string]$Policy.local_id -or $_ -eq [string]$Policy.local_name }).Count -gt 0
                $configText = Get-Content -Raw -LiteralPath $Policy.config_path
                $escapedId = [regex]::Escape([string]$Policy.local_id)
                $tunnelPattern = '(?mi)^\s*tunnel\s*:\s*[''"]?' + $escapedId + '[''"]?\s*$'
                $selectedByConfig = $configText -match $tunnelPattern
                if (-not $selectedByCommand -and -not $selectedByConfig) { return @{ state = 'wrong_tunnel'; command_fingerprint = Get-Sha256 $commandLine } }
                @{ state = 'ready'; command_fingerprint = Get-Sha256 $commandLine }
            } catch { @{ state = 'unknown'; command_fingerprint = $null } }
        }
        PrepareTunnelConfig = {
            param($Policy)
            $path = [string]$Policy.config_path
            $existing = Get-Content -Raw -LiteralPath $path
            $updated = Get-ManagedTunnelText $existing $Policy
            if ($updated -ceq $existing) { return $true }
            $temporary = "$path.exomem-cold-standby.$([guid]::NewGuid().ToString('N')).tmp"
            $backup = "$path.exomem-cold-standby.$([DateTime]::UtcNow.ToString('yyyyMMddHHmmss')).bak"
            try {
                $updated | Set-Content -LiteralPath $temporary -Encoding utf8
                Invoke-NativeCommand 'cloudflared' @('--config', $temporary, 'tunnel', 'ingress', 'validate') | Out-Null
                Assert-TunnelPolicyRouting $temporary $Policy | Out-Null
                Copy-Item -LiteralPath $path -Destination $backup -ErrorAction Stop
                Move-Item -LiteralPath $temporary -Destination $path -Force -ErrorAction Stop
            } finally {
                if (Test-Path -LiteralPath $temporary) { Remove-Item -LiteralPath $temporary -Force }
            }
            $true
        }
        ReloadTunnelService = {
            param($Name)
            Restart-Service -Name $Name -ErrorAction Stop
            $service = Get-Service -Name $Name -ErrorAction Stop
            $service.WaitForStatus([ServiceProcess.ServiceControllerStatus]::Running, [TimeSpan]::FromSeconds(15))
            $true
        }
        Probe = {
            param($Url)
            try {
                $response = Invoke-WebRequest -Uri $Url -Method Get -TimeoutSec 10 -SkipHttpErrorCheck -ErrorAction Stop
                if ($response.StatusCode -eq 503) { return @{ state = 'inactive'; status_code = 503 } }
                if ($response.StatusCode -eq 502) { return @{ state = 'unserved'; status_code = 502 } }
                if ($response.StatusCode -ne 200) { return @{ state = 'unknown'; status_code = $response.StatusCode } }
                $payload = $response.Content | ConvertFrom-Json -AsHashtable
                if ($payload.status -eq 'ready') { return @{ state = 'ready'; status_code = 200; instance_id = $payload.instance_id } }
                if ($payload.status -eq 'not_ready') { return @{ state = 'inactive'; status_code = 200; instance_id = Get-OptionalValue $payload 'instance_id' } }
                @{ state = 'unknown'; status_code = 200 }
            } catch { @{ state = 'unreachable' } }
        }
        ProbeOAuth = {
            param($Url)
            try {
                $response = Invoke-WebRequest -Uri $Url -Method Get -TimeoutSec 10 -SkipHttpErrorCheck -ErrorAction Stop
                if ($response.StatusCode -ne 200) { return @{ state = 'unknown' } }
                $payload = $response.Content | ConvertFrom-Json -AsHashtable
                if ([string]::IsNullOrWhiteSpace([string]$payload.issuer)) { return @{ state = 'unknown' } }
                @{ state = 'ready'; issuer = [string]$payload.issuer }
            } catch { @{ state = 'unreachable' } }
        }
        GetSyncthing = {
            param($Syncthing)
            try {
                $apiKey = [Environment]::GetEnvironmentVariable([string]$Syncthing.api_key_environment)
                if ([string]::IsNullOrWhiteSpace($apiKey)) { return @{ api = 'unavailable' } }
                $headers = @{ 'X-API-Key' = $apiKey }
                $folder = [Uri]::EscapeDataString([string]$Syncthing.folder_id)
                $device = [Uri]::EscapeDataString([string]$Syncthing.peer_device_id)
                $status = Invoke-RestMethod -Uri "$($Syncthing.api_url.TrimEnd('/'))/rest/db/status?folder=$folder" -Headers $headers -TimeoutSec 10 -ErrorAction Stop
                $completion = Invoke-RestMethod -Uri "$($Syncthing.api_url.TrimEnd('/'))/rest/db/completion?folder=$folder&device=$device" -Headers $headers -TimeoutSec 10 -ErrorAction Stop
                @{
                    api = 'reachable'
                    folder_state = [string]$status.state
                    pending_items = [int64]$status.needItems
                    pending_bytes = [int64]$status.needBytes
                    peer_completion = [double]$completion.completion
                    peer_complete = ([double]$completion.completion -ge 100 -and [int64]$completion.needItems -eq 0 -and [int64]$completion.needBytes -eq 0)
                }
            } catch { @{ api = 'unavailable'; folder_state = 'unknown'; pending_items = $null; pending_bytes = $null; peer_completion = $null; peer_complete = $null } }
        }
        GetMarkerVersion = {
            param($IntentPath)
            Get-NativeMarkerVersion $IntentPath
        }
        GetIntentDelivery = {
            param($Syncthing, $Generation, $IntentPath)
            try {
                $markerVersion = Get-NativeMarkerVersion $IntentPath
                if ([int64]$markerVersion.generation -ne [int64]$Generation) { return @{ state = 'unknown'; generation = $null; marker_version = $null } }
                $apiKey = [Environment]::GetEnvironmentVariable([string]$Syncthing.api_key_environment)
                if ([string]::IsNullOrWhiteSpace($apiKey)) { return @{ state = 'unknown'; generation = $null; marker_version = $null } }
                $headers = @{ 'X-API-Key' = $apiKey }
                $folder = [Uri]::EscapeDataString([string]$Syncthing.folder_id)
                $device = [Uri]::EscapeDataString([string]$Syncthing.peer_device_id)
                $markerPath = [Uri]::EscapeDataString([string]$Syncthing.intent_relative_path)
                $file = Invoke-RestMethod -Uri "$($Syncthing.api_url.TrimEnd('/'))/rest/db/file?folder=$folder&file=$markerPath" -Headers $headers -TimeoutSec 10 -ErrorAction Stop
                if ($null -eq $file.local) { return @{ state = 'pending'; generation = $null; marker_version = $null } }
                $localProperties = @($file.local.PSObject.Properties.Name)
                if ('deleted' -in $localProperties -and $file.local.deleted) { return @{ state = 'pending'; generation = $null; marker_version = $null } }
                if ('invalid' -in $localProperties -and $file.local.invalid) { return @{ state = 'pending'; generation = $null; marker_version = $null } }
                if ('modified' -notin $localProperties -or 'version' -notin $localProperties -or 'size' -notin $localProperties) { return @{ state = 'unknown'; generation = $null; marker_version = $null } }
                $modifiedText = [string]$file.local.modified
                try { $modifiedTime = [DateTimeOffset]::Parse($modifiedText, [Globalization.CultureInfo]::InvariantCulture, [Globalization.DateTimeStyles]::RoundtripKind) } catch { return @{ state = 'unknown'; generation = $null; marker_version = $null } }
                $fractionMatch = [regex]::Match($modifiedText, '\.(?<fraction>[0-9]+)(?:Z|[+-][0-9]{2}:[0-9]{2})$')
                $modifiedNs = if ($fractionMatch.Success) { [int64]($fractionMatch.Groups['fraction'].Value.PadRight(9, '0').Substring(0, 9)) } else { 0 }
                # PowerShell DateTime ticks resolve to 100 ns even when Syncthing
                # reports a Linux filesystem's full nanosecond timestamp.
                $modifiedNsAtPowerShellPrecision = $modifiedNs - ($modifiedNs % 100)
                if ([int64]$modifiedTime.ToUnixTimeSeconds() -ne [int64]$markerVersion.modified_s -or $modifiedNsAtPowerShellPrecision -ne [int64]$markerVersion.modified_ns -or [int64]$file.local.size -ne [int64]$markerVersion.size) { return @{ state = 'pending'; generation = $null; marker_version = $null } }
                $globalProperties = if ($null -ne $file.global) { @($file.global.PSObject.Properties.Name) } else { @() }
                if ('version' -notin $globalProperties) { return @{ state = 'pending'; generation = $null; marker_version = $null } }
                $localVector = (@($file.local.version) | ForEach-Object { [string]$_ }) -join ','
                $globalVector = (@($file.global.version) | ForEach-Object { [string]$_ }) -join ','
                if ([string]::IsNullOrWhiteSpace($localVector) -or $localVector -ne $globalVector) { return @{ state = 'pending'; generation = $null; marker_version = $null } }
                $completion = Invoke-RestMethod -Uri "$($Syncthing.api_url.TrimEnd('/'))/rest/db/completion?folder=$folder&device=$device" -Headers $headers -TimeoutSec 10 -ErrorAction Stop
                $remoteNeed = Invoke-RestMethod -Uri "$($Syncthing.api_url.TrimEnd('/'))/rest/db/remoteneed?folder=$folder&device=$device&page=1&perpage=100" -Headers $headers -TimeoutSec 10 -ErrorAction Stop
                $remoteProperties = @($remoteNeed.PSObject.Properties.Name)
                $needed = @(if ('items' -in $remoteProperties) { @($remoteNeed.items) }) + @(if ('files' -in $remoteProperties) { @($remoteNeed.files) })
                $markerNeeded = @($needed | Where-Object {
                    $properties = @($_.PSObject.Properties.Name)
                    $neededPath = if ('name' -in $properties) { [string]$_.name } elseif ('path' -in $properties) { [string]$_.path } else { '' }
                    $neededPath -eq [string]$Syncthing.intent_relative_path
                }).Count -gt 0
                if ([double]$completion.completion -ge 100 -and [int64]$completion.needItems -eq 0 -and [int64]$completion.needBytes -eq 0 -and -not $markerNeeded) { @{ state = 'delivered'; generation = [int64]$Generation; marker_version = $markerVersion.version; syncthing_version = $localVector } } else { @{ state = 'pending'; generation = $null; marker_version = $null } }
            } catch { @{ state = 'unknown'; generation = $null; marker_version = $null } }
        }
        GetTunnel = {
            param($Name)
            try {
                $output = Invoke-NativeCommand 'cloudflared' @('tunnel', 'info', $Name, '--output', 'json')
                $payload = ($output -join "`n") | ConvertFrom-Json -AsHashtable
                $connections = if ($payload.ContainsKey('connections')) { @($payload.connections) } else { @() }
                @{ state = if ($connections.Count) { 'connected' } else { 'disconnected' } }
            } catch { @{ state = 'unknown' } }
        }
        GetRoute = {
            param($Cloudflare, $Tunnels)
            try {
                $token = [Environment]::GetEnvironmentVariable([string]$Cloudflare.api_token_environment)
                $zone = [Environment]::GetEnvironmentVariable([string]$Cloudflare.zone_id_environment)
                if ([string]::IsNullOrWhiteSpace($token) -or [string]::IsNullOrWhiteSpace($zone)) { return @{ state = 'unknown'; tunnel_id = $null; target = $null } }
                $name = [Uri]::EscapeDataString([string]$Tunnels.stable_hostname)
                $response = Invoke-RestMethod -Uri "https://api.cloudflare.com/client/v4/zones/$zone/dns_records?type=CNAME&name=$name" -Headers @{ Authorization = "Bearer $token" } -TimeoutSec 10 -ErrorAction Stop
                if (-not $response.success -or @($response.result).Count -ne 1) { return @{ state = 'unknown'; tunnel_id = $null; target = $null } }
                $target = ([string]$response.result[0].content).TrimEnd('.').ToLowerInvariant()
                $match = [regex]::Match($target, '^([0-9a-f-]{36})\.cfargotunnel\.com$')
                if (-not $match.Success) { return @{ state = 'unknown'; tunnel_id = $null; target = $target } }
                @{ state = 'known'; tunnel_id = $match.Groups[1].Value; target = $target }
            } catch { @{ state = 'unknown'; tunnel_id = $null; target = $null } }
        }
        SetRoute = {
            param($Tunnel, $Hostname)
            Invoke-NativeCommand 'cloudflared' @('tunnel', 'route', 'dns', '--overwrite-dns', $Tunnel, $Hostname) | Out-Null
            $true
        }
        WriteJson = {
            param($Path, $Value)
            $parent = Split-Path -Parent $Path
            New-Item -ItemType Directory -Force -Path $parent | Out-Null
            $temporary = "$Path.$([guid]::NewGuid().ToString('N')).tmp"
            try {
                $Value | ConvertTo-Json -Depth 20 | Set-Content -LiteralPath $temporary -Encoding utf8
                Move-Item -LiteralPath $temporary -Destination $Path -Force -ErrorAction Stop
            } finally { if (Test-Path -LiteralPath $temporary) { Remove-Item -LiteralPath $temporary -Force } }
            $true
        }
        ReadJson = { param($Path) if (Test-Path -LiteralPath $Path) { Get-Content -Raw -LiteralPath $Path | ConvertFrom-Json -AsHashtable } else { $null } }
        GetTrace = { @() }
    }
}

try {
    $config = Get-Config $ConfigPath
    $adapter = if ($AdapterPath) { . $AdapterPath } else { Get-NativeAdapter }
    if ($adapter -isnot [hashtable]) { throw 'Adapter must return a hashtable.' }
    $script:ActiveAdapter = $adapter

    if ($Action -eq 'Configure') {
        $identity = Get-IdentityContract $config $adapter
        $policy = Get-TunnelPolicy $config
        $tunnelInspection = Invoke-Adapter $adapter 'InspectTunnelConfig' @($policy)
        if ($tunnelInspection.state -notin @('ready', 'needs_update')) { Write-Terminal @{ terminal = 'refused'; reasons = @('tunnel configuration is unavailable or unsafe to update'); next_action = 'restore a valid config with a final 404 catch-all' } 2 }
        $serviceBinding = Invoke-Adapter $adapter 'InspectTunnelServiceBinding' @($config.services.cloudflared, $policy)
        if ($serviceBinding.state -ne 'ready') { Write-Terminal @{ terminal = 'refused'; reasons = @('cloudflared Windows service is not bound to the configured file and local tunnel'); next_action = 'reinstall or correct the cloudflared service command, then rerun Configure' } 2 }
        $taskCommand = "-NoProfile -NonInteractive -File `"$PSCommandPath`" Activate -IfUnserved -ConfigPath `"$ConfigPath`""
        $taskInspection = if ($config.host.role -eq 'desktop') { Invoke-Adapter $adapter 'InspectDesktopTask' @($taskCommand, [int]$config.operations.desktop_logon_delay_seconds) } else { @{ state = 'not_applicable' } }
        if ($config.host.role -eq 'desktop' -and $taskInspection.state -notin @('ready', 'needs_update')) { Write-Terminal @{ terminal = 'refused'; reasons = @('desktop scheduled-task state is unknown'); next_action = 'inspect the current-user scheduled task' } 2 }
        $plan = @('set-exomem-demand-start', 'prepare-health-only-tunnel-ingress', 'reload-cloudflared-if-config-changed', 'publish-redacted-identity-manifest')
        if ($config.host.role -eq 'desktop') { $plan += 'register-current-user-delayed-activate-if-unserved' }
        if ($WhatIfPreference) { Write-Terminal @{ terminal = 'what_if'; plan = $plan; next_action = 'review the plan, then rerun without -WhatIf' } }
        Invoke-Adapter $adapter 'SetDemandStart' @($config.services.exomem) | Out-Null
        if ($tunnelInspection.state -eq 'needs_update') {
            Invoke-Adapter $adapter 'PrepareTunnelConfig' @($policy) | Out-Null
            Invoke-Adapter $adapter 'ReloadTunnelService' @($config.services.cloudflared) | Out-Null
            $connected = $false
            for ($index = 0; $index -lt [int]$config.operations.probe_attempts; $index++) {
                $tunnel = Invoke-Adapter $adapter 'GetTunnel' @($config.tunnels.local_name)
                if ($tunnel.state -eq 'connected') { $connected = $true; break }
                if ($index -lt ([int]$config.operations.probe_attempts - 1) -and [int]$config.operations.probe_delay_seconds -gt 0) { Invoke-Adapter $adapter 'Sleep' @([int]$config.operations.probe_delay_seconds) | Out-Null }
            }
            if (-not $connected) { Write-Terminal @{ terminal = 'refused'; reasons = @('cloudflared did not reconnect to the configured local tunnel after reload'); next_action = 'inspect the Windows service command and tunnel logs; direct ingress may not be hardened live' } 2 }
        }
        Write-OperationJson $adapter $config.state.manifest_path $identity.manifest
        if ($config.host.role -eq 'desktop' -and $taskInspection.state -eq 'needs_update') { Invoke-Adapter $adapter 'RegisterDesktopTask' @($taskCommand, [int]$config.operations.desktop_logon_delay_seconds) | Out-Null }
        Write-Terminal @{
            terminal = 'configured'
            tunnel_policy = @{ stable = 'forward-all'; direct = @('/health', '/health/ready'); direct_fallback = 'http_status:404' }
            next_action = 'configure the peer, converge Syncthing, then run Status and dry runs'
        }
    }

    $evidence = Get-Evidence $config $adapter

    if ($Action -eq 'Status') {
        $reasons = @(Get-StatusReasons $evidence $config)
        $localInactive = $evidence.local_health.state -eq 'inactive' -or ($evidence.local_health.state -eq 'unserved' -and $evidence.local_tunnel.state -eq 'connected')
        $peerInactive = $evidence.peer_health.state -eq 'inactive' -or ($evidence.peer_health.state -eq 'unserved' -and $evidence.peer_tunnel.state -eq 'connected')
        $active = if ($evidence.local_health.observed_ready -and $evidence.peer_health.observed_ready) {
            'unsafe_ambiguous'
        } elseif ($evidence.local_health.state -eq 'ready' -and $peerInactive -and $evidence.stable_health.state -eq 'ready' -and (Get-OptionalValue $evidence.stable_health 'instance_id') -eq $config.host.id -and (Test-ExpectedRoute $evidence.route $config.tunnels.local_id)) {
            $config.host.id
        } elseif ($evidence.peer_health.state -eq 'ready' -and $localInactive -and $evidence.stable_health.state -eq 'ready' -and (Get-OptionalValue $evidence.stable_health 'instance_id') -eq $config.host.peer_id -and (Test-ExpectedRoute $evidence.route $config.tunnels.peer_id)) {
            $config.host.peer_id
        } else { 'unknown' }
        $next = if ($active -eq $config.host.id) { 'none: local host is serving' } elseif ($active -eq $config.host.peer_id) { 'none: peer is serving' } else { 'none: resolve unknown or unsafe evidence' }
        $publicEvidence = @{
            service = $evidence.service
            local_health = $evidence.local_health
            peer_health = $evidence.peer_health
            stable_health = $evidence.stable_health
            syncthing = $evidence.syncthing
            local_tunnel = $evidence.local_tunnel
            peer_tunnel = $evidence.peer_tunnel
            route = $evidence.route
            intent = @{ state = $evidence.intent.state; desired_host = if ($evidence.intent.state -eq 'present') { $evidence.intent.value.desired_host } else { $null }; generation = if ($evidence.intent.state -eq 'present') { $evidence.intent.value.generation } else { $null } }
            journal = @{ state = $evidence.journal.state; phase = if ($evidence.journal.state -eq 'present') { Get-OptionalValue $evidence.journal.value 'phase' } else { $null }; terminal = if ($evidence.journal.state -eq 'present') { Get-OptionalValue $evidence.journal.value 'terminal' } else { $null } }
            identity = @{ state = $evidence.identity.state; fingerprints = if ($evidence.identity.state -eq 'available') { $evidence.identity.manifest.fingerprints } else { $null } }
        }
        Write-Terminal @{ terminal = 'status'; active_host = $active; evidence = $publicEvidence; reasons = $reasons; next_action = $next }
    }

    if ($Action -eq 'Activate') {
        if ($Force -and $Acknowledge -ne $ForceAcknowledgement) { Write-Terminal @{ terminal = 'refused'; reasons = @("-Force requires the exact acknowledgement: $ForceAcknowledgement"); next_action = 'supply both inputs only after accepting split-brain and data-loss risk' } 2 }
        if (-not $Force -and -not [string]::IsNullOrWhiteSpace($Acknowledge)) { Write-Terminal @{ terminal = 'refused'; reasons = @('the risk acknowledgement has no effect without -Force'); next_action = 'remove -Acknowledge or use the complete disaster override' } 2 }
        if (-not $WhatIfPreference) { Invoke-ActivationReplay $adapter $config $evidence }
        $reasons = @(Get-ActivationReasons $evidence $config -GuardedBoot:$IfUnserved)
        $bypassable = @('Syncthing is not converged', 'peer inactivity is unproven', 'desired-host intent names peer')
        $bypassed = @()
        if ($Force) {
            $bypassed = @($reasons | Where-Object { $_ -in $bypassable })
            $reasons = @($reasons | Where-Object { $_ -notin $bypassable })
        }
        if ($reasons.Count) { Write-Terminal @{ terminal = 'refused'; reasons = $reasons; bypassed_guards = $bypassed; next_action = 'resolve each named guard; peer unreachability is not proof of shutdown' } 2 }
        $plan = @('write-desired-host-intent', 'start-local-service', 'verify-local-instance', 'route-stable-hostname', 'verify-cloudflare-target', 'verify-public-instance-and-oauth', 'commit-journal')
        if ($WhatIfPreference) { Write-Terminal @{ terminal = 'what_if'; plan = $plan; bypassed_guards = $bypassed; next_action = 'review the plan, then rerun without -WhatIf' } }
        $priorRoute = $evidence.route
        $generation = Get-NextGeneration $adapter $evidence.intent
        $intent = @{ schema_version = 1; desired_host = $config.host.id; generation = $generation; advisory = $true }
        $journal = @{
            action = 'Activate'; phase = 'intent_written'; generation = $generation; desired_host = $config.host.id
            previous_route = if ($priorRoute.state -eq 'known') { $priorRoute.tunnel_id } else { $null }
            started_service = $false; terminal = $null; bypassed_guards = @($bypassed)
        }
        Write-OperationJson $adapter $config.state.intent_path $intent
        Save-Journal $adapter $config $journal
        try {
            $journal.started_service = $true
            $journal.phase = 'starting_service'
            Save-Journal $adapter $config $journal
            Invoke-Adapter $adapter 'StartService' @($config.services.exomem) | Out-Null
            $journal.phase = 'service_started'
            Save-Journal $adapter $config $journal
            $local = Invoke-BoundedProbe $adapter $config.health.local_url ([int]$config.operations.probe_attempts) ([int]$config.operations.probe_delay_seconds)
            if ($local.state -ne 'ready' -or (Get-OptionalValue $local 'instance_id') -ne $config.host.id) { throw 'local runtime readiness did not report the configured instance_id' }
            $journal.phase = 'routing'
            Save-Journal $adapter $config $journal
            Invoke-Adapter $adapter 'SetRoute' @($config.tunnels.local_name, $config.tunnels.stable_hostname) | Out-Null
            $journal.phase = 'route_changed'
            Save-Journal $adapter $config $journal
            $route = Invoke-Adapter $adapter 'GetRoute' @($config.cloudflare, $config.tunnels)
            if (-not (Test-ExpectedRoute $route $config.tunnels.local_id)) { throw 'Cloudflare DNS does not target the configured local tunnel ID' }
            $public = Test-PublicHost $adapter $config $evidence.identity $config.host.id
            if (-not $public.ready) { throw 'public readiness or OAuth discovery does not identify the configured local host and base URL' }
            $journal.phase = 'public_verified'
            Save-Journal $adapter $config $journal
        } catch {
            Invoke-ActivationCompensation $adapter $config $journal $_.Exception.Message
        }
        $journal.phase = 'terminal'
        $journal.terminal = 'activated'
        Save-Journal $adapter $config $journal
        Write-Terminal @{
            terminal = 'activated'
            route_target = "$($config.tunnels.local_id).cfargotunnel.com"
            bypassed_guards = $bypassed
            next_action = 'keep the existing connector URL; reconnect or reauthorize only if its host-local OAuth session is absent'
        }
    }

    if ($Action -eq 'Handoff') {
        if ($Force -or $IfUnserved) { Write-Terminal @{ terminal = 'refused'; reasons = @('-Force and -IfUnserved apply only to Activate'); next_action = 'run Handoff without activation-only switches' } 2 }
        if (-not $WhatIfPreference) { Invoke-HandoffReplay $adapter $config $evidence }
        $reasons = @(Get-HandoffReasons $evidence $config)
        if ($reasons.Count) { Write-Terminal @{ terminal = 'refused'; reasons = $reasons; next_action = 'restore source, target, replication, intent, identity, and route evidence before handoff' } 2 }
        $plan = @('write-target-intent', 'confirm-exact-generation-delivery', 'stop-source-service', 'route-stable-hostname-to-target', 'verify-target-route', 'report-bounded-outage')
        if ($WhatIfPreference) { Write-Terminal @{ terminal = 'what_if'; plan = $plan; next_action = 'review the outage and target-local activation steps before proceeding' } }
        $generation = Get-NextGeneration $adapter $evidence.intent
        $intent = @{ schema_version = 1; desired_host = $config.host.peer_id; generation = $generation; advisory = $true }
        $journal = @{
            action = 'Handoff'; phase = 'intent_writing'; generation = $generation; desired_host = $config.host.peer_id
            previous_route = $config.tunnels.local_id; source_was_running = $true; terminal = $null
            identity = @{ expected_base_url = $evidence.identity.expected_base_url }
        }
        Save-Journal $adapter $config $journal
        Write-OperationJson $adapter $config.state.intent_path $intent
        $markerVersion = Invoke-Adapter $adapter 'GetMarkerVersion' @($config.state.intent_path)
        if ([int64]$markerVersion.generation -ne $generation -or [string]::IsNullOrWhiteSpace([string]$markerVersion.version)) { throw 'the newly written desired-host marker could not be versioned' }
        $journal.marker_version = $markerVersion.version
        $journal.phase = 'intent_written'
        Save-Journal $adapter $config $journal
        $delivery = Confirm-ExactIntentDelivery $adapter $config $journal
        if (-not $delivery.delivered) {
            $journal.phase = 'terminal'; $journal.terminal = 'intent_delivery_unproven'; Save-Journal $adapter $config $journal
            Write-Terminal @{ terminal = 'intent_delivery_unproven'; reasons = @($delivery.reason); next_action = 'leave the source serving; restore replication, then rerun Handoff to retry this exact generation' } 2
        }
        $journal.phase = 'intent_delivered'
        Save-Journal $adapter $config $journal
        Invoke-HandoffTransition $adapter $config $journal
    }
} catch {
    Write-Terminal @{ terminal = 'error'; reasons = @($_.Exception.Message); next_action = 'correct configuration or unavailable evidence before retrying' } 1
}
