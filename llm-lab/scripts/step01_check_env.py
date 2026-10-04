"""第 1 步：确认 Python、PyTorch 和 GPU 三者真的能一起工作。

这里还没有任何语言模型的内容。目的只是把地基钉死：如果 GPU 或自动求导
本身是坏的，后面每一步的报错都会看起来像我们自己代码的 bug。
"""

from __future__ import annotations

import platform
import time

import torch

SIZE = 4096
ROUNDS = 20


def describe_environment() -> None:
    """打印实际加载到的是哪个 Python、哪个 PyTorch 构建。"""
    print(f"python         : {platform.python_version()} on {platform.system()}")
    print(f"torch          : {torch.__version__}")


def describe_device() -> torch.device:
    """返回 GPU 设备；如果没有可用的 GPU，就带着明确的提示停下来。"""
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
    """两边矩阵乘法的结果必须一致——又快又错的 GPU 比没有 GPU 更糟。"""
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
    """自动求导是后面每一次训练的引擎，先确认它是好的。"""
    x = torch.tensor([1.0, 2.0, 3.0], device=device, requires_grad=True)
    y = (x**2).sum()
    y.backward()

    expected = 2 * torch.tensor([1.0, 2.0, 3.0], device=device)
    ok = torch.allclose(x.grad, expected)
    print(f"autograd       : d(sum(x^2))/dx = {x.grad.tolist()} -> {'ok' if ok else 'MISMATCH'}")
    if not ok:
        raise SystemExit("Autograd produced an unexpected gradient.")


def benchmark_matmul(device: torch.device) -> None:
    """粗略感受一下 GPU 的速度，顺带提醒预热是必须的。"""
    a = torch.randn(SIZE, SIZE, device=device)
    b = torch.randn(SIZE, SIZE, device=device)

    for _ in range(3):  # 头几次调用要承担内核选择和显存分配的开销
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
