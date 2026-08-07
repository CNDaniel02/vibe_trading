# Development Log

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
  - P1 exact replication、`synthetic_short_equity`、`negative_news_long_put` 和 adaptive event calibration 被明确列为隔离后续实验，不与 base shadow PnL 混合。

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
- P2 三条实验尚未实现交易或收益合并。必须先积累基础事件样本，再分别建立 synthetic short、long put 和 walk-forward calibration，避免后验选择方向或污染主账户。

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
