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
- `POST /api/items/{id}/records/merge`，离线记录批量合并（见下）
- `POST /api/items/{id}/transition`，必须提交`expected_version`
- `GET /api/audit`

允许角色：field_commander, incident_commander, logistics, viewer。火线长度、风向变化和离线记录数量影响风险等级；同一资源不能同时出现在多个活动任务中。

## 离线记录合并

队员回营后通过`POST /api/items/{id}/records/merge`批量补录离线记录，请求体为`{"records": [...]}`，每条含：

- `client_ref`（必填）：客户端生成的幂等键，同一事件内已存在时视为重复，返回原记录且不新增审计；
- `resource_id`（可选）：队员/资源标识，若该资源在其他未关闭事件仍有`open`记录，则整批退回（409），响应的`conflicts`数组说明冲突事件与队员，且不写入任何记录；
- `kind`、`detail`（必填）与`status`（可选，`open`/`closed`，默认`open`）：记录内容。

响应恒含`created_count`、`duplicate_count`、`rejected_count`（三者之和等于批内条数），成功时另附`created`与`duplicates`记录明细；整批退回时附`conflicts`明细。整批在单事务内处理，不存在部分写入。

## 测试

```bash
python3 -m unittest discover -s tests -v
```
