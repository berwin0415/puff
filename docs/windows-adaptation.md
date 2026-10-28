# 从 macOS arm64 迁到 Windows x86_64：适配评估

日期：2026-10-29。依据：当前分支 `harness-lab`（HEAD `7be4381`，2026-10-28）与 `IMPLEMENTATION_PLAN.md` 第 2.1 节的 Mac 基线。

本文只评估“把本轮实验环境搬到当前这台 Windows 电脑”的可行性和代价，不改写历史结论、证据与冻结哈希（口径见 [中文维护说明](validation/chinese-maintenance.md)）。

## 1. 结论

**可以适配，但不能直接运行。** 当前分支的全部运行假设都是“macOS arm64 宿主编排 + Linux arm64 容器”：本机既没有容器运行时，也没有 WSL 发行版，而 compose、依赖锁、宿主脚本和配置层都把 arm64 与 POSIX 写死在代码里。

三条路线里推荐 **A（WSL2 + Ubuntu 内 Docker Engine，仓库与数据根都放 ext4）**：它把“宿主只做编排”这一条原验证口径完整保留下来，宿主侧 Python（6 个脚本 `import fcntl`、`/proc` 读取、pidfd、`geteuid`）与测试口径**一行都不用改**，需要动的只是平台标记、依赖锁和路径参数。预计到“冒烟 + 单候选端到端”约 3–4 个工作日；八候选完整重跑另计。

**B（Docker Desktop + 在 Windows 上直接跑宿主编排层）** 要多改 12 处以上宿主代码，并且把多用户隔离、空闲实例采样这类依赖 UID/PID 语义的专项变成不等价验证，不建议作为首轮。**C（不装 WSL2 的纯 Windows 原生）** 不可行：这些镜像是 Linux 镜像，Windows 容器无法运行。

## 2. 环境核对

| 项目 | Mac（本轮实验实际，2026-10-20 记录） | 本机（2026-10-29 实查） | 影响 |
| --- | --- | --- | --- |
| 机器 | Apple M3，8 核，16 GiB | Ryzen 7 9700X，8 核 16 线程，31.2 GiB | 内存更宽裕，但资源上限按 `configs/experiment.yaml:34-35`（6 GiB / 3 CPU）固定，不能放大后与旧结果混算 |
| 架构 | arm64 | amd64 | 全部镜像、全部依赖锁都要按 x86_64 重做 |
| 系统 | macOS | Windows 11 26H2（build 26300.9711） | 宿主脚本的 POSIX 假设全部失效 |
| 容器运行时 | colima，Linux arm64，Engine 29.5.2，Compose 5.6.0 | 无：`docker`、`podman`、`docker compose` 均不在 PATH | 必须先装 |
| 虚拟化组件 | 原生 | Hyper-V/WSL 组件未安装：`vmcompute`、`WslService`、`LxssManager`、`vmms` 服务均不存在；`HvHost` 在跑（通常来自记忆完整性等基于虚拟化的安全功能），但 `wsl.exe --list --verbose` 报“未安装用于 Linux 的 Windows 子系统” | 装 WSL2 需要管理员权限并重启一次 |
| 宿主 Python | 3.12 | 3.11.9（默认）、3.14；`uv` 已卸载（`%APPDATA%\uv` 不存在，`py -0p` 里只剩失效条目） | `requires-python ">=3.12,<3.13"`，两个已装版本都不满足 |
| 宿主 Node | — | v24.16.0 / npm 11.13.0 | 只有三个 `.mjs` 用例用得到；候选镜像内自带 node 24.22.0 |
| 磁盘 | 数据盘余约 91 GiB | D: 余 149.6 GiB（C: 69.2、E: 305.9、F: 187.7） | 够用；WSL vhdx 建议放 D: |
| 其他 | 工作目录 `/Users/hanbowen01/repo/puff` | `D:\projects\puff`；代码页 936（GBK）；`LongPathsEnabled=1`；git 仓库内 `core.autocrlf=false` | 终端编码与行尾两处需要额外注意 |

仓库体积：`.git` 只有 5.2 MiB（pack 2.61 MiB），工作区 4.4 GB 几乎全是 `llm-lab/.venv` 的 21810 个文件（另一项目的残留、已被忽略、在 Windows 上不可用）。**迁移时只带 git 对象和源码**，虚拟环境在新机重建。

## 3. 阻塞项

### 3.1 P0：不做就跑不起来

| 编号 | 问题 | 证据 |
| --- | --- | --- |
| P0-1 | 没有任何容器运行时 | `docker info` 不可用；服务、`C:\Program Files\Docker`、`~/.docker` 均无 |
| P0-2 | 架构是 arm64 写死 | `compose/docker-compose.yml` 14 个服务全部 `platform: linux/arm64`（第 8、48、61、75、90、107、124、148、172、196、220、244、268、292 行） |
| P0-3 | 三个候选的 `uv.lock` 只解析了 linux aarch64 | `adapters/{deepagents_langgraph,google_adk,openai_agents_sdk}/pyproject.toml` 里都有 `environments = ["sys_platform == 'linux' and platform_machine == 'aarch64'"]`（分别在第 26、18、16 行），锁文件里 `manylinux…aarch64` 命中 30/32/19 次、`x86_64` 命中 0 次 |
| P0-4 | 两份 compose 依赖锁的哈希只覆盖 arm64 wheel | `compose/locks/bench-requirements.txt`（15 包、39 行 hash，每包 2 个）与 `runtime-requirements.txt`；`bench-requirements.txt:44` 自己写明“sdist 和 CPython 3.12 Linux aarch64 wheel 固定值”；`--require-hashes` 在 x86_64 上找不到匹配 wheel 会直接失败 |
| P0-5 | OpenHands 依赖清单文件名即架构 | `adapters/openhands/requirements-linux-aarch64.txt`（125 KB）被 `compose/candidates/openhands/Dockerfile:24` 直接引用；另两个 arm64 点：`codex_sdk/Dockerfile:19` 用 `@openai/codex-linux-arm64` 的 `aarch64-unknown-linux-musl` 二进制，`openclaw/Dockerfile:32` 有 `--config.supportedArchitectures.cpu=arm64` |
| P0-6 | 配置层拒绝 Windows 绝对路径 | `runner/config.py:227` 把 `"\\" in str(path)` 当非法字符；而 `Path("D:/x")` 在 Windows 上渲染为 `D:\x`，`_root()` 会抛 `ConfigError("…必须是绝对目录路径…")`；反过来 `/Users/...` 在 Windows 上 `is_absolute()` 为假。配置在 Windows 上根本加载不了 |
| P0-7 | 数据根与源码路径是 Mac 绝对路径 | `configs/experiment.yaml:4` `data_root: /Users/hanbowen01/harness-lab-data`；`configs/candidates/*.yaml` 11 条 `path:` 指向 `/Users/hanbowen01/repo/*` 或 `/Users/hanbowen01/harness-lab-sources/*`；这些源码/归档本机都不存在（`external/` 被忽略、不在库内） |
| P0-8 | 缺模型凭证 | 无 `.env`；`model-proxy` 需要 `MODEL_API_BASE_URL` / `MODEL_API_KEY` / `MODEL_NAME`（`configs/experiment.yaml:9-11`、`.env.example`） |

其中 P0-3/P0-4/P0-5 是同一条根因（镜像按 arm64 生成）的三个面：**只要换架构，所有带哈希或二进制轮子的锁都得在 x86_64 上重新生成**。基础镜像本身不用改——`python:3.12.13-slim-bookworm@sha256:3f9c7f75…`、`mcr.microsoft.com/playwright:v1.64.0-noble@sha256:4109b399…`、`node:24.22.0-bookworm-slim@sha256:156e9bc7…` 都是多架构 index 摘要，amd64 可直接拉取同一摘要。

### 3.2 P1：容器能起也跑不完，或结论不可比

| 编号 | 问题 | 证据 |
| --- | --- | --- |
| P1-1 | 6 个宿主脚本 `import fcntl` | `scripts/run_detailed_batch.py:9`、`run_fault_batch.py:9`、`run_fault_recovery.py:8`、`run_multi_user.py:12`、`run_pause_recovery.py:9`、`probe_native_state.py:8`（唯一编排入口 `scripts/lab.py` 不受影响） |
| P1-2 | 依赖 `/proc` 与 pidfd | `runner/shared_supervisor.py:139` 读 `/proc/{pid}/stat`；`runner/shared_uid.py:38` 读 `/proc/{pid}/status`、`:117` `os.geteuid()`、`:119-120` `pidfd_open`/`pidfd_send_signal`、`:339/:342` pidfd kill |
| P1-3 | 多处断言“在 Linux 容器里” | `runner/bench.py:26`、`scripts/audit_instance_cost_sampling.py:38-42`（`/.dockerenv` + `sys.platform`）、`tests/test_shared_uid.py:84-86`、`tests/integration/*.py`（也 `import fcntl`） |
| P1-4 | compose 里 53 处 Mac 绝对挂载默认值 | `${LAB_EVIDENCE_HOST:-/Users/hanbowen01/…}` 等，第 71、85、86、100、118 行与 8 个候选的 6 行一组（137-142、161-166、185-190、209-214、233-238、257-262、281-286、305-310 行）。`scripts/lab.py:111-113、183-187` 会覆盖，但漏传一处就挂载到不存在的路径 |
| P1-5 | cp936 终端与 UTF-8 输出 | `scripts/lab.py:40` 用 `subprocess.run(..., text=True)` 按系统编码解码 docker 输出；本机 ACP=936，中文/UTF-8 输出会乱码甚至抛 `UnicodeDecodeError`（本次用 PowerShell 5.1 读仓库内 UTF-8 文件已复现乱码） |
| P1-6 | Windows 上导出源码会变样 | `scripts/prepare_candidate_source.py` 用 `git ls-files` 读工作区字节（`:91-102`）、重建符号链接（`:73`）、`chmod`（`:60`）；全局 `core.autocrlf=true` 会把导出物改成 CRLF，符号链接要开发者模式/管理员才建得出来，而 `SOURCE_MANIFEST.json` 的 `export_sha256` 会因此与 Mac 端不一致 |
| P1-7 | Mac 路径进了断言 | `tests/test_config.py:40` 断言 `str(config.data_root) == "/Users/hanbowen01/harness-lab-data"`；`scripts/summarize_usage.py:165` 默认值同路径 |

全仓库 20 个文件、628 处 `/Users/hanbowen01`，其中 545 处在 `docs/validation/*.json` 与 `pause-resource-summary.json` 这类**历史证据里，按仓库口径不得改写**；真正要动的是 compose 的 53 处、`configs/` 的 4 处、`test_config.py` 的 1 处和 `summarize_usage.py` 的 1 处。

### 3.3 P2：不影响可行性，但会影响交付质量

- **Docker Desktop 许可**：若走 B，需要确认公司规模是否符合 Docker Desktop 免费条款；走 A（Ubuntu 内 Docker Engine）不涉及。
- **网络**：首次要拉 4 个基础镜像、8 个候选的构建依赖（PyPI / npm / Playwright 浏览器层）与 11 份上游源码，国内网络建议先确认代理。
- **性能数字不可比**：容器配额相同（6 GiB / 3 CPU / `pids_limit` / `read_only`），但 x86_64 与 arm64 的墙钟时间、镜像体积、构建耗时都不能与旧报告混算。

## 4. 路线对比

| | A. WSL2 + Ubuntu 内 Docker Engine（推荐） | B. Docker Desktop + Windows 侧跑宿主编排 | C. 纯 Windows 原生 |
| --- | --- | --- | --- |
| 前置安装 | 管理员 + 重启；Ubuntu 24.04；docker-ce + compose 插件；uv + Python 3.12（都在 Ubuntu 内） | 管理员；Docker Desktop（WSL2 后端）；Windows 侧 Python 3.12 + uv | 无 Linux 运行时可用 |
| 仓库改动面 | 平台标记 14 处 + 8 个候选 Dockerfile 的 arm64 细节 + 4 份锁重生成 + 2 处路径 + `.env` | A 的全部，再加 12 处以上宿主代码改写 | 全部 8 个候选镜像重写为 Windows 容器 |
| 宿主脚本 / 测试 | **零改动**（POSIX 假设成立；Linux 断言照旧在容器内跑） | `fcntl`×6、`/proc`×2、pidfd、`geteuid`、`/.dockerenv`、`config.py:227`、`text=True`、`test_config.py:40` 均要改，并重写多用户/UID 相关测试口径 | 同 B，且 `shared_uid`/`shared_supervisor` 的语义在 Windows 上无对应实现 |
| 与既有结论可比性 | 容器内配额与任务不变，只需分架构记录 | 多用户隔离、空闲实例采样变成不等价验证 | 不适用 |
| 到“冒烟 + 单候选端到端” | 约 3–4 个工作日 | 约 6–8 个工作日，且需重新冻结专项口径 | 不可行 |
| 主要风险 | WSL2 默认只分到约一半内存（需 `.wslconfig` 调整）；`/mnt/d` 是 9p 文件系统，构建慢、权限语义不同，所以仓库要放进 ext4 | 改动侵入实验语义，可能连带要求重验既往专项 | — |

## 5. 推荐方案 A 的实施步骤

每步都有验收点，未过不进下一步。

| 步骤 | 动作 | 验收 |
| --- | --- | --- |
| S0 前提 | 确认有管理员权限、可重启；BIOS 已开 SVM；GitHub / PyPI / 模型 API 可达；D: 余量 ≥ 60 GiB | 四项都能答“是” |
| S1 装 WSL2 | 管理员：`wsl --install -d Ubuntu-24.04`（自动启用 VirtualMachinePlatform/WSL 组件），重启；`wsl --set-default-version 2`、`wsl --update`；在 `%UserProfile%\.wslconfig` 设 `memory=12GB`、`processors=8`（默认只分到宿主一半内存） | `wsl -l -v` 显示 VERSION 2；Ubuntu 内 `free -g` 看到 12 GB |
| S2 装 Docker | Ubuntu 内按官方 apt 源装 `docker-ce docker-ce-cli containerd.io docker-compose-plugin docker-buildx-plugin`，当前用户入 `docker` 组；Ubuntu 24.04 的 WSL 要在 `/etc/wsl.conf` 开 `[boot] systemd=true`（否则每次 `service docker start`） | `docker info` 的 OSType=linux、MemTotal ≥ 6 GiB、`docker compose version` |
| S3 落仓库 | 在 Ubuntu 内 `git clone` 到 `~/repo/puff`（**不要**放 `/mnt/d`），确认 `core.autocrlf=false`；数据根设 `~/harness-lab-data` | `git status` 干净；`python -c` 能加载 `configs/experiment.yaml` |
| S4 Python | 装 `uv`，`uv python install 3.12`，`uv sync`（dev 依赖含 pytest 8.4.2） | `uv run pytest --collect-only -q` 能收集 |
| S5 平台标记 | 把 14 处 `platform: linux/arm64` 抽成锚点 `${LAB_PLATFORM:-linux/amd64}`；8 个候选 Dockerfile 的 `*-linux-arm64` 缓存 id 改名 | `docker compose config` 通过 |
| S6 依赖锁 | 三个 `pyproject.toml` 的 `environments` 改成 `platform_machine == 'x86_64'` 后重新 `uv lock`；`compose/locks/*.txt` 用 `uv export … --python-platform x86_64-unknown-linux-gnu` 重导（现有注释里的命令不足以复现 x86_64 哈希）；OpenHands 清单改名为 `requirements-linux-x86_64.txt` 并同步 `openhands/Dockerfile:24`；`codex_sdk/Dockerfile:19` 换成官方的 x64 运行时包（如 `@openai/codex-linux-x64`，以发布页为准）；`openclaw/Dockerfile:32` 改 `cpu=x64` | 8 个候选镜像逐个 `docker build` 成功，且 `pip install --require-hashes` 未因缺 wheel 失败 |
| S7 源码 | 按 11 条 `path:`/`commit:`/`official_url:` 重新取回上游（git clone 或下载归档），把 `configs/candidates/*.yaml` 的 `path` 指到新位置，跑 `uv run python scripts/prepare_candidate_source.py --all --destination-root external/build-context` | `external/build-context/<候选>/SOURCE_MANIFEST.json` 生成，`sources[].observed_commit` 与配置一致 |
| S8 凭证与冒烟 | 写 `.env`（三个模型变量，不入库）；跑 infrastructure-smoke | bench/fixtures/site/tools/model-proxy/browser 全部启动，冒烟通过 |
| S9 端到端 | 跑 DeepSeek Harness 一条工具任务（含浏览器步骤） | 工具调用、浏览器操作、证据与账本齐全 |
| S10 收尾 | 记录 amd64 镜像 ID、耗时、版本，单独成档 | 新结果与 arm64 报告分开存放，不混算 |

口径提醒：`configs/experiment.yaml:34-35` 的 6 GiB / 3 CPU 是“全实验总预算”，换机器不要顺手放大，否则结果不可比；确实要放大就作为新变体单独记录。

## 6. 可以原样复用的部分

- **全部文档与报告**：`docs/report/`（含 8 份候选报告、附录、分享快照）与 `docs/validation/` 的结论、设计、证据索引都按平台无关撰写，可原样使用；只有“版本与来源”类附录里的镜像架构信息需要按新构建补记。
- **编排结构与安全约束**：compose 的服务划分、`read_only` 根、`tmpfs`、`mem_limit`/`cpus`/`pids_limit`、服务名寻址、`/evidence`、`/workspace`、`/state` 挂载约定都不必改。
- **基础镜像摘要**：三个 digest 是多架构 index，amd64 拉同一摘要即可，不需要重新核验。
- **`adapters/*/bootstrap-requirements.txt`**：只钉 `uv==0.9.18`，19 个哈希是跨平台的，可直接复用。
- **测试与验收逻辑**：2216 通过 / 18 跳过的基线（见 [中文维护说明](validation/chinese-maintenance.md:25)）在 x86_64 镜像里应当复现，差异需逐条解释。
- **宿主编排入口**：`scripts/lab.py` 不依赖 `fcntl`，在 WSL2 里原样可用（只需 UTF-8 环境）。

## 7. 需要先确认的四件事

1. **目标是什么**：只是要在这台机器上读代码、改文档、继续写报告，还是要真的把实验重跑一遍？前者只需 S3–S4，半天内可用。
2. **权限与重启**：装 WSL2 需要一次管理员操作 + 重启。如果没有管理员权限，方案 A/B 都做不成，只能退到“纯文档维护”。
3. **模型凭证**：`.env` 的三个变量是否还有效、能不能在这台机器上调用（决定 S8/S9 能否做）。
4. **重跑范围**：只跑冒烟 + DSH 一条端到端，还是八候选全部重跑？后者约为原轮实验的工作量，且结论要按 x86_64 重新记录。

## 8. 采证记录

本文数字均来自本轮实查：`systeminfo`、`Get-Service`、`Test-Path`、`py -0p`、`node -v`、`git config --list --show-origin`、`Get-ChildItem` 统计、`Select-String` 计数，以及直接读取上表列出的文件与行号。本轮只新增本文档，未改动任何代码、配置或证据。

`HvHost` 在运行只说明宿主机有基于虚拟化的安全功能；`vmcompute`、`WslService`、`LxssManager`、`vmms` 缺失才决定 WSL2/Hyper-V 尚不可用，两者不矛盾。沙箱内 `Get-CimInstance` 被拒，因此硬件虚拟化开关以 BIOS 现场确认为准。
