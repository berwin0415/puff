# llm-lab 业务域

llm-lab 是从零手写 mini-GPT 的学习/实验域，目标是理解 Transformer 与训练流程，不是交付产品。学习/实验属性由业务域本身表达，不再额外增加 `labs/` 目录或 `type:lab` 标签。

## 定位

- 只服务于学习与实验：所有代码、讲解、语料和进度都收敛在本域内。
- 独立 Python 环境，**不参与 pnpm workspace**，也不注册 Nx target。
- 不引入 `transformers` 等现成实现；只依赖 PyTorch 的张量与自动求导。
- 项目按代码形态登记为 `type:lib`：可依赖同域 lib、平台 lib 与契约。

## 项目清单

| 项目 | 路径 | 类型 | 说明 |
| --- | --- | --- | --- |
| `llm-lab-mini-gpt` | `libs/mini-gpt` | lib（Python） | 从零实现 mini-GPT，含讲解文档、步骤脚本与训练代码 |

## 目录约定

```
domains/llm-lab/
├─ README.md                 本文件
└─ libs/
   └─ mini-gpt/              学习项目
      ├─ AGENTS.md           目录与写作规范
      ├─ README.md           入口与当前进度
      ├─ ROADMAP.md          步骤阶梯与验收标准
      ├─ PROGRESS.md         进度日志
      ├─ requirements.txt    Python 依赖
      ├─ notes/              每一步的讲解
      ├─ scripts/            可运行的步骤入口（stepNN_*.py）
      ├─ src/minigpt/        实现
      └─ data/               语料（data/raw 不入库）
```

新增本域项目时放在 `libs/<name>/`，并补 `project.json`：

```json
{
  "name": "llm-lab-<name>",
  "tags": ["domain:llm-lab", "type:lib", "lang:py"]
}
```

## 运行

```bash
# macOS / Linux
cd domains/llm-lab/libs/mini-gpt
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements.txt
.venv/bin/python scripts/step01_check_env.py
```

```powershell
# Windows PowerShell
cd domains\llm-lab\libs\mini-gpt
py -3.11 -m venv .venv
.venv\Scripts\python -m pip install -r requirements.txt
.venv\Scripts\python scripts\step01_check_env.py
```

## 边界

- 学习/实验属性由业务域本身表达；不要为学习或实验项目新增 `labs/` 目录或 `type:lab` 标签。
- 不加入 `pnpm-workspace.yaml`，不实现根 `dev` / `build` / `lint` / `typecheck` / `clean` 脚本契约。
- 不修改 `apps` / `services` 的业务代码；如需复用，先把实现抽到 `libs/` 并单独评审。
- 依赖方向与标签由 `tools/checks/project-boundaries.mjs` 检查。
