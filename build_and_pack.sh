#!/usr/bin/env bash
# 一键构建前端 + 打部署包 deploy.tar.gz
# 用法:  bash build_and_pack.sh
# 适用于 Windows(git bash,自带 tar)/ macOS / Linux。

set -euo pipefail

# 切到脚本所在目录(=项目根),不依赖调用时的工作目录。
cd "$(dirname "$0")"
ROOT="$(pwd)"

echo "==> [1/3] 安装前端依赖 (npm install)"
( cd web/frontend && npm install )

echo "==> [2/3] 构建前端 (vite build,跳过 vue-tsc 类型检查)"
# 用 vite build 而非 npm run build:避免类型告警中断打包(README 约定)。
( cd web/frontend && npx vite build )

# 构建产物必须存在,否则线上前端是空的。
if [ ! -f web/frontend/dist/index.html ]; then
  echo "!! 构建失败:未找到 web/frontend/dist/index.html" >&2
  exit 1
fi

echo "==> [3/3] 打包 deploy.tar.gz"
rm -f deploy.tar.gz
tar -czf deploy.tar.gz \
  --exclude='*__pycache__*' --exclude='*.pyc' --exclude='*.log' \
  --exclude='web/frontend/node_modules' \
  tradingagents cli web pyproject.toml README.md .env uv.lock

# 校验前端成品确实进了包(否则线上是旧界面 / 空白页)。
DIST_COUNT="$(tar -tzf deploy.tar.gz | grep -c 'web/frontend/dist/' || true)"

echo ""
echo "=== 打包完成 ==="
ls -lh deploy.tar.gz
echo "包内 web/frontend/dist/ 文件数: ${DIST_COUNT}"
if [ "${DIST_COUNT}" -lt 1 ]; then
  echo "!! 警告:包里没有前端成品,请检查构建。" >&2
  exit 1
fi
echo "位置: ${ROOT}/deploy.tar.gz"
