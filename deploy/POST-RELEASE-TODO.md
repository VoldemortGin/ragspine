# ragspine 发版后待办（2026-06-25）

> 家族大程已收口，**仅剩这一步必须手动 web 操作**（GitHub API 限制，无法脚本化）。
> 后面照此做即可。

---

## 唯一待办：GHCR 镜像 visibility（设 public，可选）

### 背景
`image-publish` workflow 已修通，镜像已推到 GHCR：

```
ghcr.io/voldemortgin/ragspine:0.6.0
ghcr.io/voldemortgin/ragspine:v0.6.0
ghcr.io/voldemortgin/ragspine:latest
```

首次推送的 GHCR package **默认 private**。

### 为什么必须手动
GitHub 改 package visibility **没有 REST API、gh CLI 也不支持**
（见 cli/cli discussion #6003）。只能走 web UI——这点和 Cloudflare（可用 API token
全自动）不同。

### 决策：public 还是 private？
| 选项 | 含义 | 适用 |
|---|---|---|
| **public** | 任何人免登录 `docker pull` | 开源项目 / 生产服务器 / CI 免凭据拉取 |
| **private**（默认，不动） | 拉镜像需 `docker login ghcr.io`（PAT 或 GITHUB_TOKEN） | 不公开镜像 |

⚠️ **public 不可逆**——一旦设为 public 无法改回 private。

### 设 public 的步骤
1. GitHub → 右上角头像 → **Your packages**
2. 点 **ragspine**
3. 右侧 **Package settings**
4. 底部 **Danger Zone** → **Change visibility** → 选 **Public** → 确认

### 验证（设 public 后）
```bash
# 不需要 docker login 即应成功
docker pull ghcr.io/voldemortgin/ragspine:0.6.0
```

---

## 家族收尾状态（reference，均已完成，无需操作）

- **PyPI**：corespine `0.1.1` + rag-spine `0.6.0` 已发布
- **文档站**：4 站全 active —— `rag-spine.org`(apex) + `core.` / `agent.` / `pdf.rag-spine.org`
- **release.yml CI gate**：四坑修通（fetch-depth `0` / docstring-refs 豁免 data/ / matrix 去 3.10 / mypy override numpy·torch）
- **image-publish**：build 两坑修通（uv `--no-sources` 走 PyPI 装 corespine；builder COPY force-include 的 `llms.txt`+`docs/llms`）
