"""业务路由汇总：通用资源路由 + 区域覆盖/快照对账工作流路由。"""
from __future__ import annotations

from app.routers import resource, workflow

# 固定路径的工作流路由先注册，避免被通用 /{module}/{entry_id} 模式抢先匹配。
ROUTERS = [workflow, resource]
