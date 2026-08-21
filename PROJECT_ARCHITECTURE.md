# Auto Trading Skill 项目结构与完整 Pipeline

## 1. 项目是什么

`auto-trading-skill` 是一个使用真实或接近实时市场数据、但只使用本地虚拟资金成交的美股和 long-premium 期权研究系统。它的目的不是证明某个模型会赚钱，而是把“收集数据、形成候选、研究证据、做出决策、通过风控、模拟成交、管理持仓、退出、记账、评估”连接成可以长期运行和审计的闭环。

历史主账户和旧 AI-gated sleeve 的虚拟本金均为 `$2,000`，其账户、订单、成交、PnL 和日志保持原样，不迁移也不改写。`long_directional_options_v2_weighted` 与 `ai_gated_technical_v1` 已停止新增 entry，但已有 open order 和 position 仍由各自原 monitor/exit logic 管理直到清仓。新策略 `ai_instrument_allocator_v1` 使用完全独立的 `$10,000` namespace；它不会重置或借用旧账户。

项目目前不能进行真实交易。`config/paper_mode.yaml` 固定声明：

```yaml
mode:
  paper: true
  live_readonly: false
  live_trading: false
```

Robinhood 连接只用于显式 allowlist 内的只读市场数据。项目 broker 只有本地 paper broker，不提供真实订单 review、place 或 cancel 接口。即使 LLM 输出 `buy`，也必须经过 Python 风险引擎，最后只能写入本地虚拟账户。

## 2. 系统中的策略线

系统不是单一 Agent，而是几条目的不同、状态隔离程度不同的策略线。

| 策略 | 当前角色 | 是否可创建本地 paper order | 账户 |
| --- | --- | --- | --- |
| `weighted_relative_strength_v2` | 股票确定性候选和 360 分钟标签 | 否，暂停为 shadow-only | 无新增资金占用 |
| `long_directional_options_v2_weighted` | 旧 long call/put 加权策略 | 否；仅管理已有订单和仓位 | 历史主账户 |
| `relative_strength_v1` | 原始股票 baseline | 否，shadow comparison | 无独立资金 |
| `long_directional_options_v1` | 原始期权 baseline | 否，shadow comparison | 无独立资金 |
| `multi_agent_relative_strength_v2_candidate` | 对 active 股票候选做 LLM 对照 | 否，shadow-only | 无独立资金 |
| `exa_deepseek_catalyst_v1` | 独立发现事件和交易机会 | 否，shadow-only | 无独立资金 |
| `llm_news_drift_v1` | 全市场新闻优先的价格盲漂移实验 | 否，shadow-only | 独立参考预算，无账户 |
| `ai_gated_technical_v1` | 旧技术前排加 Exa/DeepSeek 策略 | 否；管理旧仓并继续 shadow 决策 | 历史 `$2,000` AI sleeve |
| `ai_instrument_allocator_v1` | signed-return 预测加股票/期权工具分配 | 是，仅本地 paper | 独立 `$10,000` sleeve |

股票和期权仍会并行筛选；旧两条加权执行线停止新增仓位，新 allocator 可以在自己的 `$10,000` sleeve 中从同一份 signed signal 比较 long equity、long call 或 long put。任何可执行线路都不能各自把账户资金用满，因为 `scripts/risk/shared_portfolio_risk.py` 会同时执行：

- 账户总部署金额上限；
- 股票线部署金额上限；
- 期权线部署金额上限；
- 两条线合计的每日 entry 次数上限；
- open order 预留资金；
- 缺失持仓报价时 fail closed。
- 股票与期权合计最多 3 个 executable position、每日最多 3 次 entry；
- 同一 underlying 同时只能有一个 executable equity 或 option exposure。

AI sleeve 的状态目录、订单、持仓、daily counter、journal 和 metrics 都有独立 namespace。它的收益不能被加到主账户收益中。

## 3. 总体架构

```mermaid
flowchart TD
    S["APScheduler supervisor"] --> F["5 分钟主 forward cycle"]
    S --> A["旧 AI-gated monitor + shadow research"]
    S --> IA["Allocator 两速研究与执行"]
    S --> C["每小时 catalyst shadow cycle"]
    S --> N["每分钟 news-drift shadow cycle"]
    S --> M["5 分钟 AI sleeve monitor"]
    S --> E["1 分钟 EOD guard"]
    S --> R["30 分钟 evaluation"]
    S --> H["30 秒 heartbeat"]

    D["Robinhood MCP 只读数据"] --> SNAP["timestamped snapshot"]
    P["Alpaca IEX fallback"] --> SNAP
    V["Vibe OHLCV"] --> SNAP
    X["Exa 外部证据"] --> AI["DeepSeek structured agents"]
    X --> NH["价格盲 headline classifier"]

    SNAP --> EQ["股票确定性评分"]
    SNAP --> OP["期权方向和合约筛选"]
    SNAP --> AI

    EQ --> EL["360 分钟净成本标签"]
    EL --> EV["shadow 评估"]
    OP --> RG
    AI --> RG
    AI --> SB["signed return buckets"] --> RP["期权情景重新定价"] --> RG
    NH --> NT["ticker + tradability checks"]
    NT --> ND["isolated SQLite shadow ledger"]
    RG --> PB["local paper brokers"]
    PB --> ST["state + append-only logs"]
    ST --> MON["monitor + exits"]
    ST --> MET["metrics + dashboard + journal"]
```

当前可执行边界可以简化为：

```mermaid
flowchart LR
    W["weighted equity v2"] --> WS["shadow candidate only"]
    O["weighted long options v2"] --> OR["option risk gate"] --> M["主 paper 账户"]
    A["旧 AI gated"] --> AC["entry frozen；monitor/exit + shadow 决策"] --> S["历史 $2,000 AI sleeve"]
    IA["AI instrument allocator"] --> AR["共享 deterministic risk"] --> NS["独立 $10,000 sleeve"]
    C["catalyst / news drift / baselines"] --> CS["shadow ledgers only"]
    M -. "无真实 broker 方法" .-> L["本地 JSON/JSONL"]
    S -. "无真实 broker 方法" .-> L
```

网络调用不会直接运行在长期 supervisor 的主线程中。`scripts/orchestrator/forward_paper_service.py` 通过 `scripts/runtime/subprocess_runner.py` 为每个网络密集型周期启动有硬截止时间的子进程。超时会终止完整子进程树，并在 `logs/runtime_jobs.jsonl` 和 heartbeat 中留下失败证据。

`--readiness` 和 `scripts.runtime.healthcheck` 只检查配置、凭据可用性与只读数据源，不构造 stateful forward service，也不会初始化任何策略 sleeve。新的 allocator 账户只在实际 allocator stage 或 monitor 首次运行时建立。

各作业声明自己会使用的资源，例如 `main_account`、`ai_account`、`allocator_account`、`evidence_store` 和 `news_event_store`。资源冲突时作业会明确记录 `skipped`，而不是同时写同一份状态。旧 EOD guard 继续管理旧账户；allocator 使用自己的 horizon-aware monitor，不能被旧“每日收盘全部平仓”规则误伤。News-drift 只占用自己的 SQLite 资源，不能阻塞或写入任一账户。

## 4. 目录职责

### 根目录

- `README.md`：安装、命令、安全边界和简明运行说明。
- `SKILL.md`：Codex 操作本项目时必须遵守的工作流和安全不变量。
- `PROJECT_ARCHITECTURE.md`：本文件，描述当前真实架构和端到端流程。
- `DEVELOPMENT_LOG.md`：持续追加的开发和运行检修记录。
- `.env.example`：环境变量名称示例，不保存密钥。
- `.env.local`：本机密钥文件，必须由 `.gitignore` 排除并留在版本控制之外，不能提交。
- `requirements.txt`：PyYAML、pytest、APScheduler、jsonschema、pandas、exchange-calendars、requests、pydantic、MCP 等运行依赖。

### `config/`

- `paper_mode.yaml`：模式、初始现金、交易时段、行情 stale 时间和 EOD 退出窗口。
- `equity_universe.yaml`：股票观察池、独立期权观察池、ETF 标识和排除项。
- `strategy_profiles.yaml`：active、baseline、catalyst、旧 AI-gated 和新 allocator 策略参数，并显式记录旧策略 `new_entries_enabled: false`。
- `paper_risk_limits.yaml`：股票 25% notional、allocator 计划止损 1% NAV、交易次数、止损止盈和禁止加仓等规则。
- `options_universe.yaml`：允许的期权类型、21 至 45 DTE、delta、volume、open interest 和 spread 条件。
- `options_risk_limits.yaml`：long-premium、3% 单笔 premium、8% aggregate、最多 3 个总仓位和退出规则。
- `shared_risk_limits.yaml`：股票与期权共享的账户部署、每日 3 次 entry、3 个总仓位和同 underlying 互斥规则。
- `execution_costs.yaml`、`options_execution_costs.yaml`：commission、bid/ask、slippage 和订单有效期。
- `llm.yaml`：provider、模型、thinking policy、超时、重试和各 Agent 调用预算。这里只引用环境变量名，不存 API key。
- `integrations.yaml`：Robinhood、Alpaca、Exa、Vibe、forward runtime 和 replay 参数。
- `evaluation.yaml`：forward session、closed trade、profit factor、drawdown 和 rule violation 的最低晋级条件。

### `scripts/`

- `adapters/`：外部系统边界。负责 Robinhood/Alpaca 行情、Robinhood 期权数据、Exa 证据、Vibe OHLCV 和 Vibe sidecar。
- `agents/`：deterministic investment team、API multi-agent team、catalyst team、旧 AI-gated team 和 signed-return allocator team。
- `news_drift/`：独立 SQLite event ledger、事件关系、shadow proposal 和新闻优先 pipeline。
- `broker/`：Robinhood OAuth MCP capability audit 和严格只读 client。
- `core/`：配置、数据模型、审计、时间戳和公共基础设施。
- `dashboard/`：只读 HTTP dashboard，读取 state/logs 并转换为初学者可理解的摘要。
- `discovery/`：候选发现、event/ticker cooldown、证据 snapshot、旧 AI-gated pipeline 和新 instrument allocator pipeline。
- `evaluation/`：绩效、Agent eval、strategy comparison、outcome label、报告和 API pilot。
- `exit/`：股票退出规则和 restart-safe position mandate。
- `journal/`：订单、成交、持仓生命周期、每日复盘和交易 journal。
- `llm/`：provider 抽象、strict schemas、prompt、usage 和成本记录。
- `options/`：期权模型、fill model、paper broker、risk gate、Greeks、跨到期日合约候选、IV/时间/价格情景重新定价和退出。
- `orchestrator/`：one-shot、continuous service、dry run 和 shadow cycle 入口。
- `replay/`：virtual clock、历史 event stream、CSV replay 和 Vibe point-in-time replay。
- `research/`：snapshot 构建、技术研究和候选输入。
- `risk/`：股票 pre-trade 风控、仓位计算和股票/期权共享账户风控。
- `runtime/`：scheduler wrapper、heartbeat、watchdog、process lock、healthcheck 和 subprocess deadline。
- `simulation/`：股票 paper broker、订单状态机、fill model、virtual account 和原子状态持久化。
- `strategies/`：确定性股票/期权方向策略及 allocator conditional-plan state。

### 数据和测试目录

- `schemas/`：market snapshot、agent input/output、order、position、journal 等 JSON Schema。
- `tests/`：风控、成交、重启恢复、无前视、LLM schema、dashboard、options 和集成测试。
- `fixtures/`、`tests/agent_evals/fixtures/`：固定无网络测试数据和 Agent 评估 snapshot。
- `state/`：虚拟账户、订单、持仓、计数器、权重、replay run 和运行锁。它是运行态，不应提交。
- `logs/`：append-only JSONL 审计、决策、订单、成交、模型 usage、runtime jobs 和 journal。它是运行态，不应提交。
- `output/`：生成的报告和临时输出，不是策略源代码。
- `third_party/`：外部项目复用说明和许可证，不把不明来源代码散落到业务目录。
- `graphify-out/`：代码关系图。修改代码后运行 `graphify update .`。

## 5. 时间模型和无前视原则

每个决策输入必须同时保存：

- `snapshot_id`；
- `decision_time`；
- `data_cutoff_time`；
- 行情自己的 `asof`；
- 新闻的 `published_at`、`event_time`、`first_seen_at` 和检索时间；
- 数据源及 provider metadata。

forward cycle 的初始时钟只用于确认交易时段。网络请求结束后，系统会用新的当前时间作为真实决策时钟，防止刚返回的合法行情因为比周期起点晚几秒而被误判为“未来数据”。显式传入 replay 时间时不会这样推进，确保历史测试可重复。

缺失报价、过期报价、异常未来时间戳、ask 小于 bid、异常价差、未完成 OHLCV 或证据时间越过 cutoff 都必须 fail closed。Exa 的 crawl time 不能冒充新闻发布时间。历史 replay 不能读取当前网络新闻。

```mermaid
flowchart LR
    PUB["published_at"] --> FS["first_seen_at"] --> RET["retrieved_at"]
    Q["quote.asof"] --> CUT["data_cutoff_time"]
    RET --> CUT
    CUT --> DEC["decision_time"] --> ORD["order.created_at"] --> FILL["fill.filled_at"]
    FUT{"任何输入时间 > cutoff?"} -- "是" --> REJ["fail closed + audit"]
    FUT -- "否" --> DEC
```

## 6. Continuous Forward Service

用户在终端运行：

```powershell
.\.venv\Scripts\python.exe -m scripts.orchestrator.forward_paper_service
```

这是当前连续运行的正式入口。它会获取 `state/forward_service.lock`，避免重复启动。第二个进程会明确报出 `forward paper service is already running`。正常停止方式是在启动它的终端按 `Ctrl+C`，supervisor 会关闭 scheduler、终止仍在运行的 worker、写 shutdown 日志并释放 lock。

默认调度为：

- 主 forward cycle：每 300 秒；
- 旧 AI-gated：停止 entry，保留每 300 秒 monitor，并按原有界周期继续生成不可执行的 shadow 决策；
- 新 allocator：20:00 慢速完整研究、08:00 仅对 active plan 做增量 evidence update、09:25 仅对 active plan 做 News/Challenge invalidation、09:32 无 LLM 的 fresh-quote execution、正常时段每 3600 秒 fast research；
- 新 allocator position monitor：每 300 秒；
- catalyst discovery：每 3600 秒，并与 AI cycle 使用错开的启动偏移；
- news-drift worker：每 60 秒解析到期标签；全市场 Exa discovery 独立限流为每 15 分钟最多一次；
- AI sleeve position monitor：每 300 秒；
- EOD guard：每 60 秒；
- performance evaluation：每 1800 秒；
- supervisor heartbeat：每 30 秒。

`scripts/runtime/scheduler.py` 提供了轻量 APScheduler wrapper，适合其他受控任务；当前 continuous service 为了管理多资源 worker，直接构造 APScheduler `BlockingScheduler`。这是当前实现事实，不应把 wrapper 描述成 supervisor 的唯一入口。

`scripts/runtime/healthcheck.py` 分开报告三个层次：`runtime_healthy` 表示进程、状态和 heartbeat 可信；`forward_ready` 表示核心股票 forward 数据可用；`full_forward_evaluation_ready` 表示所有已启用的股票、期权、catalyst、AI-gated 和 news-drift 线路都可用。只靠备用报价维持股票线时会显示 `operational_status=degraded`，不能再把部分运行误读成全系统正常。

## 7. 主股票 Forward Pipeline

一次 `run_once` 按以下顺序进行：

1. 用 NYSE calendar 判断当前是否为 regular session。非正常时段只记录 skip，不交易。
2. 从 `default_watchlist` 读取股票池，从 `options_watchlist` 读取期权标的池，取并集收集行情，避免两条线互相限制候选。
3. 通过 Vibe 获取 point-in-time 日线历史，用 Robinhood MCP 获取 bid/ask。Robinhood 失败时可使用配置好的 Alpaca IEX fallback。
4. 获取 session volume，并建立包含 SPY benchmark、历史收益、成交量、价差、事件时间和持仓状态的 snapshot。
5. 先处理上周期 open orders、成熟的 outcome label、股票退出、期权 open orders 和期权退出。
6. 临近收盘 10 分钟时进入 exit-only，不再创建新 entry。
7. 对股票池同时运行 `relative_strength_v1` shadow baseline 和 `weighted_relative_strength_v2` shadow score。
8. weighted v2 把 relative strength、1 日动量、5 日动量、成交量确认、市场 regime 和 chase quality 作为软特征加权。行情有效性、时段、极端追高、已有仓位和二元事件仍是硬 gate。
9. 按分数排序，只取每周期有界的最高候选。
10. 每个候选注册 point-in-time outcome observation；默认目标为 360 分钟后的 bid 减不利滑点，与此前约 6 小时的实际持仓周期对齐。
11. `execution=shadow_only` 时明确跳过 `_submit_weighted_entry`，因此没有股票 entry order、fill 或资金占用。
12. 最多对一个已筛选候选运行 baseline-gated multi-agent shadow；它同样不能创建订单。
13. 写入 decision、candidate observation、成熟标签、portfolio snapshot、usage 和 heartbeat。

adaptive weight 当前关闭，避免继续用此前高度相关的 60 分钟标签改变执行分数。360 分钟标签只有目标时刻仍位于正常交易时段时才会注册，并且行情到达不能晚于目标 15 分钟。新标签达到独立样本要求并在净成本后显示正向 out-of-sample 结果之前，股票线不能恢复 `paper_broker`。

```mermaid
flowchart TD
    Q["新鲜股票 quote + 历史 bars"] --> G{"数据硬门通过?"}
    G -- "否" --> N["no candidate"]
    G -- "是" --> B["baseline v1 score"]
    G -- "是" --> W["weighted v2 score"]
    W --> T["保存 snapshot 和 360m target"]
    T --> X{"未来 bid 在允许延迟内到达?"}
    X -- "否" --> E["expired label + audit"]
    X -- "是" --> P["按 ask/bid/slippage 计算净收益标签"]
    P --> R["shadow report；不创建股票订单"]
```

## 8. 主期权 Pipeline

期权线只允许：

- buy-to-open long call；
- buy-to-open long put；
- sell-to-close；
- 一次最多一张合约；
- fully paid premium，不使用 margin。

它明确拒绝 short option、sell-to-open、spread、0DTE、加仓摊低、exercise 和 assignment。

处理步骤为：

1. 只对独立 `options_watchlist` 构建 snapshot。该列表优先选择一张标准 100 股合约仍可能落入小账户预算的普通股票和 ETF。
2. `long_directional_options_v2_weighted` 仍分别计算 bullish 和 bearish score 供审计，但 `new_entries_enabled=false`；已有合约继续由原 exit logic 管理。新期权 entry 只能来自 allocator 的独立 `$10,000` sleeve。
3. 读取 earnings calendar。临近二元事件且不符合策略规则时 fail closed。
4. 从 Robinhood 只读 option chain 中按 21 至 45 DTE、绝对 delta 0.30 至 0.65、volume、open interest、IV、Greeks 和最多 2% bid/ask spread 过滤；1.5% 以下为 preferred。
5. 使用账户净值 3% 的单笔 premium risk、8% aggregate option premium 和账户总 60% 部署上限过滤一张合约成本。
6. allocator 会保留最多三个到期日的有界候选。实际比较使用 underlying move、剩余时间与 IV contraction/unchanged/expansion 的重新定价；Delta/Gamma/Theta/Vega 只用于 sensitivity 和解释。
7. option limit buy 使用 ask 加不利滑点；limit sell 使用 bid 减不利滑点。限价不可达到时订单保持 open，不能用 midpoint 假设成交。
8. monitor 处理 stop loss、take profit、position mandate、临近到期和 broker sellout。是否跨夜由 `intraday_close`、`next_close` 或 `two_to_five_days` mandate 决定。

当前没有模拟行权、指派、实物交割、组合保证金或 multi-leg spread。为了不让这些缺失变成隐含风险，持仓必须在 expiry/sellout 前强制平仓。当前也没有可用于策略晋级的完整历史期权 replay，期权有效性主要依赖 forward paper 数据。

```mermaid
flowchart TD
    S["股票 snapshot"] --> D{"bullish score 或 bearish score 达标?"}
    D -- "都不达标" --> NT["no_trade"]
    D -- "bullish" --> C["long call"]
    D -- "bearish" --> P["long put"]
    C --> CH["Robinhood option chain"]
    P --> CH
    CH --> F{"DTE / delta / volume / OI / Greeks / IV / spread <= 2%"}
    F -- "否" --> DG["保存逐项 rejection diagnostics"]
    F -- "是" --> B{"一张合约 premium <= 3% 且 aggregate <= 8%?"}
    B -- "否" --> DG
    B -- "是" --> O["buy-to-open local paper order"]
    O --> M["5 分钟 monitor"] --> E["止损 / 止盈 / 时间 / 收盘前退出"]
```

## 9. LLM 和 Multi-Agent Pipeline

`scripts/llm/base_provider.py` 定义统一 provider contract。业务 Agent 不直接依赖 DeepSeek SDK。`api_provider.py` 使用 OpenAI-compatible API，当前默认模型是 `deepseek-v4-flash`；`mock_provider.py` 提供确定性测试，`local_provider.py` 只保留未来本地模型接口。

API key 只能从环境变量读取。每次调用记录 model、prompt version、输入/输出 token、latency、estimated cost、错误和 retry count。strict JSON Schema 校验失败时不能把自由文本当作交易结论。

角色划分为：

- Regime Agent：确定性 Python，不调用 LLM；
- Technical/Relative Strength Agent：确定性 Python，不调用 LLM；
- News/Bull Agent：读取已有 snapshot 和 Exa evidence，输出结构化事件；
- Challenge Agent：寻找反例、证据矛盾、陈旧信息、追高和 event risk，可以建议 veto；
- Decision Manager：只能输出 `buy`、`hold`、`exit` 或 `no_trade` 及结构化条件；
- Deterministic Risk Gate：最终 veto，永远位于 LLM 之后。

News、candidate ranking 和 Challenge 使用非 thinking 严格结构化输出。只有各 pipeline 的最终 Decision Manager 使用 thinking；若 thinking 因输出长度无法提供结构化 JSON，retry 会关闭 thinking 并要求简洁 schema 输出。这样保留最终综合能力，同时避免 Challenge 阶段此前频繁的长输出重试。模型永远拿不到 broker client，也不能修改 YAML。

## 10. Catalyst Shadow Pipeline

`exa_deepseek_catalyst_v1` 不依赖股票 active strategy 先产生 buy。它组合 core watchlist、earnings、已有 Robinhood saved scans 和 bounded Exa market query，形成最多 30 个候选，再低成本排名，最多对 3 个候选做深度研究。

Exa 负责外部非结构化证据，不替代报价、historicals、fundamentals、earnings、liquidity 或 tradability。证据按 URL、event fingerprint 和 content hash 去重，并写入不可变 timestamped snapshot。同一 ticker 两小时内没有新事件时不重复完整研究，同一事件默认冷却 24 小时。

这条线目前永远是 shadow-only。它会形成 equity 或 long-option proposal 并运行 deterministic risk veto，但 `paper_orders_created` 必须为 0。当前使用 Exa Search 和 inline highlights；没有使用 Exa Agent、Monitors 或单独的 Contents pipeline，因为调度、状态和 Agent 综合已由本项目负责。

## 11. 旧 AI-Gated Sleeve 的退出管理

`ai_gated_technical_v1` 的历史 `$2,000` sleeve 不迁移、不重置。它现在 `new_entries_enabled=false`：每个 cycle 先运行 monitor 和 open-order reconciliation，再继续执行原 discovery、Exa 和 DeepSeek 路径作为 shadow comparison。可执行信号不会发布；actionable 决策在进入 quote、risk 或 broker 路径前固定记录为 `shadow_only`，paper order 数必须为 0。已有股票和 long option 继续使用原 paper broker 和 exit logic，直到完全清仓。

1. 从 read-only watchlist、scanner、earnings 和市场数据形成候选。
2. Python 同时计算 bullish 和 bearish 技术分数，选择前 5 至 8 个有界候选，并为已确认的财报 surprise 保留少量位置。
3. Exa 对候选做有限并行搜索，DeepSeek 先做一次低成本结构化排序。
4. 对最多三个深度候选补充 primary-source evidence；如果排名结果中有足够 bearish 候选，至少两个位置保留给 bearish 方向，再用整体最高分填满其余位置。
5. 运行 News/Bull、Challenge 和 Decision。Challenge 可以 veto，Decision 可以 no-trade。
6. trade action 必须同时给出 `entry_now=true`、`max_entry_price`、可选 `min_entry_price` 和最多五分钟有效的 `entry_valid_until`。模型写在自然语言里的“等待回调”或“缺口不超过某值”不能绕过这些字段。
7. Python 刷新 underlying quote，逐项检查有效期和 ask 是否位于模型价格边界；不满足就记录 no-trade，不创建 pending 条件单。
8. 当天因 stop loss 退出的 ticker 会在发现阶段和执行阶段同时被阻止重新入场，避免同一事件反复研究和止损重买。
9. 股票 proposal 进入 sleeve 的股票 risk gate；期权 proposal 使用带拒绝诊断的 contract selection，再进入 option/shared risk gate。
10. 只有所有检查通过才写本地 paper order，之后由独立 monitor 管理退出。

entry-frozen 后仍运行上述研究和决策步骤用于对照评估，但第 7-10 步的可执行路径被 `shadow_only` gate 取代，不刷新执行报价、不调用 risk/order path，也不创建新订单。旧日志、订单、成交和 PnL 不会被转换成新 allocator 记录。

```mermaid
flowchart TD
    U["Robinhood scanner/watchlist/earnings"] --> PY["Python bullish + bearish pre-score"]
    PY --> TOP["5-8 个候选"]
    TOP --> EXA["Exa 48h evidence + 去重/cooldown"]
    EXA --> RK["Flash non-thinking ranker"]
    RK --> DR["最多 3 个深研；bearish 最少 2 个可用名额"]
    DR --> BN["Bull/News non-thinking"] --> CH["Challenge non-thinking"]
    CH --> VT{"Challenge veto?"}
    VT -- "是" --> NT["no_trade"]
    VT -- "否" --> DM["Decision Manager thinking"]
    DM --> EC{"entry_now + price bounds + <=5m expiry?"}
    EC -- "否" --> NT
    EC -- "是" --> RQ["刷新 underlying quote"]
    RQ --> PM{"ask 位于模型边界且 ticker 今日未止损?"}
    PM -- "否" --> NT
    PM -- "是" --> IN{"equity / call / put"}
    IN --> RG["deterministic account + instrument risk"]
    RG -- "拒绝" --> NT
    RG -- "通过" --> PB["独立 AI sleeve paper order"]
```

## 11.1 AI Instrument Allocator V1

`ai_instrument_allocator_v1` 是当前唯一允许新增 AI paper entry 的策略，使用 `state/strategy_sleeves/ai_instrument_allocator_v1/` 下独立 `$10,000` 账户。完整正式规格见 `docs/superpowers/specs/2026-08-19-ai-instrument-allocator-v1-design.md`。

1. Robinhood 只读 scans/watchlist/earnings 和结构化行情产生候选，Python 用双向技术分数保留前 8 名；模型不能扩大 ticker universe。
2. Exa 在 48 小时窗口内补充公司、SEC、监管和可靠新闻证据，并按 URL、event fingerprint 和 content hash 去重。
3. DeepSeek Flash 先低成本排名，再对最多 3 个候选运行 News、Challenge 和 Decision。只有 overnight 的 Challenge/Decision 可用 thinking；fast stage 不用 thinking。
4. Decision 只输出一个 horizon 和 7 个互斥 signed-return buckets，总和必须在 `1e-6` 内等于 1。Python 派生 bullish/bearish/neutral mass 与保守 move；原始概率标记 `uncalibrated`，校准前不计算或显示 probability EV。
5. bullish 时比较 long equity 与 long call；bearish 时只比较 long put；neutral 或方向优势不足时 `no_trade`。
6. 期权不是用 Delta/Gamma/Theta 局部展开估计多日收益，而是对 underlying move、剩余时间和 IV 情景重新定价。Vega 和其他 Greeks 只作为 sensitivity 诊断。
7. 股票 25% 是 notional cap，同时 planned stop loss 不得超过 NAV 1%。期权单笔 premium 不得超过 3%、aggregate 不得超过 8%。这里的 entry NAV 是现金加所有既有股票和 long option 按最新可执行 bid 计价的保守净值；任一持仓报价缺失、过期、来自未来、异常或 identity 不匹配时，新增和重试 entry 都 fail closed。股票与期权合计最多 3 个仓位、每日 3 次 entry，同 underlying 只能有一个 executable exposure。
8. `$2,000` counterfactual 只检查 allocator 已选中的完全相同 instrument 的可负担数量、风险比例和拒绝原因，不能重新选择 ticker、strike 或 expiration。
9. `short_equity_counterfactual` 只是假设直接做空 underlying 的 shadow benchmark；它没有账户、没有订单，PnL 不与 long put 合并。
10. 每个订单先注册 restart-safe mandate。新建 V2 mandate 会冻结 `max_holding_trading_days` 和股票 `planned_stop_price`。`intraday_close` 当日退出，`next_close` 下一交易日退出，`two_to_five_days` 只持有指定 2-5 个交易日；每次恢复和监控都会验证 `planned_exit_at` 位于 horizon 对应的 XNYS 正常 session、session 距离等于冻结天数且 `thesis_valid_until >= planned_exit_at`。重启会在普通 open-order 处理前取消遗留 `created` entry；只有 order id、strategy、exposure id、ticker 和股票/期权类型均匹配有效 pending/open mandate 的 retryable entry 才能继续。实际持仓的 mandate identity 不匹配会结构化 fail closed 退出，不会进入字段转换异常。allocator 以计划时间作为最长持仓期限，以持久化价格作为股票权威止损，不会因后续 risk config 改变而重算；旧 V1 mandate 不迁移，只在 horizon 固有范围内兼容读取。旧策略仍使用原百分比止损和自然日 time stop。止盈、期权 DTE/到期/sellout、确定性失效和收盘强平继续生效；缺失、矛盾、损坏、到期或已触发失效的 mandate 会 fail closed 退出。

```mermaid
flowchart TD
    R["Robinhood 只读候选与技术分"] --> T["Top 8"]
    T --> X["Exa 不可变证据 snapshot"] --> K["DeepSeek rank"]
    K --> N["News"] --> C["Challenge"] --> D["Decision signed buckets"]
    D --> V{"Python schema / sum=1 / veto"}
    V -- "失败" --> NT["no_trade"]
    V -- "通过" --> Q["fresh stock + option quotes"]
    Q --> S["underlying/time/IV scenario repricing"]
    S --> I{"equity / call / put clears executable hurdle?"}
    I -- "否" --> NT
    I -- "是" --> G["deterministic shared risk"]
    G -- "拒绝" --> NT
    G -- "通过" --> P["$10,000 namespaced paper broker"]
    P --> M["horizon mandate + monitor + exit"]
    I --> A["$2,000 same-instrument affordability"]
    V --> B["short_equity_counterfactual shadow-only"]
```

两速时钟：20:00 ET 生成慢速 conditional plans；08:00 和 09:25 只更新/失效计划；09:32 ET 不调用 LLM，只用 active plan 和 fresh quote 重建执行经济性；正常交易时段以有界间隔运行 fast research。09:25 无论是否出现新证据，都必须成功写入当日 `preopen_revalidated_at` 执行许可；任务缺席或状态写入失败时，09:32 必须拒绝旧计划。夜间和盘前模型的 `entry_now=false` 只禁止研究阶段下单，不会取消已保存计划；只有带合法非正常时段来源且通过当日盘前复核的计划能在 09:32-09:37 ET 重验，窗口外调用和 intraday plan 均拒绝。相同 ticker 的更新分析会 supersede 旧计划，no-trade/fail-closed 会 invalidate 旧计划。成功 rank 后全部候选事件都进入 cooldown，不只 top-3 deep analysis。每次真正执行授权最多有效 300 秒。

mandate 中的 `invalidation_condition` 是研究与审计自由文本，V1 不会用周期性 LLM 自动判断它。只有确定性规则、明确人工动作或 replay 事件设置 `invalidation_triggered` 后，monitor 才以 thesis invalidation 退出；Dashboard 也会明确区分“仅记录条件”和真正已触发状态。

`entry_condition` 同样只是 V1 研究与审计文本，不能独立授权订单。模型若仍在等待未来价格、突破或确认，必须输出 `no_trade`；实际 entry 只能由 Python 的当前报价、remaining move、流动性、授权时限和风险 gate 放行。

概率校准按 horizon 完全分开。expanding walk-forward 的每个训练 fold 只能使用在该 test decision time 前已经成熟的标签；主要比较 out-of-sample Brier score 与 log loss，ECE 和 reliability curve 仅作诊断。每条记录保存 calibration version、training cutoff、sample size 和 horizon。

## 11.2 News-First LLM Drift Shadow Pipeline

`llm_news_drift_v1` 与主股票、期权、catalyst 和 AI-gated 都隔离。它不先读取固定 watchlist，也不要求技术策略先产生 buy candidate。worker 每分钟解析标签，但全市场 Exa query 每 15 分钟最多运行一次；结果保存为不可变 raw snapshot，并按 URL、content hash 和 event fingerprint 去重。同一事件在 24 小时 cooldown 内不会反复发送给模型。

唯一的 LLM 阶段是 `NewsDriftHeadlineAgent`。输入只有 headline、published time、source、source tier、可选 ticker/company hint 和最近事件标题；没有 price、quote、volume、technical、position 或 account。strict output 同时完成 ticker mapping、方向、事件类型、materiality、novelty、ambiguity、confidence 和 event relation。`duplicate` 不再产生 signal；`material_update` 和 `contradiction` 可以重新评估。

完成模型调用后，Python 才验证 exact US-listed instrument，并从 Robinhood 读取 fundamentals、historicals 和 bid/ask。stale/future quote、低流动性、小市值、宽价差、signal latency、缺少 pre-event reference 或 initial reaction 过大都会 deterministic reject。只有日期而没有时分的发布时间会保留原值，但使用 `first_seen_at` 作为保守可交易时间；盘前只因 stale quote 被拒的信号会在开盘后用新行情重验，不重复调用 LLM。第一阶段只有正面事件能够形成 long-equity shadow proposal；负面事件只保存，留给独立的 `short_equity_counterfactual` 或 long-put 研究。

proposal 的参考本金为 `$2,000`，单笔最多 25%，entry 使用 ask 加不利滑点。它只写 `state/news_events.sqlite`、`logs/news_drift_*` 和 `logs/news_drift_snapshots/`，没有 paper broker、orders 或 positions。收益标签按 +1m、+5m、+15m、same-day close、next close 和 second close 分开，退出按 bid 减不利滑点。

评估分别报告 event、firm-day 和 portfolio-day 的 gross/net return、hit rate、profit factor、observed cost、break-even cost 和成本敏感性。至少 100 个有效标签及 20 个 portfolio day 之前只能显示 `insufficient_forward_evidence`；配置为 shadow-only 时永远不能自动晋级。完整政策、Exa 功能取舍、论文复现边界和 P2 隔离实验见 `references/llm_news_drift_policy.md`。

## 12. 订单、成交和账户记账

股票和期权订单都支持以下生命周期：

```text
created
submitted_to_paper_broker
open
partially_filled
filled
cancelled
expired
rejected
```

第一版通常不会主动产生部分成交，但模型和持久化结构支持 `partially_filled`。`created` 绝不等于持仓。只有 fill 被原子应用到账户和 positions 后，系统才增加持仓和交易计数。

```mermaid
stateDiagram-v2
    [*] --> created
    created --> submitted_to_paper_broker
    submitted_to_paper_broker --> filled: 可立即按不利价格成交
    submitted_to_paper_broker --> open: 限价尚不可达
    submitted_to_paper_broker --> rejected: deterministic risk 拒绝
    open --> partially_filled: 数据结构支持
    open --> filled: 新报价达到限价
    open --> cancelled
    open --> expired
    partially_filled --> filled
    partially_filled --> cancelled
    filled --> [*]
    rejected --> [*]
    cancelled --> [*]
    expired --> [*]
```

股票买入成交价基于 ask 加不利滑点；卖出基于 bid 减不利滑点。期权使用真实合约 bid/ask 和单独配置的不利滑点。limit 不可达到时订单保持 open，之后由新报价重试、过期或取消。

账户、positions、orders 和 counters 使用原子文件替换保存。JSONL 审计使用跨进程锁和 durable append。idempotency key、duplicate order gate、已有持仓 gate、禁止 average down 和 AI 同日 stop-loss ticker block 共同阻止重复下单。

## 13. Monitor、Exit 和 EOD

股票和期权各自有退出规则。退出判断仍需要新鲜 bid/ask；缺失价格时不能假设以 last 或 midpoint 平仓。

主 forward cycle 每五分钟先监控持仓再寻找新 entry。AI sleeve 有独立的五分钟 monitor，所以即使一小时 discovery 尚未运行，它的现有持仓仍会管理。EOD guard 每分钟检查两个账户，在临近收盘时处理退出，并在重启后修复跨夜残留状态。

当前设计目标是日内或短持有期 paper evaluation，不允许策略依赖未实现的隔夜期权交割行为。

## 14. Journal、Metrics 和 Dashboard

每个决策和订单会留下当时看见的数据时间、策略名、thesis、支持证据、反方证据、风险结论、成交信息和退出原因。主要日志为 append-only JSONL，便于按时间重建事件。

`calculate_metrics.py` 分别统计股票、期权和聚合结果，包括：

- net return 和 realized PnL；
- closed trade 数和 win rate；
- profit factor；
- max drawdown；
- filled/open/expired/rejected order 数；
- executable order 的 fill rate 和 unfilled rate；
- rule violation；
- LLM latency、token 和 estimated cost；
- baseline、AI sleeve 和 shadow decision comparison。

AI sleeve 额外按方向拆分 `bullish` 与 `bearish`：decision count、trade proposal、filled entry、fill rate、拒绝原因、closed trade、win rate、净 P/L，以及成交记录中的 slippage/commission 模型化成本。股票和 long call 归入 bullish，long put 归入 bearish；两个方向不能再用合并结果掩盖差异。

deterministic risk rejection 不再被计入“未成交率”的分母，因为它从未进入市场执行生命周期；它仍作为独立 rejected count 和风险诊断保留。

dashboard 是只读视图。它不启动服务、不修改策略、不下单，只从 `state/` 和 `logs/` 生成初学者摘要。服务和 dashboard 应在两个终端分别启动；dashboard 终端中的 `Ctrl+C` 只停止页面服务，不会停止 forward service。

dashboard 使用五个相互隔离的客户端视图：

1. `总览`：先显示 service、market session 和 freshness，再并列显示旧 `$2,000` 账本与 `$10,000 ai_instrument_allocator_v1` sleeve。两套账户的 cash、positions、orders 和 PnL 不合并。
2. `持仓与订单`：分别收集主账户、旧 AI sleeve 和 allocator sleeve 的 equity/option position 与 order。只有 `filled`、`cancelled`、`expired` 和 `rejected` 视为已结束；`created`、`submitted_to_paper_broker`、`open`、`partially_filled` 以及未知状态都保留为未完成订单，不能显示为持仓。已结束订单默认折叠。
3. `策略表现`：按行显示策略的 execution mode、账户归属、决策、入场、平仓、PnL、胜率和最近结论。`shadow_only`、`只管理旧仓`、`影子研究 / 管理旧仓` 和 `模拟交易` 不混淆。
4. `AI 决策`：展示候选、Exa 证据、DeepSeek 结构化结果、Challenge 和 deterministic Python risk veto。长证据默认折叠，`reasoning_content` 和 API key 不进入 dashboard state 或页面。
5. `系统健康`：当前 heartbeat、scheduler、market data、paper boundary、Exa、DeepSeek 和 audit 状态优先；最近交易日累计错误另列为历史事件，避免把历史 390 次失败误读成当前仍有 390 个故障。

页面使用 URL hash 保存当前 tab，支持左右方向键、Home/End 和 ARIA tab semantics。浏览器每 15 秒刷新，在 `document.hidden=true` 时暂停；服务端 `_read_jsonl` 从文件尾部按块读取最后 N 条有效记录，不再为每次页面刷新整文件加载几十 MB 的 audit/decision/runtime log。HTTP handler 只实现 `GET`、`HEAD` 和 `OPTIONS`，且 dashboard 模块不导入 broker adapter。

安全带不是静态声明：runtime 启动与 healthcheck 都调用 `assert_paper_mode`，只有 `paper=true`、`live_readonly=false`、`live_trading=false` 才能继续；API 返回配置中的真实三态值，dashboard 也只在 `true / false / false` 时显示纯模拟状态。顶部把 forward heartbeat、heartbeat 中的市场状态和最新股票报价分开；健康页再分别显示股票与期权报价观察时间。损坏、未来或过期 heartbeat 以及未来报价均按 stale fail-closed 展示，旧 market session 不能继续显示成当前正常交易。没有最近作业状态时 scheduler 保持中性，不得显示绿色正常。

订单 payload 会忽略损坏的 null 记录、保留并显示所有未完成订单，并在每个账户/工具组中独立选择最近完成记录；未知订单状态按未完成告警，不会被误计为历史完成。页面显示最近 20 笔和真实完成总数。主账户、旧 AI sleeve 和 allocator sleeve 的 metrics 使用状态文件与日志签名缓存，任何相关文件变化都会失效重算；News Drift 的签名覆盖 SQLite、WAL、SHM、`llm_usage.jsonl` 和 `news_drift_cycles.jsonl`，旧 AI directional metrics 还覆盖 `ai_gated_decisions.jsonl`。News Drift 数据库或 JSON 指标异常被限制在该组件内，返回 `metrics_available=false`，不会中断整个只读页面。

日报 session 以 heartbeat 中最近一次 forward exchange session 为优先来源，`daily_counters.date` 只作回退，避免无新入场时把当天作业、模型调用和异常计数归到前一日。策略页的 sleeve 累计 PnL 使用当前净值减初始资金；News Drift 的收益标签与真实平仓分开，达到最小样本数但尚未盈利也不会显示成正面结论。

## 15. Historical Replay 和 Forward Evaluation

项目有两类历史路径：

- `scripts/replay/replay_run_manager.py`：基于 CSV event stream、virtual clock、原始 deterministic investment team 和 paper broker 的基础 replay。
- `scripts/replay/vibe_replay_run_manager.py`：基于 Vibe 5 分钟 OHLCV，合成不利 top-of-book，并复用股票 broker、risk、fill、exit 和 journal。

当前 Vibe replay 的 entry strategy 仍是 `relative_strength_v1`，不是 forward 中的 `weighted_relative_strength_v2` 候选口径，也不包含完整期权 replay。因此 replay 与 forward 尚未使用完全相同的策略组合。历史结果只能发现明显错误，不能替代 forward paper evidence。

最终晋级判断以 `config/evaluation.yaml` 为准，默认至少需要：

- 20 个 forward session；
- 30 笔已平仓交易；
- 正 net return；
- profit factor 至少 1.2；
- 最大回撤不超过 10%；
- 0 个 risk rule violation。

达到这些数字也只表示“值得继续验证”，不表示未来盈利得到保证。

## 16. 外部组件边界

- Robinhood MCP：项目自有 OAuth client，只允许显式只读方法。完整 capability manifest 也包含交易工具，但 generic call 不对业务代码开放，写工具不在 allowlist。
- Alpaca：当前作为 Robinhood equity quote 的备用来源，默认 IEX feed，不应描述成完整 SIP。
- Exa：用于 recent external evidence、market/ticker search 和 highlights，不作为价格源。
- Exa Search 的 inline Contents highlights 已使用；Deep Search、Exa Agent、Monitors 和独立 Contents endpoint 尚未接入，避免把额外延迟放进一分钟关键路径。
- DeepSeek：通过 provider-neutral OpenAI-compatible HTTP API 使用，主要用于 News、Challenge、Decision 和 bounded ranking。
- Vibe-Trading：固定 commit，通过隔离 subprocess adapter 提供 OHLCV、独立 backtest 和可选 read-only research sidecar；没有把整个上游源码复制进业务目录。
- APScheduler：负责时间调度；heartbeat、process lock、resource conflict、hard timeout、state recovery 和 fail-closed 由项目代码负责。
- LangGraph：当前未引入。确定性 pipeline 尚不需要复杂 graph checkpoint、human approval 或长期条件图。

## 16.1 Hawkes Process 研究结论

Hawkes Process 是带自激强度的点过程，适合研究“某类事件发生后，短时间内同类或交叉事件到达率是否上升”。金融文献最常见的输入是逐笔成交、买卖方向、订单提交/取消和 order-book 状态，而不是稀疏新闻标题。参考：[Hawkes processes in finance](https://arxiv.org/abs/1502.04592)、[state-dependent Hawkes order flow](https://arxiv.org/abs/1809.08060) 和关于显著性、非平稳性的检验讨论 [Nonparametric Hawkes Processes and Financial Data](https://papers.ssrn.com/sol3/Delivery.cfm/SSRN_ID2583431_code1821037.pdf?abstractid=2450101&mirid=1)。

本次审计的新闻 ledger 只有 211 个事件、165 个 ticker，单 ticker 最多 5 个事件；Exa 又是按 15 分钟轮询发现事件。现在拟合 Hawkes，强度很可能反映 scheduler cadence、来源重复和抓取延迟，而不是可交易的信息聚集。因此项目当前没有新增 Hawkes dependency，也没有把 Hawkes score 接入候选、风险或订单路径。这是基于数据不适用的明确拒绝，不是遗漏。

完整研究边界和未来 shadow experiment contract 见 `references/hawkes_process_assessment.md`。

只有满足以下条件后才允许建立隔离 shadow experiment：

1. 获得 point-in-time 的逐笔成交或 order-book event stream，并保存交易方向、事件类型和交易所时间；
2. 每个资产每天至少有数千个可验证事件，而不是个位数新闻；
3. 在历史 replay 中完成稳定性、残差、goodness-of-fit、非平稳基线和 out-of-sample 检验；
4. Hawkes 输出只作为额外 shadow feature，与不使用它的同 snapshot baseline 对照；
5. 在扣除 spread、slippage 和延迟后显示独立增益，才讨论进入 deterministic candidate score，仍不能绕过 risk gate。

```mermaid
flowchart LR
    T["未来 tick/order-book 数据"] --> V{"事件量、时间精度和许可满足?"}
    V -- "否；当前状态" --> OFF["不实现 Hawkes"]
    V -- "是" --> FIT["多变量 Hawkes 拟合"]
    FIT --> TEST["残差/稳定性/OOS 检验"]
    TEST -- "失败" --> OFF
    TEST -- "通过" --> SH["shadow feature"]
    SH --> CMP["同 snapshot 净成本对照"]
    CMP -- "有独立增益" --> REVIEW["人工评审后才可能晋级"]
```

## 17. 当前明确限制

1. 当前累计 forward 数据不足，尚无稳定盈利证据。
2. 一张标准期权合约代表 100 股，`$2,000` counterfactual 在 3% premium cap 下会让许多合约不可负担；它只能报告相同合约的拒绝原因，不能为小账户另选便宜合约。
3. 没有 short option、spread、margin、exercise、assignment、实物交割或 portfolio margin 模拟。
4. weighted v2 股票线因 forward 净成本结果为负而处于 shadow-only，也没有可支持重新晋级的完整同策略 historical replay。
5. AI-gated 已有少量成交但结果为负；可执行入场契约修复后的新样本仍为零，旧样本不能用于证明修复后策略有效。
6. catalyst 策略只有 shadow proposal，不能用它的决策结果宣称 paper PnL。
7. news-drift 刚进入 forward shadow 收集阶段，尚无足够 event、firm-day 或 portfolio-day 样本；Exa 搜索费用在未配置合同单价时仍是 unpriced。
8. 官方论文 replication package 尚未下载和独立复现；当前指标只是为该复现预留兼容聚合口径，不能称为论文复现结果。
9. saved Robinhood scans 只有用户已创建时才能产生候选，项目不会创建或修改 scanner。
10. Alpaca IEX 和部分第三方历史源不等于全市场 consolidated feed，成交模拟精度仍有限。
11. dashboard 是解释层，不是账户真相。发生冲突时，以 state、append-only logs、runtime heartbeat 和 paper broker ledger 为准。
12. 当前没有适合 Hawkes Process 的逐笔成交或 order-book event stream，新闻 ledger 也过于稀疏，因此没有 Hawkes 交易信号。

## 18. 开发和验证顺序

每次改变策略或运行代码，应按以下顺序进行：

1. 保持 `paper=true` 和 `live_trading=false`，检查改动没有新增真实 broker 写方法。
2. 用临时 root 和 mock provider 运行相关单元测试，不能污染真实 `state/`。
3. 运行全量 pytest。
4. 运行无网络 dry run，验证 research、risk、broker、fill、exit、journal 和 metrics 闭环。
5. 用 `--readiness` 和 `scripts.runtime.healthcheck` 做只读运行检查。
6. 如果 continuous service 正在运行，不从其他进程执行 state-mutating one-shot。由用户在原终端 `Ctrl+C` 后重启。
7. 更新 `DEVELOPMENT_LOG.md`，记录证据、文件、测试和是否需要重启。
8. 运行 `graphify update .`，使知识图与代码保持一致。

这个顺序的核心原则是：先证明数据和状态可信，再讨论策略收益；先保留拒绝和失败证据，再调整门槛；任何模型结论都不能替代确定性风险和 paper broker 账本。
