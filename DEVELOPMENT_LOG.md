# Development Log

## 2026-08-19 (America/Los_Angeles) - AI Instrument Allocator V1 与旧策略有序退出

### 变更原因和账户边界

- 用户已停止连续服务；本次没有启动 supervisor、连接真实下单工具，也没有修改任何现有 `state/`、`logs/`、订单、成交、PnL 或 OAuth 文件。
- `long_directional_options_v2_weighted` 和历史 `ai_gated_technical_v1` 现在停止创建新 entry；每次旧 AI cycle 仍先运行原 monitor，旧主账户的 open option order 和 position 仍由原 forward monitor/exit logic 处理，直到全部清仓。
- 历史主账户和旧 AI sleeve 的 `$2,000` 账本原地保留。新增 `ai_instrument_allocator_v1` 使用独立 `$10,000` state/log namespace；首次运行只在该 namespace 不存在时初始化，重启时以已有状态为准。

### 新决策、工具分配和风险流程

- DeepSeek 不再输出彼此独立的方向概率和无符号幅度分布。最终信号必须为指定 horizon 下 7 个互斥 signed-return buckets，总和在 `1e-6` 内等于 1；bullish、bearish、neutral、dominant bucket 和保守 move 全部由 Python 派生，原始值固定标记 `uncalibrated`。
- 新 pipeline 采用两速时钟：20:00 ET 完整研究、08:00 盘前更新、09:25 证据失效复核、09:32 仅刷新报价并执行有效计划、正常时段有限频率研究；09:32 不重复调用 LLM。每个持仓都有 restart-safe mandate，按 `intraday_close`、`next_close` 或 `two_to_five_days` 管理；缺失或失效 mandate 会 fail closed 退出。
- Python allocator 比较 bullish 的 long equity/long call 或 bearish 的 long put。期权使用 underlying move、剩余时间和多组 IV 情景重新定价，纳入 Vega、spread、slippage、tick 和 break-even；Delta/Gamma/Theta/Vega 只作 sensitivity 解释，不替代多日 payoff 重新定价。
- 股票限制为 25% NAV notional，并要求计划止损风险不超过 1% NAV。期权保持单笔 premium 3%、aggregate 8%；股票和期权合计最多 3 个 executable position、每日最多 3 次 entry，同一 underlying 同时只能存在一个 equity 或 option exposure。同日 stop-loss/thesis invalidation 后仍禁止重新入场。
- `$2,000` counterfactual 只检查 `$10,000` allocator 已选中的同一股票或同一 option contract，记录可负担性、最大数量、风险比例和拒绝原因，不重新选择工具。看跌 shadow benchmark 统一命名为 `short_equity_counterfactual`，不创建账户或订单，且 PnL 不与 long put 合并。

### 评估、可视化和文档

- 新增按 horizon 隔离、label maturity 安全的 expanding walk-forward split，主要指标为 out-of-sample multiclass Brier score 和 log loss；ECE/reliability curve 仅作诊断。当前不拟合 calibrator，不显示 probability EV，每条记录保留 calibration version、training cutoff、sample size 和 horizon。
- 绩效增加 round-trip 成本恒等式：midpoint gross PnL 减 spread、slippage/tick 和 commission 必须等于 executable net PnL。dashboard 新增独立 `$10,000` allocator、同工具 `$2,000` 对照、看跌影子基准、mandate 和未校准概率边界，并对 `null`/损坏状态降级显示而不返回 HTTP 500。
- 正式规范见 `references/ai_instrument_allocator_policy.md`；README、SKILL、options/news policy、JSON Schema 和中文 Mermaid 架构图已同步。
- 最终 `compileall` 通过，pytest 为 `217 passed`，仅有 4 条上游 `exchange_calendars` deprecation warning。只读 healthcheck/readiness 确认 Robinhood 54-tool manifest、期权数据、Exa、DeepSeek、Alpaca fallback 和 Vibe 均 ready，`ready_for_ai_instrument_allocator_paper=true`、`ready_for_full_forward_evaluation=true`、`paper_mode=true`、`live_trading=false`。
- 三套离线 dry run 均为 `used_network=false`、`used_live_order_tools=false`；allocator 的 overnight、premarket、pre-open、09:32 execution、monitor 和 restart 临时目录验证为 6 项通过。执行时钟会推进到本轮最晚的股票或期权报价观察时间，新增回归确认晚到 5 秒的期权报价不会再被误拒为 future quote。Playwright 在 1440x1000 和 390x844 下确认无页面横向溢出、0 console error，并修复浏览器取消刷新时的 `ConnectionAbortedError` 终端 traceback。`graphify update .` 完成为 2,434 nodes / 6,380 edges。
- 测试后没有保留 forward service 或 dashboard 进程。连续 scheduler 配置只有在用户从自己的终端重新启动服务后才会生效。
- 审计发现旧 `--readiness` 会因构造完整 service 而提前初始化空白 allocator state。本次运行已在 19:17 生成新的 `$10,000` 空白 namespace；它没有订单、成交或 PnL，且没有触碰旧账本，因此予以保留。CLI 已改为直接调用无状态 healthcheck，文件哈希验证后续 readiness 前后 state 完全一致。

## 2026-08-16 (America/Los_Angeles) - 亏损根因修复、DeepSeek V4 Flash 迁移与方向性评估

### 运行和亏损证据

- 审计期间服务已由用户停止；本次没有接管、启动或修改连续运行进程。主模拟账户累计已实现损益为 `-$108.8394`，其中股票线 26 笔已平仓、`-$84.84`、胜率 `38.46%`、profit factor `0.3432`；期权线 7 笔已平仓、`-$24.00`、胜率 `42.86%`、profit factor `0.4286`。独立 AI sleeve 为 6 笔已平仓、`-$48.8163`、胜率 `16.67%`、profit factor `0.1275`。
- 两笔历史 EOD 异常宽价差约放大亏损 `$44.17`；该问题已在上一提交通过报价 provider/fallback 和最大退出价差保护修复，历史 append-only 记录未回写。
- 21 笔可比股票往返交易的标的 midpoint 变化约 `-$17.38`，模拟执行拖累约 `-$18.17`，对应已实现约 `-$35.53`。因此亏损同时来自信号边际不足和交易成本，而不是单一成交模型错误。
- AI sleeve 的主要可执行性缺陷是模型写出的条件入场没有被机器执行：例如输出“价格低于某值再买”，旧代码仍可能按当前 ask 立即下单；止损后的同日 ticker 也可再次进入完整研究和下单路径。两类缺陷均已封闭。
- 期权历史结果中，合约 midpoint 变化合计约 `+$20.50`，但实际模拟结果为 `-$24.00`，估算 spread/slippage 拖累约 `$44.50`。5 笔 put 合计 `-$1.00`，2 笔 call 合计 `-$23.00`；当前样本不能证明“看跌判断更准”，只能支持将看涨和看跌独立计量。

### 修改内容

- API provider 默认模型迁移到 `deepseek-v4-flash`。新闻提取、候选排序和 Challenge 使用非 thinking；只有 Catalyst 与 AI gated 的最终 Decision Manager 使用 thinking。所有 API key 仍只从环境变量读取。
- AI gated 最终决策 schema 新增 `entry_now`、明确的最小/最大入场价和最长 5 分钟有效期。执行前刷新正股 ask；价格越界、条件尚未成立、授权过期或字段缺失时保持 `no_trade`，不会创建订单。
- 止损成交后的 ticker 在同一交易时段进入 cooldown，并在 Exa/LLM 前和最终执行前各检查一次，阻止同日止损后重新追入。
- AI 深度研究候选从 2 个提高到 3 个，其中在存在合格负面候选时至少保留 2 个名额；没有人为降低最终风险门槛，也没有扩大 long premium 之外的期权权限。
- 期权最大相对价差由 6% 收紧到 4%。合约筛选会保存精确拒绝原因；方向绩效按 bullish equity/call 与 bearish put 分开统计，包含提案数、成交数、拒绝原因、已平仓 PnL 和成本。
- `weighted_relative_strength_v2` 改为 `shadow_only`，继续产生候选和 360 分钟后收益标签，但不再进入 paper broker。`relative_strength_v1` 仍保留为原始 deterministic baseline，未自动替换 active strategy。
- Hawkes Process 暂不进入执行 pipeline。当前约 211 个离散新闻事件分布在约 165 个 ticker，且 Exa 为分钟级轮询，不具备可靠估计自激过程所需的逐笔成交、订单簿或高密度同类事件序列。评估和未来数据门槛记录在 `references/hawkes_process_assessment.md`。
- 中文架构文档补充总执行边界、无前视时间轴、股票影子决策树、期权流程、AI gated 流程、订单状态机、方向绩效和 Hawkes 后续实验图。dashboard 同步显示当前配置的“只观察”边界、AI 看涨/看跌提案和净结果。

### 安全、验证和部署

- 模式仍为 `paper=true`、`live_readonly=false`、`live_trading=false`。LLM 不能直接创建订单、修改风控、扩张 universe 或调用 Robinhood 写工具；所有可执行提案仍需经过 deterministic risk gate。
- deterministic forward dry run：网络调用 0、真实下单工具调用 0、股票订单 0、股票持仓 0、期末净值 `$2,000`，并确认 weighted lane 为 `shadow_only`。
- options dry run：网络调用 0、真实下单工具调用 0；固定 fixture 的 put 以 ask 加不利滑点买入、以 bid 减不利滑点退出，测试损益 `+$18`。该结果只验证闭环和成本方向，不是策略盈利证据。
- 真实 DeepSeek Flash API pilot：4 次模型调用、0 error、1 次 structured retry；总延迟约 `40.886s`，8,368 input tokens、4,586 output tokens，按当前配置估算 `$0.0024556`。fixture 没有 Exa 或市场数据调用，没有创建 paper order，也没有调用真实下单工具。
- 全量 pytest 为 `179 passed`，仅有 4 条上游 `exchange_calendars` deprecation warning；`compileall` 通过。只读 readiness 确认 Robinhood MCP、Exa、Alpaca fallback、DeepSeek、期权数据和 Vibe 均 ready，`ready_for_full_forward_evaluation=true`。`graphify update .` 已重建 1,954 nodes / 5,327 edges；连续服务必须由用户在自己的终端重新启动，新的 scheduler 和模型配置才会加载。

## 2026-08-11 (America/Los_Angeles) - Forward results audit and execution-quality repair

### Runtime and performance evidence

- The supervisor remained healthy and paper-only. From `2026-08-10T12:18:15Z` through this audit it completed 2,665 supervised jobs with no failure or timeout in that interval. It nevertheless spawned 1,021 idle EOD workers, 1,133 mostly skipped news-drift workers, 226 forward workers, and 218 AI monitor workers.
- The shared main account is flat with `$1,919.8167` cash and raw realized PnL of `-$80.1834` (`-4.0092%`). It has 14 sessions and 24 closed trades, below the configured 20-session and 30-trade evidence thresholds.
- The main equity line reports 20 closed trades, `-$67.1833`, 40% win rate, and 0.3703 profit factor. Two August 10 mandatory exits used Alpaca IEX quotes with 4.29% and 5.39% spreads. Their bid-to-last gaps alone overstated loss by about `$44.17`; the immutable raw account and logs remain unchanged.
- The main long-premium options line reports 4 closed trades, `-$13.00`, 50% win rate, and 0.5185 profit factor. The latest TLT long put made `$4.00`; the sample is too small for threshold tuning.
- The isolated AI sleeve is flat with `$1,987.8623` cash and `-$12.1378` realized PnL (`-0.6069%`). It has 3 closed trades, 33.33% win rate, and 0.3701 profit factor. On August 10 it bought EMBJ, lost `$14.9679`, then repeated the full Exa/DeepSeek research path while already holding EMBJ before deterministic position risk rejected the duplicate order.
- The news-drift lane remains shadow-only: 131 events, 23 proposals, 50 valid return labels, 7 portfolio days, and no broker orders. All five available next-close labels still use the legacy label policy; the new executable-preclose policy does not yet have a next-close sample.
- A fresh read-only healthcheck found Alpaca, Exa, DeepSeek, and Vibe ready, but the persisted Robinhood MCP OAuth session now requests authorization in new processes. Main equity can use Alpaca fallback; options, scanner discovery, Catalyst, AI gated research, and news drift are degraded until OAuth is refreshed.

### Changes

- EOD equity quotes now follow the configured provider order instead of hard-coding Alpaca first. A missing, invalid, or wider-than-allowed primary quote is audited and retried through the configured fallback.
- Equity exits continue to ignore entry-only volume, price-floor, and universe filters, but now fail closed when spread exceeds the new `max_exit_spread_bps: 100` execution-quality limit.
- AI gated discovery removes tickers with an existing sleeve position or active equity/option order before market context, Exa, or DeepSeek work. The skip reason remains visible in cycle output.
- The supervisor now suppresses forward, EOD, and AI monitor subprocesses outside regular hours. Catalyst and AI research run only during regular hours or their bounded premarket windows; news drift runs only during its configured premarket, regular, or after-hours windows.
- Strategy scores, entry thresholds, position limits, stop rules, and adaptive weights were not changed. The available clean trade sample is not sufficient to distinguish a profitable parameter change from overfitting.

### Safety, validation, and deployment

- Paper/live mode boundaries are unchanged: `paper=true`, `live_readonly=false`, `live_trading=false`. No Robinhood order, review, replace, or cancel capability was added or called.
- Historical state, fills, account cash, OAuth material, SQLite records, and append-only logs were not edited.
- Focused broker, AI, and orchestrator regression suite: `82 passed`. Full suite: `170 passed`; only four upstream `exchange_calendars` deprecation warnings remain. `compileall` passed.
- Read-only healthcheck confirms the supervisor heartbeat and lock are healthy, but full forward readiness is false because Robinhood OAuth must be refreshed.
- Child workers load the EOD and AI fixes immediately, but scheduler window suppression lives in the long-running supervisor process. Restart the service from the user's terminal after reauthorizing Robinhood MCP.

本文件是项目的持续开发日志。后续任何改变代码、配置、运行行为、数据源、风险边界或评估口径的更新，都应在文件顶部追加一条记录，不覆盖历史记录。

每条记录至少包含：变更原因和运行证据、修改内容、安全影响、验证结果、是否需要重启。纯格式调整可以合并记录，但不能省略会影响交易决策或绩效统计的变化。

## 2026-08-06 (America/Los_Angeles) - Overnight recovery and entry accounting repair

### Runtime evidence

- The August 5 pre-close guard could not obtain Robinhood or Alpaca quotes during a DNS outage, so AVGO, JPM, TLT put, and the isolated AI XLV position remained overnight.
- On August 6 the global overnight flag closed a newly opened MSFT position after one minute. Five entry and exit fills then exhausted the shared daily limit, causing 167 equity and 73 option entry rejections.

### Changes

- Overnight recovery now targets only positions opened before the current session. Current-session positions and pending orders are left alone.
- Both the main and AI paper lanes block new entries while their own overnight recovery is incomplete.
- Daily counters now count only equity buys and option buy-to-open fills. Exits still update realized PnL but do not consume entry capacity.
- Equity exits bypass entry-only spread, price-floor, liquidity, and universe filters while retaining fresh, valid, non-halted quote checks.
- Strategy pipelines short-circuit before quote refresh, option-chain selection, or order creation when daily entry limits are already exhausted.
- News-drift close labels target the configured pre-close liquidation time and defer abnormal-spread observations; old immutable labels remain unchanged.
- The existing Catalyst ranker output budget increased from 900 to 1600 tokens so its structured retry can use the existing 3072-token ceiling.

### Safety and deployment

- Paper-only and live-trading-disabled boundaries are unchanged. No broker write tool was added.
- The AI isolated sleeve skips Exa and LLM research when every permitted entry line is already blocked by recovery or daily limits; one remaining eligible line still permits research.
- The service was stopped before this repair. A restart is required so the supervisor loads the corrected scheduler and recovery code.

### Validation

- Focused broker, options, orchestrator, AI-sleeve, and news-drift tests passed.
- Full suite: `166 passed`; only four third-party `exchange_calendars` deprecation warnings remain.
- `compileall` passed; runtime mode remains `paper=true`, `live_readonly=false`, `live_trading=false`.
- `graphify update .` rebuilt the local code graph without an API call.

## 2026-08-05 (America/Los_Angeles) - 连续运行检修与新闻/期权时间修复

### 运行证据

- forward service 和 dashboard 自 2026-08-04 启动后持续存活；heartbeat 每 30 秒更新，模式仍为 `paper=true`、`live_trading=false`。检修没有停止或接管用户进程。
- 本次服务启动以来共检查到 3,113 个已结束 scheduler job：全部 `completed`，0 failed，0 timeout。dashboard `/api/state` 正常返回。
- 主账户已有 10 个 forward session、14 笔已平仓交易，累计 realized PnL `-$10.9104`、净回报 `-0.5455%`、profit factor `0.7836`、最大回撤 `2.0629%`、0 rule violation。股票线 13 笔、`+$4.0898`；期权线 1 笔 TLT long put、`-$15`。样本仍低于 20 session/30 trade 门槛。
- news-drift 在一个交易日内执行了 630 次 Exa Search、处理 3,780 条原始结果、形成 31 个 signal，但 0 proposal。主要原因不是模型结论，而是 Exa 日期级发布时间被标准化为当日 `00:00` 后触发两小时年龄拒绝，以及盘前报价 stale 后没有开盘重验。
- 期权诊断共检查 13,540 个合约，其中 901 次在筛选阶段被判为 future quote。实际 paper order 中还有 19 次 future quote rejection 和 39 次“已有期权仓位”拒绝。样本证明一笔报价在本地 `19:36:48.818` 收到、远端时间为 `19:36:49.133`，筛选通过 0.315 秒容差后，下单阶段却使用旧的整秒 `19:36:48` 再次校验并误拒。

### 修改内容

- news-drift worker 仍每 60 秒解析到期标签，但 Exa market discovery 增加持久化 15 分钟限流；三个查询按 discovery interval 正确轮换，不再每分钟付费轮询重复结果。
- Exa evidence 新增 `published_at_precision`。日期级发布时间保留原值，并以 `first_seen_at` 作为保守 actionable time；精确发布时间和 `event_at` 的语义不变。
- `signal_latency` 改为衡量 `first_seen_at -> signal_time`，不再因稍后重验行情而虚增。盘前仅因 stale/missing quote 被拒的 signal 会在正常交易时段用新行情重验，无需再次调用 LLM。
- 期权 entry/monitor 在网络返回后使用微秒级本地观察时间，保留既有 1 秒未来报价容差和真正 future quote 的 fail-closed 行为。
- 当期权仓位数量已达上限或已有 open option order 时，策略在 earnings/chain/quote 网络调用和 paper order 创建前直接输出 `no_trade`，不再制造重复 rejected order。

### 安全和验证

- 没有改变 `paper=true`、`live_trading=false`、long-premium-only、无 margin、无 short option、无真实 broker write surface 等边界；历史订单、成交、SQLite ledger 和 append-only 日志均未修改。
- 新闻、Exa、期权和 forward 集成定向测试：`63 passed`；全量测试：`159 passed`。仅有 4 条 `exchange_calendars` 上游 deprecation warning。
- 运行中的 supervisor 不需要重启：网络作业由新 Python 子进程加载代码和配置。若将来修改 supervisor 自身 scheduler 周期，才需要由用户在原终端重启。

## 2026-08-04 (America/Los_Angeles) - 独立价格盲新闻漂移 Shadow Pipeline

### 目标和运行证据

- 按 `LLM_NEWS_DRIFT_OPTIMIZATION_PLAN.md` 的结构要求新增独立 `llm_news_drift_v1`，解决 AI 只能在技术 baseline 已筛出候选后才介入的问题。
- 开发前后保持 `paper=true`、`live_trading=false`。本次检查时 `state/forward_service.lock` 不存在，未发现 forward service 或 dashboard 进程，因此没有停止或接管用户进程。
- 现场 readiness 验证显示 Robinhood MCP live probe 正常、53 个工具可见、discovery 和 options 所需只读方法无缺失；Exa、DeepSeek、Alpaca fallback、Vibe 均 ready；`ready_for_full_forward_evaluation=true`、`ready_for_news_drift_shadow=true`。
- 子智能体使用实际可用的 `gpt-5.6-luna`、最高推理强度完成 SQLite event ledger 和定向测试；没有使用不存在的 `5.6-luma` 标识，也没有引入第三方数据库依赖。
- 官方论文核对为 Lopez-Lira 与 Tang 的 *Can ChatGPT Forecast Stock Price Movements?*。Mendeley Data v2 DOI 为 `10.17632/f39x226htv.2`，包含两个约 502 MB ZIP，许可为 CC BY 4.0。当前没有下载整包或宣称完成论文复现。

### 修改内容

- 新增 `scripts/news_drift/event_store.py`
  - 标准库 SQLite 六表 ledger：events、relations、signals、tradability、shadow proposals、outcome labels。
  - 支持 `new_event`、`duplicate`、`clarification`、`material_update`、`contradiction`、`follow_up` 和跨重启幂等。
  - 文件数据库启用 WAL；所有 proposal 状态固定为 `shadow_only`。
- 新增 `scripts/agents/news_drift_headline_agent.py`、prompt、strict JSON Schema 和 mock provider 输出。
  - 一次调用完成 ticker mapping、direction、event type、materiality、novelty、ambiguity、confidence 和 event relation。
  - 模型输入只有 headline/source/time/hint 和最近事件标题，没有 price、quote、volume、technical、position 或 account。
- 新增 `scripts/news_drift/pipeline.py`
  - 每周期轮换一个全市场 Exa query，不依赖固定 ticker 或 baseline candidate。
  - raw evidence 使用独立 immutable snapshot、URL/content/event 去重和 24 小时 event cooldown。
  - LLM 后才执行 exact US instrument、market cap、ADV、bid/ask、spread、quote freshness、event age、signal latency、pre-event reference 和 initial-reaction 检查。
  - regular session、盘前 120 分钟和盘后 120 分钟按 60 秒运行；不会整夜每分钟调用 Exa。
  - 第一阶段仅产生正面事件 long-equity shadow proposal，参考预算 `$2,000`、单笔最多 25%，支持项目既有 `0.001` fractional increment；不创建 paper order。
  - +1m、+5m、+15m、same-day close、next close 和 second close 标签使用 bid/ask、双向不利滑点及 commission；目标前报价和超过 tolerance 的报价不能伪装成精确 horizon。
- 新增 `scripts/evaluation/evaluate_news_drift.py`
  - 分开统计 event、firm-day、portfolio-day 和每个 horizon。
  - 分开保留 gross、spread/slippage-adjusted net、observed cost、break-even cost 和 0/5/10/25/50 bps sensitivity。
  - 按 event type、direction、source tier、market-cap bucket 分组；至少 100 个有效标签及 20 个 portfolio day 才结束 evidence-insufficient 状态。
  - Exa 合同单价未配置时显示 unpriced，不当作零成本；API pilot usage 不混入正式策略成本。
- 新增 `scripts/evaluation/run_news_drift_api_pilot.py`
  - 使用两个固定 headline 做一次真实或 mock structured-output 调用，不调用 Exa、市场数据或 broker。
- `forward_paper_service.py`
  - 增加 `--news-drift-once` 和每分钟 supervised worker，120 秒 hard timeout，独占 `news_event_store` 资源。
  - one-shot 直接构造 news-drift 所需组件，不构造主账户或 AI sleeve paper broker。
  - `--evaluate-once` 同时生成独立 news-drift metrics/report。
- `healthcheck.py` 和 dashboard
  - readiness 新增 news-drift lane；完整 forward ready 会包含该已启用线路。
  - 初学者 dashboard 新增“新闻漂移影子实验”，显示事件、提案、有效标签、次日收盘净收益和明确的“不下单”说明。
- `EvidenceSnapshotStore` 增加向后兼容的 namespace 参数；catalyst 默认路径不变，news-drift 使用自己的 snapshots 和 cooldown state。
- 新增 `references/llm_news_drift_policy.md`，并同步 README、SKILL、data-source policy 和完整架构文档。
  - 当前 Exa 使用 Search + inline `contents.highlights`；Deep Search、Agent、Monitors 和独立 Contents endpoint 不进入一分钟关键路径。
  - P1 exact replication、`short_equity_counterfactual`、`negative_news_long_put` 和 adaptive event calibration 被明确列为隔离后续实验，不与 base shadow PnL 混合。

### 安全影响

- 新策略没有 broker 属性、账户、position、order 或真实交易工具；独立 one-shot 测试确认不会创建 `paper_account.json` 或 `paper_orders.json`。
- LLM 不能看到价格，不能直接创建 proposal；ticker、tradability、成本和预算全部由后置 Python 决定。
- `paper_orders_created` 始终为 0，`live_order_tools_called` 始终为 false。负面新闻目前只保存，不能产生股票空头或 long put 订单。
- 主股票、主期权、AI sleeve 的策略、账户和订单状态均未被 news-drift 读取或修改。

### 验证

- news-drift event store、pipeline、evaluation、API pilot 定向测试全部通过，覆盖严格 schema、价格盲输入、重复事件跨重启抑制、stale/future/target-before quote、无前视 bar、fractional sizing、成本后标签和账户隔离。
- 全量 pytest：`154 passed`；仅有 4 条 `exchange_calendars` 上游 deprecation warning。
- 真实 DeepSeek price-blind pilot：1 次调用，约 `3921.898 ms`，888 input tokens、271 output tokens、估算 `$0.00062205`，0 errors、0 retries；market-data calls 0、paper orders 0、live order tools 0。
- dashboard API、桌面 viewport 和 `390x844` 移动 viewport 使用 Playwright 验证；中文可读、布局无重叠，新策略明确显示“只观察”。
- `git diff --check` 通过；runtime state、OAuth、`.env.local`、logs、SQLite 和 screenshots 均由 `.gitignore` 排除。

### 部署和未完成项

- 当前 service 未运行。代码验证完成后，需要用户在自己的终端重新启动 continuous service；新 scheduler 只有重启后才会加载。
- 进入设定窗口前可先执行 `--readiness`；实际新闻发现只在 regular、开盘前 120 分钟和收盘后 120 分钟运行。
- 论文 502 MB replication package 尚未下载、解压、阅读 README 或复现原表；当前只完成与其聚合和成本概念兼容的 forward 指标。
- P2 三条实验尚未实现交易或收益合并。必须先积累基础事件样本，再分别建立 direct-short counterfactual、long put 和 walk-forward calibration，避免后验选择方向或污染主账户。

## 2026-08-03 (America/Los_Angeles) - 连续运行检修、期权可交易性和指标口径修复

### 运行证据

- 检查了从 2026-08-02 启动至本次检修时的父进程、子进程、进程锁、heartbeat、APScheduler 作业、forward stage、订单、成交、持仓、LLM usage、期权筛选诊断和 dashboard API。
- 服务保持 `paper=true`、`live_trading=false`。检修期间没有停止用户启动的服务，也没有调用 Robinhood 的 review、place 或 cancel 工具。
- 运行窗口内没有 scheduler job failure 或 worker timeout。少量 job skip 均为显式资源冲突，例如 EOD guard 等待 main account，属于 fail-closed 协调结果。
- 最近完成的常规交易日实现约 `+$12.45` 已实现损益；累计约 `-$12.78`，累计回报约 `-0.64%`。当前约 9 个 forward session、11 笔已平仓交易，胜率约 `54.55%`、profit factor 约 `0.64`、最大回撤约 `2.06%`。
- 上述样本仍低于 `config/evaluation.yaml` 的 20 个 session 和 30 笔已平仓交易要求，不能得出策略稳定盈利的结论。
- 股票订单中有 38 次被共享总风险上限拒绝，其中 37 次发生在最近交易日。它们大多在创建订单前已经可以确定不会通过，因此属于无效订单和日志噪声。
- 期权线完成了大量方向判断和合约筛选，但没有生成订单。原观察池中的 NVDA、AAPL 等标的一张符合 delta 和流动性条件的合约通常超过约 `$187.60` 的单笔 premium 预算；XLE 等较便宜合约又常因成交量、持仓量或价差失败。
- Robinhood MCP 只读检查确认 F、BAC、SOFI、XLF、SLV、TLT、XLE 具有普通股票或非杠杆 ETF 属性、较高现货成交量和可用期权链，因此适合作为 `$2,000` 纸面账户的独立期权标的池。该检查没有访问真实账户资金或真实订单。
- DeepSeek usage 中发现 2 次模型错误：一次 News Agent 超时；一次 Challenge thinking 耗尽输出长度，随后结构化重试仍使用 thinking 而再次失败。
- Robinhood 主报价源曾有一次 OAuth 请求失败，Alpaca IEX fallback 成功完成该周期。未发现重复的未来时间戳误拒问题。

### 修改内容

- `scripts/risk/shared_portfolio_risk.py`
  - 新增 `shared_entry_capacity`，在订单创建前计算共享总上限和单策略线上限下的剩余可部署金额。
- `scripts/orchestrator/forward_paper_service.py`
  - 股票和期权改用独立观察池，并仅用二者并集收集必要行情。
  - 股票策略只处理股票观察池，期权策略只处理期权观察池。
  - 股票下单前先做共享资金容量预检。明显不可能通过的订单记录为 `weighted_order_skipped`，不再写成 paper broker 的 rejected order。
  - 周期结果新增股票和期权观察池数量，便于 dashboard 和审计识别覆盖范围。
- `config/equity_universe.yaml`
  - 新增独立 `options_watchlist`：F、BAC、SOFI、XLF、SLV、TLT、XLE。
  - 将 SLV、TLT 声明为普通 ETF，仍排除杠杆和反向 ETF。
- `scripts/adapters/robinhood_option_market_data_adapter.py`
  - 当合约在 premium budget 前已通过其他条件时，记录最低可用 premium、最便宜合约和预算缺口，不再只显示笼统的 `premium_budget` 计数。
- `scripts/llm/api_provider.py`
  - 保留首次 Challenge/Decision thinking；若结构化输出因 `finish_reason=length` 失败，重试自动关闭 thinking，优先获得可校验 JSON，而不是重复耗尽 reasoning token。
- `config/llm.yaml`
  - News Agent 和 AI-gated News Agent 的请求上限由 30 秒调整为 45 秒，仍由 supervisor 的外层硬超时约束。
- `scripts/discovery/ai_gated_pipeline.py`
  - 期权选择失败时保留经过安全处理的异常消息，便于区分数据源失败和无合约通过筛选。
- `scripts/evaluation/calculate_metrics.py`
  - `fill_rate` 和 `unfilled_rate` 的分母改为真正进入执行生命周期的订单，不再把 deterministic risk rejection 当作未成交。
  - 新增 `execution_order_count`；总订单数和拒绝数仍单独保留，风险拒绝不会被隐藏。
- `scripts/runtime/healthcheck.py`
  - 区分 `runtime_healthy`、核心股票 `forward_ready` 和 `full_forward_evaluation_ready`。
  - 当主报价只能依靠 fallback，或已启用的期权、catalyst、AI-gated 线路未就绪时，明确输出 `operational_status=degraded` 和原因，顶层 `ok` 不再掩盖部分策略线停摆。
- `tests/agent_evals/test_agent_pipeline.py`、`tests/test_options_paper.py`、`tests/test_vibe_integration.py`
  - 新增 thinking 长度失败重试、共享容量、期权预算诊断、独立观察池、下单前容量预检和指标分母测试。
- `README.md`、`SKILL.md`、`references/data_sources.md`
  - 修正文档与当前代码不一致的入口、adaptive sample 数量和 Robinhood 只读 allowlist。
- `.gitignore`
  - 忽略 generated `output/`，避免将本地绩效报告或派生运行数据误提交到 Git。
- `PROJECT_ARCHITECTURE.md`
  - 新增完整自然语言架构和 pipeline 说明。

### 安全影响

- 保持纯 paper 模式；没有新增任何真实券商写方法。
- LLM 仍不能创建订单、修改风险配置、扩大未验证 ticker universe 或绕过 deterministic risk gate。
- 新增低价期权标的只提高“一张合约可能落入预算”的概率，不降低 DTE、delta、spread、volume、open interest、Greeks、IV、事件和共享资金限制。
- 共享账户总部署上限、股票线上限、期权线上限和每日总 entry 次数保持不变。

### 验证

- 定向回归：`68 passed`。
- 全量测试：`138 passed`，只有来自 `exchange_calendars` 的 4 条上游 deprecation warning。
- 股票 forward dry run：完成 research、weighted decision、risk、paper fill、journal 和 metrics，`used_network=false`、`used_live_order_tools=false`。
- long-put options dry run：完成 buy-to-open、sell-to-close、共享虚拟现金和独立 options PnL，`used_network=false`、`used_live_order_tools=false`。
- catalyst dry run：形成有 deterministic risk approval 的 shadow proposal，但 `paper_orders_created=0`，`used_network=false`、`used_live_order_tools=false`。
- dashboard `/api/state` 可访问，heartbeat 为 `ok`，service lock 的 PID 存活，页面数据明确显示 `paper=true`、`live=false`。
- 当前 healthcheck 显示 `runtime_healthy=true`、股票 `forward_ready=true`，但 `operational_status=degraded` 和 `full_forward_evaluation_ready=false`。原因是 Robinhood OAuth 需要重新授权；Alpaca fallback 仍可支持股票报价，但期权、scanner discovery、catalyst 和 AI-gated 完整流程暂不可用。
- 已运行 `graphify update .`：知识图重建为 1,698 个节点、4,531 条边和 119 个 community。4 个 fixture JSON 因不包含可提取代码节点而产生 warning，不影响代码图更新。

### 部署说明

- 本次检修没有停止当前服务。为确保 supervisor 及所有后续 worker 使用同一版本，验证完成后应由用户在原终端按 `Ctrl+C` 正常停止，再重新运行 continuous service。

## 2026-07-25 - 独立 Exa + DeepSeek catalyst shadow 策略

- 新增 `exa_deepseek_catalyst_v1`，从 bounded discovery pool 独立发现事件和 ticker，不依赖 active baseline 先发出 buy。
- 引入 Exa 48 小时证据窗口、URL/event/content 去重、ticker cooldown、event cooldown、primary-source verification 和 timestamped snapshot。
- 策略保持 shadow-only，不创建 paper order，也不能替换 active strategy。

## 2026-07-25 - 股票和 long-premium 期权 paper 双线

- 新增 long call/long put paper broker、期权 bid/ask fill、合约筛选、Greeks/IV 校验、到期前强制退出和独立统计。
- 股票和期权拥有独立订单、持仓、日志和 PnL，但共享主虚拟账户现金、总部署比例和每日 entry 上限。
- 明确禁止 short option、sell-to-open、spread、margin、0DTE、exercise 和 assignment。

## 2026-07-19 - 初始 paper/shadow 系统

- 建立 `$2,000` 虚拟账户、股票 paper broker、订单状态机、滑点模型、风险 gate、append-only JSONL、historical replay、forward cycle 和基础评估。
- 建立 `relative_strength_v1` deterministic baseline 和 provider-neutral multi-agent shadow comparison。
- 从初始版本开始，真实 Robinhood 下单不属于项目能力范围。
