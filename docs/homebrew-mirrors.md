# Homebrew 源配置

本文整理在 macOS 上配置 Homebrew 镜像源、排查 `brew update` / `brew upgrade` 超时的方法。

## 背景

Homebrew 5 之后，更新和安装通常会涉及三类网络访问：

1. Homebrew 自身 Git 仓库：`brew --repo`
2. Homebrew API：默认 `https://formulae.brew.sh/api`
3. Bottle 二进制包：默认 `https://ghcr.io/v2/homebrew/core`

如果只配置 `HOMEBREW_BOTTLE_DOMAIN`，下载 bottle 会走镜像，但 Homebrew 仍可能访问官方 API，导致 `brew update` 或 `brew upgrade` 超时。

## 推荐配置：USTC

当前实测清华 TUNA 的 Homebrew Git 镜像可能出现排队：

```text
remote: Waiting in queue... (Position: 834)
```

因此推荐使用 USTC 作为 Homebrew 主源、API 和 bottle 源。

### 1. 切换 Homebrew 主仓库

Apple Silicon 默认路径：

```bash
git -C /opt/homebrew remote set-url origin https://mirrors.ustc.edu.cn/brew.git
```

Intel Mac 默认路径：

```bash
git -C /usr/local/Homebrew remote set-url origin https://mirrors.ustc.edu.cn/brew.git
```

### 2. 配置 API 和 bottle 镜像

zsh 用户建议写入 `~/.zprofile`：

```bash
cat >> ~/.zprofile <<'EOF'

# Homebrew mirrors
export HOMEBREW_API_DOMAIN="https://mirrors.ustc.edu.cn/homebrew-bottles/api"
export HOMEBREW_BOTTLE_DOMAIN="https://mirrors.ustc.edu.cn/homebrew-bottles"
EOF

source ~/.zprofile
```

bash 用户可以写入 `~/.bash_profile`：

```bash
cat >> ~/.bash_profile <<'EOF'

# Homebrew mirrors
export HOMEBREW_API_DOMAIN="https://mirrors.ustc.edu.cn/homebrew-bottles/api"
export HOMEBREW_BOTTLE_DOMAIN="https://mirrors.ustc.edu.cn/homebrew-bottles"
EOF

source ~/.bash_profile
```

### 3. 验证配置

```bash
brew config | grep -E 'ORIGIN|HOMEBREW_API_DOMAIN|HOMEBREW_BOTTLE_DOMAIN'
```

期望看到类似输出：

```text
ORIGIN: https://mirrors.ustc.edu.cn/brew.git
HOMEBREW_API_DOMAIN: https://mirrors.ustc.edu.cn/homebrew-bottles/api
HOMEBREW_BOTTLE_DOMAIN: https://mirrors.ustc.edu.cn/homebrew-bottles
```

然后执行：

```bash
brew update
brew upgrade
```

## 额外 tap 的处理

`brew update` 会更新所有 tap。即使 Homebrew 主仓库已切到镜像，额外 tap 仍可能访问 GitHub，例如：

```text
/opt/homebrew/Library/Taps/leoafarias/homebrew-fvm
origin https://github.com/leoafarias/homebrew-fvm
```

查看所有 tap 的 remote：

```bash
for repo in "$(brew --repo)" /opt/homebrew/Library/Taps/*/*; do
  [ -d "$repo/.git" ] || continue
  echo "### $repo"
  git -C "$repo" remote -v
 done
```

如果某个 tap 长期卡住，可以考虑：

1. 确认是否还需要该 tap：

   ```bash
   brew tap
   brew list --formula | grep '^fvm$'
   ```

2. 不需要时移除：

   ```bash
   brew untap leoafarias/fvm
   ```

3. 需要时保留，但要接受它仍可能访问 GitHub。

## 快速诊断命令

### 查看 Homebrew 源配置

```bash
brew config
brew --repo
git -C "$(brew --repo)" remote -v
```

### 测试 API / bottle 源速度

```bash
curl -L --connect-timeout 8 --max-time 20 -o /dev/null -sS \
  -w 'http=%{http_code} connect=%{time_connect}s start=%{time_starttransfer}s total=%{time_total}s size=%{size_download}\n' \
  https://mirrors.ustc.edu.cn/homebrew-bottles/api/formula.jws.json
```

### 测试官方 API 是否慢

```bash
curl -L --connect-timeout 8 --max-time 20 -o /dev/null -sS \
  -w 'http=%{http_code} connect=%{time_connect}s start=%{time_starttransfer}s total=%{time_total}s size=%{size_download}\n' \
  https://formulae.brew.sh/api/formula.jws.json
```

### 手动测试 Git fetch 卡点

```bash
git -C "$(brew --repo)" fetch --tags --force origin
```

如果看到 `Waiting in queue...`，通常是当前 Git 镜像源拥塞。

## 恢复官方源

如需恢复官方 Homebrew 源：

```bash
git -C /opt/homebrew remote set-url origin https://github.com/Homebrew/brew.git
```

并从 shell 配置文件中删除：

```bash
export HOMEBREW_API_DOMAIN="https://mirrors.ustc.edu.cn/homebrew-bottles/api"
export HOMEBREW_BOTTLE_DOMAIN="https://mirrors.ustc.edu.cn/homebrew-bottles"
```

重新打开终端或执行：

```bash
unset HOMEBREW_API_DOMAIN HOMEBREW_BOTTLE_DOMAIN
brew update
```

## 当前结论

如果 `brew upgrade` 总是超时，优先检查：

1. `brew config` 里是否缺少 `HOMEBREW_API_DOMAIN`
2. Homebrew 主仓库是否使用了拥塞的 Git 镜像
3. 额外 tap 是否仍在访问 GitHub
4. `brew update --verbose` 或手动 `git fetch` 是否显示 `Waiting in queue...`
