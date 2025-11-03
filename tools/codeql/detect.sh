#!/usr/bin/env bash
# 作用：修复“could not resolve module cpp / semmle...”问题，并只用单个QL文件跑分析
set -euo pipefail

# -------- 基本配置（按需改） --------
QL_FILE="./queries/InvalidPointerDeref.ql"
QUERY_DIR="./queries"                 # 你的 QL 文件所在目录
PROJECT_DIR="./demo_cpp_project"      # 含 Makefile 的 C/C++ 项目
DB_DIR="./codeql-db-cpp"
SARIF_OUT="./results.sarif"
CSV_OUT="./results.csv"
LANG="cpp"
THREADS=0                             # 0 让 codeql 自动选线程
# -----------------------------------

echo "==> Checking codeql..."
command -v codeql >/dev/null 2>&1 || { echo "ERROR: codeql 不在 PATH 中"; exit 2; }

[ -f "$QL_FILE" ] || { echo "ERROR: 找不到 QL_FILE: $QL_FILE"; exit 3; }
[ -d "$PROJECT_DIR" ] || { echo "ERROR: 找不到 PROJECT_DIR: $PROJECT_DIR"; exit 4; }
mkdir -p "$QUERY_DIR"

# 1) 确保 queries 目录是一个合法的 Query Pack（有 qlpack.yml）
QLPACK_YML="$QUERY_DIR/qlpack.yml"
if [ ! -f "$QLPACK_YML" ]; then
  echo "==> 未发现 $QLPACK_YML，自动创建一个最小化 query pack 清单..."
  cat > "$QLPACK_YML" <<'YAML'
name: local/invalid-pointer-deref
version: 0.0.1
library: false
extractor: cpp
dependencies:
  codeql/cpp-all: "*"
  codeql/cpp-queries: "*"
YAML
  echo "==> 已写入 $QLPACK_YML"
else
  echo "==> 已存在 $QLPACK_YML，保持不动"
fi

# 2) 拉取官方依赖包（标准库 + C++ 查询/库）
echo "==> 下载必需的 CodeQL 包（若本地已缓存，会跳过实际下载）..."
codeql pack download codeql/cpp-all codeql/cpp-queries

# 3) 安装本地 query pack（生成 lockfile 并解析依赖）
echo "==> 安装本地查询包依赖 (codeql pack install)..."
( cd "$QUERY_DIR" && codeql pack install )

# 4) 重建数据库
if [ -d "$DB_DIR" ]; then
  echo "==> 发现已有数据库目录，备份后重建..."
  ts=$(date +%Y%m%d_%H%M%S)
  mv "$DB_DIR" "${DB_DIR}_backup_${ts}"
fi

echo "==> 创建 CodeQL 数据库（语言: $LANG）并用 Makefile 构建以收集编译信息..."
codeql database create "$DB_DIR" \
  --language="$LANG" \
  --command="make -C \"$PROJECT_DIR\"" \
  --overwrite

# 5) 运行分析（只跑你这个 pack 下的 .ql；pack 里目前只有一个文件，就等价于只跑它）
echo "==> 运行分析（SARIF 输出）..."
THREADS_ARG=""
[ "$THREADS" -gt 0 ] && THREADS_ARG="--threads=$THREADS"

# 用“包”的方式供解析器找到依赖（推荐）：直接把目录当一个 pack 传给 analyze
codeql database analyze "$DB_DIR" "$QUERY_DIR" \
  --format=sarif-latest \
  --output="$SARIF_OUT" \
  $THREADS_ARG

echo "==> 额外导出 CSV ..."
codeql database analyze "$DB_DIR" "$QUERY_DIR" \
  --format=csv \
  --output="$CSV_OUT" \
  $THREADS_ARG

echo "==> 完成。结果："
echo "    SARIF: $SARIF_OUT"
echo "    CSV  : $CSV_OUT"

# 可选：若装了 jq，给个简单统计
if command -v jq >/dev/null 2>&1 && [ -f "$SARIF_OUT" ]; then
  cnt=$(jq '[.runs[].results[]] | length' "$SARIF_OUT" || echo "0")
  echo "    Issue 数（来自 SARIF）: $cnt"
fi
