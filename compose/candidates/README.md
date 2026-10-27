# 候选镜像定义

每个子目录定义一个候选的 Linux 容器构建。

主要内容：

- `各候选 Dockerfile`：安装固定依赖并复制经校验的候选源码和本仓库接入材料。

来源、许可证和固定版本见 configs/candidates/ 与 docs/validation/sources.md；不能使用宿主虚拟环境代替容器依赖。
