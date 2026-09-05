"""Blackwell verification gate for server-side media extraction (plan Task 0).

Confirms the GPU path works for the media engines on this box:
- torch sees CUDA and lists the GPU's compute arch (sm_120 for Blackwell RTX 50-series)
- faster-whisper (ctranslate2) loads on cuda and transcribes a generated silent clip
  — this is the real test of whether CTranslate2 has sm_120 kernels
- pymupdf + pytesseract import; the Tesseract binary is reported separately

Run: uv run python scripts/verify-media-gpu.py
Exit 0 = gate PASS only when faster-whisper/CTranslate2 executes on the GPU.
Torch and CLIP diagnostics are reported separately and do not decide ASR readiness.
"""

from __future__ import annotations

import os
import platform
import struct
import sys
import tempfile
import wave

_RUNTIME_FLOORS = {
    "ctranslate2": ("4.6.3", "5"),
    "nvidia-cublas-cu12": ("12.8.4.1", "13"),
    "nvidia-cuda-runtime-cu12": ("12.8.90", "13"),
    "nvidia-cudnn-cu12": ("9.5.0.50", "10"),
}


def _version_tuple(value: str) -> tuple[int, ...]:
    return tuple(int(part) for part in value.split("+")[0].split(".") if part.isdigit())


def _verify_runtime_distributions() -> dict[str, str]:
    from importlib.metadata import version

    observed: dict[str, str] = {}
    for distribution, (minimum, maximum) in _RUNTIME_FLOORS.items():
        value = version(distribution)
        if not (_version_tuple(minimum) <= _version_tuple(value) < _version_tuple(maximum)):
            raise RuntimeError(
                f"{distribution}={value} is outside required [{minimum}, {maximum})"
            )
        observed[distribution] = value
    return observed


def _selected_native_paths() -> dict[str, str | None]:
    """Report libraries actually mapped after CTranslate2 compute, never metadata guesses."""
    names = {"cublas": "cublas", "cudart": "cudart", "cudnn": "cudnn"}
    if platform.system() == "Linux":
        try:
            mapped = open("/proc/self/maps", encoding="utf-8", errors="replace").read().splitlines()
        except OSError as exc:
            raise RuntimeError(f"cannot inspect selected CUDA libraries: {exc}") from exc
        selected: dict[str, str] = {}
        exact = {
            "cublas": "libcublas.so.12",
            "cudart": "libcudart.so.12",
            "cudnn": "libcudnn.so.9",
        }
        for key, needle in names.items():
            paths = [
                line.rsplit(" ", 1)[-1]
                for line in mapped
                if needle in line.lower() and line.rstrip().endswith(exact[key])
            ]
            if not paths and key in {"cudart", "cudnn"}:
                selected[key] = None
                continue
            if not paths:
                raise RuntimeError(f"selected {key} library was not mapped after ASR compute")
            selected[key] = paths[0]
        return selected
    if platform.system() == "Windows":
        import ctypes

        selected = {}
        for key, dll in {"cublas": "cublas64_12.dll", "cudart": "cudart64_12.dll", "cudnn": "cudnn64_9.dll"}.items():
            handle = ctypes.windll.kernel32.GetModuleHandleW(dll)
            if not handle and key in {"cudart", "cudnn"}:
                selected[key] = None
                continue
            if not handle:
                raise RuntimeError(f"selected {key} library {dll} was not loaded after ASR compute")
            buffer = ctypes.create_unicode_buffer(32768)
            if not ctypes.windll.kernel32.GetModuleFileNameW(handle, buffer, len(buffer)):
                raise RuntimeError(f"cannot resolve selected {key} library path")
            selected[key] = buffer.value
        return selected
    raise RuntimeError("selected CUDA library inspection is unsupported on this platform")


def _verify_selected_native_paths(selected: dict[str, str | None]) -> dict[str, str | None]:
    from exomem import asr_runtime

    roots = [str(root) for root in asr_runtime._nvidia_roots()]
    for component, path in selected.items():
        if path is None:
            continue
        if not any(path.startswith(root + os.sep) for root in roots):
            raise RuntimeError(f"selected {component} is not wheel-owned: {path}")
    return selected


def _selected_native_versions(selected: dict[str, str | None]) -> dict[str, str | None]:
    """Query loaded runtime handles so a shadowed old system library cannot pass."""
    import ctypes

    runtime = None
    if selected["cudart"] is not None:
        cudart = ctypes.CDLL(selected["cudart"])
        runtime = ctypes.c_int()
        if cudart.cudaRuntimeGetVersion(ctypes.byref(runtime)) != 0:
            raise RuntimeError("cudaRuntimeGetVersion failed for selected cudart")
    cudnn_value = None
    if selected["cudnn"] is not None:
        cudnn = ctypes.CDLL(selected["cudnn"])
        cudnn_value = int(cudnn.cudnnGetVersion())
    cublas = ctypes.CDLL(selected["cublas"])
    handle = ctypes.c_void_p()
    if cublas.cublasCreate_v2(ctypes.byref(handle)) != 0:
        raise RuntimeError("cublasCreate_v2 failed for selected cublas")
    try:
        cublas_value = ctypes.c_int()
        if cublas.cublasGetVersion_v2(handle, ctypes.byref(cublas_value)) != 0:
            raise RuntimeError("cublasGetVersion_v2 failed for selected cublas")
    finally:
        cublas.cublasDestroy_v2(handle)

    return {
        "cublas": f"{cublas_value.value // 10000}.{(cublas_value.value // 100) % 100}.{cublas_value.value % 100}",
        "cudart": (
            f"{runtime.value // 1000}.{(runtime.value // 10) % 100}.{runtime.value % 10}"
            if runtime is not None
            else None
        ),
        "cudnn": (
            f"{cudnn_value // 10000}.{(cudnn_value // 100) % 100}.{cudnn_value % 100}"
            if cudnn_value is not None
            else None
        ),
    }


def _verify_selected_native_versions(versions: dict[str, str | None]) -> dict[str, str | None]:
    floors = {
        "cublas": "12.8.4",
        # cudaRuntimeGetVersion exposes toolkit ABI only (12.8), not wheel build 12.8.90.
        "cudart": "12.8",
        "cudnn": "9.5.0",
    }
    for component, floor in floors.items():
        if versions[component] is None:
            continue
        if _version_tuple(versions[component]) < _version_tuple(floor):
            raise RuntimeError(
                f"selected {component}={versions[component]} is below required {floor}"
            )
    return versions


def _asr_probe() -> int:
    """Hidden child: imports native ASR only after parent-owned loader setup."""
    from faster_whisper import WhisperModel

    from exomem import asr_runtime

    selection = asr_runtime.select_asr_runtime()
    if selection.device != "cuda":
        raise RuntimeError("GPU verification requires CUDA admission; bounded CPU fallback is not a pass")
    versions = _verify_runtime_distributions()
    tmp = os.path.join(tempfile.gettempdir(), "exomem_gate_silence.wav")
    with wave.open(tmp, "w") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(16000)
        w.writeframes(struct.pack("<" + "h" * 16000, *([0] * 16000)))
    model = WhisperModel("tiny", device=selection.device, compute_type=selection.compute_type)
    segments, _info = model.transcribe(tmp)
    list(segments)  # execution, not enumeration, is the readiness proof
    selected = _verify_selected_native_paths(_selected_native_paths())
    native_versions = _verify_selected_native_versions(_selected_native_versions(selected))
    print(
        f"faster-whisper OK on {selection.device} ({selection.compute_type}); "
        f"runtime={versions}; selected_native={selected}; native_versions={native_versions}"
    )
    return 0


def main() -> int:
    if "--asr-probe-child" in sys.argv:
        return _asr_probe()
    ok = True

    # --- torch / CUDA arch ---
    try:
        import torch

        avail = torch.cuda.is_available()
        arches = torch.cuda.get_arch_list() if avail else []
        name = torch.cuda.get_device_name(0) if avail else "(no cuda)"
        print(f"torch {torch.__version__} | cuda={avail} | device={name}")
        print(f"  arch_list={arches}")
        if avail and not any("120" in a for a in arches):
            print("  WARN: sm_120 not in arch_list — Blackwell kernels may be missing from torch")
    except Exception as e:  # noqa: BLE001
        print(f"torch diagnostic unavailable: {e}")

    # --- faster-whisper child process (the gate that matters) ---
    try:
        from exomem import asr_runtime

        env = asr_runtime.cuda_runtime_child_env(os.environ)
        env["EXOMEM_ASR_DEVICE"] = "cuda"
        proc = __import__("subprocess").run(
            [sys.executable, __file__, "--asr-probe-child"],
            env=env,
            text=True,
            capture_output=True,
            timeout=300,
        )
        if proc.returncode:
            raise RuntimeError((proc.stderr or proc.stdout).strip())
        print(proc.stdout.strip())
    except Exception as e:  # noqa: BLE001
        ok = False
        print(f"faster-whisper GPU check FAILED: {e}")

    # --- per-keyframe video CLIP (real sampler + real CLIP encode) ---
    try:
        import av
        import numpy as np

        from exomem import embeddings

        # Synthesize a ~24s clip with a white square that marches across the frame —
        # SPATIAL structure (not just colour) so the luminance pHash sees distinct
        # scenes and the sampler keeps several keyframes. (aHash is level-invariant,
        # so a textured frame is needed; real recordings are textured.)
        tmp_mp4 = os.path.join(tempfile.gettempdir(), "kb_gate_clip.mp4")
        fps, secs, w_, h_, box = 10, 24, 96, 96, 24
        with av.open(tmp_mp4, "w") as out:
            vs = out.add_stream("mpeg4", rate=fps)
            vs.width, vs.height, vs.pix_fmt = w_, h_, "yuv420p"
            n = fps * secs
            for i in range(n):
                rgb = np.full((h_, w_, 3), 30, dtype=np.uint8)
                x = int((w_ - box) * i / max(1, n - 1))
                y = int((h_ - box) * (i % fps) / max(1, fps - 1))
                rgb[y : y + box, x : x + box] = 235  # white box, moving position
                for pkt in vs.encode(av.VideoFrame.from_ndarray(rgb, format="rgb24")):
                    out.mux(pkt)
            for pkt in vs.encode():
                out.mux(pkt)

        frames = embeddings.embed_video_frames(__import__("pathlib").Path(tmp_mp4))
        assert frames, "no keyframe vectors produced"
        assert all(v.shape == (embeddings.CLIP_DIM,) for _, v in frames), "wrong vector dim"
        assert all(abs(float(np.linalg.norm(v)) - 1.0) < 1e-3 for _, v in frames), "vectors not L2-normalized"
        assert all(0.0 <= ts <= secs + 1 for ts, _ in frames), "timestamp out of range"
        print(
            f"embed_video_frames OK — {len(frames)} keyframe vector(s), "
            f"ts={[round(ts, 1) for ts, _ in frames]}"
        )
    except Exception as e:  # noqa: BLE001
        print(f"per-keyframe video CLIP diagnostic unavailable: {e}")

    # --- pymupdf / pytesseract import + Tesseract binary ---
    for mod in ("fitz", "pytesseract"):
        try:
            __import__(mod)
            print(f"{mod} import OK")
        except Exception as e:  # noqa: BLE001
            print(f"{mod} diagnostic unavailable: {e}")
    try:
        import pytesseract

        print(f"tesseract binary: {pytesseract.get_tesseract_version()}")
    except Exception as e:  # noqa: BLE001
        print(f"tesseract binary NOT found (install: winget install UB-Mannheim.TesseractOCR): {e}")

    print("GATE:", "PASS" if ok else "FAIL")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
