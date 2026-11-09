# infra

基础设施资产目录，由平台/构建团队维护。

当前为空。按设计文档，后续在这里放：

- 基础镜像（各语言运行时基线）
- Helm 基础 chart
- Terraform 模块

约定：每个可部署单元自己的 `Dockerfile` 与 Helm values 放在其项目目录内（例如 `domains/puff/services/api/`），基础部分从这里引用。新增资产前先确认部署目标（Kubernetes、镜像仓库、GitOps 仓库）已经确定。
