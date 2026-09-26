# 山火事件指挥与离线人员调度

维护火线、风向、资源和任务区，合并离线现场记录并防止人员重复分配。

## 模块结构

- `app.py`：参数解析、依赖组装和HTTP服务启动。
- `src/domain.py`：数据结构、错误、状态和基础校验。
- `src/rules.py`：状态机、角色矩阵、优先级、期限和关闭不变量。
- `src/repository.py`：SQLite建表、事务、版本控制和审计链。
- `src/service.py`：权限检查、用例编排、并发控制和审计。
- `src/http_api.py`：JSON路由和统一错误响应。
- `src/audit.py`：UTC时间和SHA-256审计事件。
- `static/index.html`：最小演示页。
- `tests/`：完整流程、规则和失败测试。

## 初始化与启动

```bash
python3 app.py --db ./data.db --port 8319
```

默认端口为`8319`，首次启动自动建库。使用`X-Actor`和`X-Role`请求头传递身份。

## 主要接口

- `GET /health`
- `GET /api/items`
- `POST /api/items`
- `GET /api/items/{id}`
- `POST /api/items/{id}/records`
- `POST /api/items/{id}/records/merge`，离线记录批量合并，body为`{"records":[...]}`
- `POST /api/items/{id}/transition`，必须提交`expected_version`
- `GET /api/audit`

允许角色：field_commander, incident_commander, logistics, viewer。火线长度、风向变化和离线记录数量影响风险等级；同一资源不能同时出现在多个活动任务中。

### 离线记录合并

`POST /api/items/{id}/records/merge`（角色 field_commander、logistics）接收`records`数组，每条含`client_ref`、`resource_id`及记录内容（`kind`、`detail`，可选`status`，默认open）。

- **幂等去重**：同一事件下`client_ref`已存在（含同一批内较早条目）时返回原记录，记为重复、不新增审计。
- **跨火线冲突**：`resource_id`在别的未关闭事件仍有open分配时，整批原子退回（HTTP 409），`conflicts`中给出冲突事件（`conflict_item_id`、`conflict_item_title`）和队员`resource_id`，不写入任何记录或审计。同一事件内的重复分配允许。
- **响应计数**：成功返回`inserted_count`（新增）、`duplicate_count`（重复）、`rejected_count`（拒绝，恒为0）及`inserted`、`duplicates`明细；冲突退回时`inserted_count=0`，`rejected_count`为被退回的新记录数，同时报告已识别的`duplicate_count`。
- 每条成功新增的记录写一条`record`审计（`offline_merge:true`），重复记录不写审计。

## 测试

```bash
python3 -m unittest discover -s tests -v
```
