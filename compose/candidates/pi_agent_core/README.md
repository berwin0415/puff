# Pi Agent Core 镜像

定义 Pi Agent Core 在本机 Linux Docker 内的候选镜像。

主要内容：

- `Dockerfile`：安装固定运行依赖，复制已校验上游材料和本仓库对应接入代码。

接入实现见 adapters/pi_agent_core/，来源与版本见 configs/candidates/pi_agent_core.yaml。通过编排脚本准备材料和构建，不能直接复制宿主依赖或密钥。
