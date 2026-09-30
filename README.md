# 大型场馆人群安全与现场指挥

只使用Python标准库和SQLite的模块化服务，默认端口`8340`。支持场馆区域、入场口、容量、通道、安保岗位、医疗点、事件、限流、开放通道、疏散和医疗任务、人员到位、区域恢复、延迟重复事件和容量冲突，并提供突发拥挤时的**疏导命令（command）**：按事件冻结现场、占额转入、通道定向、救援派单，支持并发占额、失败续跑、快照失效、取消与核销。

## 模块结构

- `app.py`：参数解析、依赖组装和服务生命周期。
- `src/domain.py`：角色、领域异常和数据对象。
- `src/rules.py`：容量计算、事件优先级、状态机和团队冲突约束。
- `src/repository.py`：SQLite持久化、乐观锁、幂等、审计查询，以及命令与容量预留的原子事务。
- `src/commands.py`：疏导命令Saga编排（冻结、占额、定向、派单、重试、取消、核销、快照失效）。
- `src/service.py`：用例编排、权限校验和版本控制。
- `src/http_api.py`：JSON接口和统一错误响应。
- `src/audit.py`：操作审计。
- `static/index.html`：疏导指挥台演示页面。

## 初始化与启动

```bash
python3 app.py --db ./data.db --port 8340
```

## 核心对象

`venue`为场馆，`zone`为区域，`gate`为入场口，`post`为安保岗位，`medical_point`为医疗点，`incident`为事件，`task`为现场任务，`command`为疏导命令。

## 疏导命令

指挥员（`coordinator`/`supervisor`/`admin`）对未结束事件提交一条疏导命令，Saga按顺序执行：

1. **占额（claim）**：在目标区域原子预留 `divert_count` 个名额（计入 `reserved_occupancy`），命令与预留同一事务落库。
2. **冻结**：受影响区域 `open/limited → frozen`、入场口 `open/restricted → frozen`、医疗点 → `frozen`。冻结后旧的放行安排无法继续放人（入场口必须 `open` 且区域可放行）。
3. **通道定向**：冻结的入场口设置 `flow_direction`（`entry` / `exit` / `bidirectional`）。
4. **救援派单**：创建并分配一条 `task` 给指定班组（班组冲突会让命令失败在该步）。

命令状态：`executing → completed → reconciled`，失败为 `failed`，外部变化为 `stale`，取消为 `cancelled`。

- **并发**：两人同时提交，数据库写锁下只一方占额成功；后到方收到 `409 zone remaining capacity conflict: requested X, remaining Y`，且不会留下命令。同一事件同时只允许一条活跃命令（`executing/failed/stale`）。
- **失败续跑**：失败保留已完成步骤（名额、冻结、已建任务），`retry` 只重跑未完成步骤；冻结/定向按命令幂等，派单复用已创建的 `task_id`，不会重复占额或重复派单。
- **旧命令失效**：命令持有事件等级、区域容量、入场口通道状态的快照；事件 `escalate`、区域 `resize`、入场口被外部改动后，重试返回 `409 StaleCommandError` 并在 `reasons` 中列出每条阻塞，命令置 `stale`，只能取消后重新提交。
- **取消**：释放预留名额、解冻区域/入场口/医疗点（入场口恢复 `entry` 方向）、取消已派出的任务。
- **核销**：`completed` 后按实际转入人数 `reconcile`，在目标区域把预留转为实际在场人数并释放多余名额。

提交示例：

```json
POST /api/commands
{
  "incident_id": "...", "venue_id": "...",
  "zone_ids": ["zone-a"], "gate_ids": ["gate-1"], "medical_point_ids": ["mp-1"],
  "target_zone_id": "zone-b", "divert_count": 30,
  "flow_direction": "exit", "team_id": "rescue-1", "task_type": "evacuation"
}
```

命令动作：`POST /api/entities/<command_id>/actions`，`action` 为 `retry` / `cancel`（需 `reason`）/ `reconcile`（需 `actual_count`）。失败响应会带上 `command_id`（和失效时的 `reasons`），便于页面定位命令后续跑或取消。

新增的现场动作：区域 `freeze` / `unfreeze` / `resize`，入场口 `freeze` / `unfreeze` / `direct`，医疗点 `freeze` / `unfreeze`，事件 `escalate`。

## 接口

- `GET /health`
- `GET /api/<kind>`，可用`?status=`过滤
- `GET /api/entities/<id>`
- `POST /api/<kind>`
- `POST /api/entities/<id>/actions`
- `GET /api/audit`

身份通过`X-User-Id`和`X-Role`请求头传入。可选`Idempotency-Key`防止重复创建。

## 测试

```bash
python3 -m unittest discover -s tests -v
```

## 局限

容量和调度规则为可运行的简化模型，不接入闸机、视频分析、室内定位、消防联动或真实应急指挥系统。
