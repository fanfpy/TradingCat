# 开源发布指南

当前工作区的 Git 根目录是上一级 `skills/`。发布 TradingCat 时必须创建独立仓库，不能
直接把整个 QwenPaw skills 工作区推送到公开远端。

## 1. 发布前检查

```bash
./.venv/bin/pip install -r requirements-dev.txt
./.venv/bin/python scripts/check_open_source.py
./.venv/bin/python -m build
./.venv/bin/python scripts/check_distribution.py
./.venv/bin/python scripts/build_source_archive.py
TRADINGCAT_ENV_FILE=/tmp/tradingcat-no-env ./.venv/bin/python -m pytest -q
TRADINGCAT_ENV_FILE=/tmp/tradingcat-no-env ./.venv/bin/python e2e_full.py
```

另外必须在最终独立仓库运行 Gitleaks 全历史扫描。GitHub workflow 已配置
`gitleaks/gitleaks-action@v3`；如果发布到 Organization，需要按该 Action 的要求配置
`GITLEAKS_LICENSE` secret。

确认以下内容没有进入候选仓库：

- `.env` 或任何真实凭证；
- `*.db`、WAL、备份、reports、data；
- 真实账户、持仓、订单号、Webhook 或审批密钥；
- QwenPaw 的 Compose、容器配置和其他 skills；
- `.venv`、缓存、构建产物和日志。

## 2. 创建独立仓库

### 方式 A：干净历史（首次公开推荐）

先在当前私有仓库提交已审核的 TradingCat 变更，然后从父仓库导出：

```bash
mkdir -p /tmp/tradingcat-public
git archive --format=tar HEAD:trading-system | tar -x -C /tmp/tradingcat-public
cd /tmp/tradingcat-public
git init -b main
git add .
git commit -m "Initial open-source release"
```

这种方式不会复制被 `.gitignore` 排除的本地凭证和数据库，也不会带出上级仓库历史。

### 方式 B：保留子目录历史

只有确认现有历史扫描干净时才使用 `git filter-repo`：

```bash
git clone --no-local /path/to/private-skills-repo /tmp/tradingcat-public
cd /tmp/tradingcat-public
git filter-repo \
  --path trading-system/ \
  --path-rename trading-system/:
```

过滤完成后再次运行 Gitleaks；路径过滤不等于凭证轮换。若任何凭证曾进入历史，先在来源
系统撤销并轮换，再处理历史。

## 3. GitHub 设置

创建空远端仓库后：

```bash
git remote add origin git@github.com:YOUR_GITHUB_OWNER/tradingcat.git
git push -u origin main
```

在仓库设置中启用：

- Private vulnerability reporting；
- Secret scanning 和 push protection（账户计划支持时）；
- Branch protection / ruleset：CI 和 Gitleaks 必须通过、禁止 force push；
- 至少一次 PR review（多人维护时）；
- 自动删除已合并分支。

首次公开前不要配置真实 Longbridge 或交易凭证为 Actions secrets；公共 CI 全部使用离线
测试。真实只读验收应留在私有受控环境。

## 4. Release

1. 更新 `pyproject.toml` 版本和 `docs/acceptance.md`；
2. 运行全部离线验证和受控只读验收；
3. 检查 wheel/sdist 只包含代码、必要文档、LICENSE 和 NOTICE；
4. 创建签名 tag，例如 `v5.0.0`；
5. 发布 GitHub Release，列出安全边界、迁移说明和已知限制；
6. PyPI 发布应先使用 TestPyPI 验证，且不要把券商凭证写入构建环境。

发布说明必须明确：默认 `DRY_RUN_ONLY`、Longbridge 固定 0.2.74、基本面默认安全缺失、
软件不构成投资建议。
