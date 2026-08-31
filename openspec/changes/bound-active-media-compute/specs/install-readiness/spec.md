## ADDED Requirements

### Requirement: ASR accelerator readiness binds the runtime actually used
Media installation and doctor checks SHALL distinguish the ASR runtime from unrelated model frameworks. Accelerator readiness SHALL bind a Blackwell-capable lower bound of the selected ASR engine, its required native runtime major, the device capability reported by that ASR engine, and a real model-execution probe when an explicit probe is requested. Wheel-owned CUDA libraries SHALL be placed on the loader path before the ASR child process starts; mutating `LD_LIBRARY_PATH` after process startup SHALL NOT be treated as readiness.

#### Scenario: PyTorch CUDA works but ASR CUDA does not
- **WHEN** the torch accelerator probe succeeds but the ASR engine cannot load or execute against its required native runtime
- **THEN** install readiness reports ASR acceleration unavailable
- **AND** it does not claim that the torch runtime repairs or proves the ASR runtime

#### Scenario: ASR CUDA works without PyTorch
- **WHEN** CTranslate2 reports a usable CUDA device and supported computation types but torch is not installed
- **THEN** ASR accelerator admission may succeed
- **AND** the absence of torch does not force the disposable ASR worker onto CPU

#### Scenario: Blackwell-capable media install
- **WHEN** a standard or media profile is installed on a supported Blackwell host
- **THEN** the ASR engine can resolve a compatible CUDA runtime and select a supported computation type
- **AND** an explicit media GPU probe executes model compute before reporting success

#### Scenario: Incompatible system cuBLAS precedes the service
- **WHEN** the host loader would otherwise resolve an older `libcublas.so.12`
- **THEN** the parent launches the disposable worker and explicit verifier with the media extra's compatible CUDA library directories first
- **AND** a fresh subprocess proves which runtime is loaded before claiming readiness

#### Scenario: CPU-only installation
- **WHEN** no accelerator is present
- **THEN** the profile remains installable and reports bounded CPU ASR
- **AND** accelerator absence does not make the core service unhealthy
