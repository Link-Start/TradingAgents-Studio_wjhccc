# TODO

## 通知推送(待实现 — 计划中)

在以下事件发生时,向外部渠道(钉钉 / Server酱 / Telegram / 邮件,任选)推送消息,
这样无需一直开着网页:

- **强信号**:定时分析产出高置信度 BUY / SELL 时推送(代码、信号、置信度、报告链接)。
- **成交**:自动交易买入/卖出、止损/止盈触发平仓时推送。
- **风控**:触发单日亏损熔断、定时任务连续失败被自动禁用时推送。
- **预算**:LLM 当日 token 用量达到预算上限、定时分析被暂停时推送。

实现建议:
- 新增 `web/backend/notify.py`,一个 `send(title, body, level)` 抽象,渠道由环境变量配置
  (如 `TRADINGAGENTS_NOTIFY_WEBHOOK` / `TRADINGAGENTS_DINGTALK_TOKEN` / `TRADINGAGENTS_SERVERCHAN_KEY`)。
- 在 `scheduler.py`(分析完成、熔断、预算暂停、失败禁用)、`risk.py`(止损止盈、熔断)、
  `routers/paper.py`(自动成交)处调用 `notify.send(...)`。
- 失败不可影响主流程(try/except 包裹,best-effort)。
- 可加节流/去重,避免同一事件刷屏。
