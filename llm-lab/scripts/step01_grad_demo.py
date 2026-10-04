"""第 1 步补充：`x.grad` 到底是什么？

`step01_check_env.py` 只证明了自动求导能跑通。这个脚本把它拆开，展示后面
整个项目都要与之相处的几条性质：梯度只在 `backward()` 之后才出现、它是
**累加**的、以及计算图会被反向传播消耗掉。
"""

from __future__ import annotations

import warnings

import _bootstrap  # 把 src/ 加进 sys.path
import torch


def show_grad_starts_empty() -> tuple[torch.Tensor, torch.Tensor]:
    x = torch.tensor([1.0, 2.0, 3.0], requires_grad=True)
    y = (x**2).sum()
    print(f"1. backward 之前        x.grad = {x.grad}")
    return x, y


def show_first_backward(x: torch.Tensor, y: torch.Tensor) -> None:
    y.backward()
    print(f"2. 第一次 backward 之后  x.grad = {x.grad.tolist()}   (= 2x)")
    print(f"   梯度形状 {tuple(x.grad.shape)}，和参数形状 {tuple(x.shape)} 一致")


def show_graph_is_consumed(y: torch.Tensor) -> None:
    try:
        y.backward()
    except RuntimeError as error:
        first_line = str(error).split(".")[0]
        print(f"3. 同一张图再 backward   -> RuntimeError: {first_line}.")
        print("   反向传播会把计算图用掉，想复用要显式 retain_graph=True")


def show_gradients_accumulate(x: torch.Tensor) -> None:
    y = (x**2).sum()
    y.backward()
    print(f"4. 新前向 + backward     x.grad = {x.grad.tolist()}   (又加了一遍)")
    print("   梯度是累加的，不是覆盖的")

    x.grad = None
    y = (x**2).sum()
    y.backward()
    print(f"5. 清零之后再 backward   x.grad = {x.grad.tolist()}   (回到 2x)")
    print("   这就是训练循环里必须 zero_grad 的原因")


def show_intermediate_has_no_grad() -> None:
    a = torch.tensor([1.0, 2.0], requires_grad=True)
    h = a * 2
    z = (h**2).sum()
    z.backward()
    with warnings.catch_warnings():
        # 每次读非叶子张量的 .grad，PyTorch 都会告警。这里读它本身就是要讲的
        # 知识点，所以把告警静音，由我们自己的文字来解释。
        warnings.simplefilter("ignore")
        h_grad = h.grad
    print(f"6. 中间张量 h.grad = {h_grad}   叶子张量 a.grad = {a.grad.tolist()}")
    print("   只有 requires_grad 的叶子张量会保留梯度（中间量默认用完即弃）")


def main() -> None:
    x, y = show_grad_starts_empty()
    show_first_backward(x, y)
    show_graph_is_consumed(y)
    show_gradients_accumulate(x)
    show_intermediate_has_no_grad()


if __name__ == "__main__":
    main()
