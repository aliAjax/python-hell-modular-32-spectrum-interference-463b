# 无线电频谱干扰调查与协调

模块化纯 Python 3.9.6+ 标准库项目，默认端口 `8332`。

模块结构：`app.py` 负责组装，`src/domain.py` 定义字段和错误，`src/rules.py` 负责评估、定位、授权和状态机，`src/repository.py` 管理 SQLite、版本和审计链，`src/service.py` 编排权限，`src/http_api.py` 提供接口，`src/audit.py` 生成审计哈希。

```bash
python3 app.py --init --db ./data.db
python3 app.py --db ./data.db --port 8332
python3 -m unittest discover -s tests -v
```

使用 `X-User-Id`、`X-Role`、`X-Region` 请求头。接口为 `GET /health`、`GET /api/state`、`POST /api/items`、`POST /api/items/<id>/sources`、`POST /api/items/<id>/actions` 和 `GET /api/items/<id>/audit`。

同频段的重复上报不再新建事件：创建时若存在未结案（非 resolved/cancelled）且频段重叠的事件，后到的上报并入该事件（HTTP 200），各监测站测到的强度、时间和来源保留在 `payload.reports` 和 `sources` 记录中；完全相同的上报（同一监测站同一时刻）仍按 `duplicate_item` 拒绝。合并或 `correct_measurement` 更新测量后，评估和定位立即失效，状态回到 `pending`，处置人需按最新测量重新评估、定位。合并出多个区域的跨区记录，区域敏感操作只能由 `regulator` 复核，普通协调员越权返回 403 `regulator_required`。合并在单事务内完成，失败整体回滚、不留半状态，值班员重新提交即可重试，已合并过的同一上报幂等返回。

测试覆盖完整调查流程、测量更正、重复事件、同频段合并、合并失效与重试、并发上报、跨区越权、定位置信度和版本冲突。协议接入、真实无线电传播模型和执法权限仍需由外部系统实现。
