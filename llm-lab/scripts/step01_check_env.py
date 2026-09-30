"""Step 1: verify that Python, PyTorch and the GPU actually work together.

Nothing here is about language models yet. The point is to make sure the
foundation is solid before building on it: if the GPU or autograd is broken,
every later step will look like a bug in *our* code.
"""

from __future__ import annotations

import platform
import time

import torch

SIZE = 4096
ROUNDS = 20


def describe_environment() -> None:
    """Print which Python and which PyTorch build we actually loaded."""
    print(f"python         : {platform.python_version()} on {platform.system()}")
    print(f"torch          : {torch.__version__}")


def describe_device() -> torch.device:
    """Return the GPU device, or stop with a clear message if there is none."""
    if not torch.cuda.is_available():
        raise SystemExit(
            "CUDA is not available. Check that you installed the cu128 build "
            "and that the NVIDIA driver is recent enough."
        )

    device = torch.device("cuda")
    props = torch.cuda.get_device_properties(device)
    print(f"gpu            : {props.name}")
    print(f"compute cap.   : sm_{props.major}{props.minor}")
    print(f"vram           : {props.total_memory / 1024**3:.1f} GiB")
    print(f"bundled cuda   : {torch.version.cuda}")
    return device


def check_cpu_matches_gpu(device: torch.device) -> None:
    """Matmul results must agree; a fast-but-wrong GPU is worse than none."""
    torch.manual_seed(0)
    a = torch.randn(512, 512)
    b = torch.randn(512, 512)

    on_cpu = a @ b
    on_gpu = (a.to(device) @ b.to(device)).cpu()
    max_diff = (on_cpu - on_gpu).abs().max().item()

    verdict = "ok" if max_diff < 1e-3 else "MISMATCH"
    print(f"cpu vs gpu     : max diff {max_diff:.2e} -> {verdict}")
    if verdict != "ok":
        raise SystemExit("GPU results disagree with CPU results.")


def check_autograd(device: torch.device) -> None:
    """Autograd is the engine behind every training step we will write."""
    x = torch.tensor([1.0, 2.0, 3.0], device=device, requires_grad=True)
    y = (x**2).sum()
    y.backward()

    expected = 2 * torch.tensor([1.0, 2.0, 3.0], device=device)
    ok = torch.allclose(x.grad, expected)
    print(f"autograd       : d(sum(x^2))/dx = {x.grad.tolist()} -> {'ok' if ok else 'MISMATCH'}")
    if not ok:
        raise SystemExit("Autograd produced an unexpected gradient.")


def benchmark_matmul(device: torch.device) -> None:
    """A rough sense of GPU speed, and a reminder that warm-up matters."""
    a = torch.randn(SIZE, SIZE, device=device)
    b = torch.randn(SIZE, SIZE, device=device)

    for _ in range(3):  # the first calls pay for kernel selection and allocation
        a @ b
    torch.cuda.synchronize()

    start = time.perf_counter()
    for _ in range(ROUNDS):
        a @ b
    torch.cuda.synchronize()
    elapsed = time.perf_counter() - start

    tflops = 2 * SIZE**3 * ROUNDS / elapsed / 1e12
    print(f"matmul {SIZE}^3  : {elapsed / ROUNDS * 1000:.2f} ms/iter, {tflops:.1f} TFLOP/s")


def main() -> None:
    describe_environment()
    device = describe_device()
    check_cpu_matches_gpu(device)
    check_autograd(device)
    benchmark_matmul(device)
    print("\nenvironment is ready for step 2")


if __name__ == "__main__":
    main()
