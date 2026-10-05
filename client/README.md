# PromptQL 网关客户端

## 准备

```bash
# 1) 服务要在跑
python -m uvicorn app.main:app --host 127.0.0.1 --port 8080

# 2) 拿一个下游 Key（管理台「下游 API」页，或命令行）
curl -s -X POST http://127.0.0.1:8080/api/apikeys -H "Content-Type: application/json" -d '{"label":"my"}' 
# 把返回的 key 写进 client/key.txt
```

## 跑 coding 任务

```bash
cd client

# 纯问答
python pql.py ask "用 Python 写一个 LRU 缓存"

# 改本地文件（读 → 改 → 写回磁盘）
python pql.py code src/buggy.py --task "修复浮点精度，改用 Decimal" --write

# 分析整个目录（只读）
python pql.py code src/ --task "找出所有 bug 并说明" --tree

# 看它的思考和工具轨迹
python pql.py code src/a.py --task "重构" --write --show-thinking --show-tools
```

## 文件放在哪

网关有**工作区沙箱**：`data/workspace/`
把要处理的代码放进去（或把 `WORKSPACE_ROOT` 指到你的项目目录）。

```bash
# 例：把整个项目挂进来（只读试验推荐先不写）
export WORKSPACE_ROOT=/path/to/your/project
```

## 直接用 HTTP

```bash
curl -X POST http://127.0.0.1:8080/v1/chat/completions \
  -H "Authorization: Bearer $KEY" -H "Content-Type: application/json" \
  -d '{
    "model": "claude opus 5.5",
    "messages": [{"role":"user","content":"审计 src/buggy.py 并修复"}],
    "workspace": {"files":["src/buggy.py"], "write":true, "force":true}
  }'
```

响应里会多两个字段：
- `agent_tools` —— agent 的工具轨迹（run_shell / write_file …）
- `workspace_applied` —— 写回结果（路径 + 字节数）
