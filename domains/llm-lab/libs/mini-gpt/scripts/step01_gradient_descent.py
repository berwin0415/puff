"""第 1 步补充：梯度是什么，为什么"沿着它下降"就能奏效？

任何规模的模型训练都能归结成一个循环：看看把参数挪一点会让损失怎么变，
然后往相反方向挪。这个脚本用小到能手算的函数演示这件事——最后那部分用
两个参数，已经是一个真实（虽然极小）的优化问题了。
"""

from __future__ import annotations

import _bootstrap  # 把 src/ 加进 sys.path
import torch


def loss_1d(w: float) -> float:
    """一条抛物线，最低点在 w = 3。"""
    return (w - 3.0) ** 2


def hand_gradient_1d(w: float) -> float:
    """手算出来的导数：d/dw (w - 3)^2 = 2(w - 3)。"""
    return 2.0 * (w - 3.0)


def autograd_gradient_1d(w: float) -> float:
    """不去求导，直接问 PyTorch。"""
    tensor = torch.tensor(w, requires_grad=True)
    loss_1d_tensor = (tensor - 3.0) ** 2
    loss_1d_tensor.backward()
    return float(tensor.grad)


def show_gradient_at_points() -> None:
    print("f(w) = (w - 3)^2,  最小值在 w = 3")
    print(f"{'w':>6} {'f(w)':>8} {'手算 2(w-3)':>14} {'autograd':>10} {'含义':<24}")
    for w in (0.0, 2.0, 3.0, 5.0):
        hand = hand_gradient_1d(w)
        auto = autograd_gradient_1d(w)
        meaning = "在最小值上，斜率为 0" if abs(hand) < 1e-9 else f"w 增大 1，f 大约增加 {hand:+.1f}"
        print(f"{w:>6.1f} {loss_1d(w):>8.1f} {hand:>14.1f} {auto:>10.1f} {meaning:<24}")
    print()


def descend(w: float, learning_rate: float, steps: int, label: str) -> None:
    history = [w]
    for _ in range(steps):
        w = w - learning_rate * hand_gradient_1d(w)
        history.append(w)
    trail = " -> ".join(f"{value:g}" for value in history[:6])
    print(f"{label}: 起点 {history[0]:g}，{steps} 步后 w = {w:.6g}，f(w) = {loss_1d(w):.3g}")
    print(f"    前几步: {trail}")


def show_descent() -> None:
    print("梯度下降: w <- w - 学习率 * 梯度")
    descend(0.0, 0.1, 20, "学习率 0.1  ")
    descend(0.0, 0.5, 20, "学习率 0.5  ")
    descend(0.0, 1.1, 20, "学习率 1.1  ")
    print("    学习率 1.1 时每步都跨过谷底并且离得更远，这叫发散\n")


def show_two_parameters() -> None:
    print("两个参数: f(a, b) = (a - 3)^2 + (b + 1)^2，最小值在 a = 3, b = -1")
    a = torch.tensor(0.0, requires_grad=True)
    b = torch.tensor(0.0, requires_grad=True)
    learning_rate = 0.1

    for step in range(1, 21):
        loss = (a - 3.0) ** 2 + (b + 1.0) ** 2
        loss.backward()
        if step in (1, 2, 5, 20):
            print(
                f"    第 {step:>2} 步: loss = {loss.item():.4f}"
                f"  梯度 = [{a.grad:.3f}, {b.grad:.3f}]"
                f"  ->  a = {a.item():.3f}, b = {b.item():.3f}"
            )
        with torch.no_grad():
            a -= learning_rate * a.grad
            b -= learning_rate * b.grad
        a.grad = None
        b.grad = None


def main() -> None:
    show_gradient_at_points()
    show_descent()
    show_two_parameters()


if __name__ == "__main__":
    main()
