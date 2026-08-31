## ADDED Requirements

### Requirement: ASR accelerator readiness binds the runtime actually used
Media installation and doctor checks SHALL distinguish the ASR runtime from unrelated model frameworks. Accelerator readiness SHALL bind `ctranslate2>=4.6.3,<5`, `nvidia-cublas-cu12>=12.8.4.1,<13`, `nvidia-cuda-runtime-cu12>=12.8.90,<13`, and `nvidia-cudnn-cu12>=9.5.0.50,<10`, Exomem's conservative computation policy, the device capability reported by that ASR engine, and a real model-execution probe when an explicit probe is requested. Engine capability reporting alone SHALL NOT override Exomem's known-safe computation policy. Wheel-owned CUDA libraries SHALL be placed on the loader path before the ASR child process starts; mutating `LD_LIBRARY_PATH` after process startup SHALL NOT be treated as readiness.

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
- **AND** a fresh subprocess proves the selected native component identities and versions meet the pinned floors before claiming readiness

#### Scenario: Wheel name matches but native version is too old
- **WHEN** a selected cuBLAS, CUDA-runtime, or cuDNN component reports a version below the supported floor
- **THEN** ASR readiness fails with the component name, selected identity, observed version, and required floor
- **AND** path precedence or package presence alone does not satisfy readiness

#### Scenario: CPU-only installation
- **WHEN** no accelerator is present
- **THEN** the profile remains installable and reports bounded CPU ASR
- **AND** accelerator absence does not make the core service unhealthy
