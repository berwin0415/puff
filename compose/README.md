# Docker 镜像与编排

保存本机实验的容器构建、服务编排和资源限制。

主要内容：

- `docker-compose.yml`：唯一基础编排，包含工具、业务、模型代理、浏览器和候选服务。
- `detailed.override.yml`：详细实验显式启用的覆盖配置。
- `Dockerfile.*`：基础服务、验收和浏览器等镜像定义。
- `candidates/`：八候选专用 Dockerfile。
- `browser/`：浏览器启动及基础验收程序。
- `locks/`：公共 Python 镜像依赖锁。

由 scripts/ 中的编排入口按实验配置启动；模型密钥不进入镜像，构建和实验遵守总资源预算。
