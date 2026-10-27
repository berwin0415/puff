# 公共镜像依赖锁

固定公共服务和验收镜像使用的 Python 包及分发包哈希。

主要内容：

- `runtime-requirements.txt`：业务服务、工具和模型代理依赖。
- `bench-requirements.txt`：验收容器依赖。

包名、版本和哈希是机器安装输入，保持原格式；更新后需重新构建并验收受影响镜像。
