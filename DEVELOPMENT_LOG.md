# Development Log

## 2026-08-28 (America/Los_Angeles) - Robinhood OAuth 自动刷新与 allocator 行情恢复

- 审计连续运行约 47 小时的 point-in-time runtime records。supervisor、dashboard、heartbeat 和 process lock 均保持存活；从 `2026-08-27 09:46:44` 到 `2026-08-28 12:59:55` 本地时间，584 个 `news_drift` job 因同一 Robinhood OAuth 错误失败。主股票 forward 周期通过 Alpaca IEX fallback 继续完成，但 Robinhood-only scanner、新闻、期权和完整 AI research 处于 degraded 状态。
- 根因不是“每周必须人工认证”，而是持久化 access token 的观测寿命约为 7.72 天，而上游 MCP Python SDK `OAuthClientProvider` 重启加载 token 后没有恢复 expiry clock，401 后错误进入完整浏览器授权。DPAPI credential store 现在保存绝对 expiry、加载时计算剩余寿命，并在到期前 60 秒进入 refresh window；provider 初始化后恢复 expiry 并自动 refresh。多个 worker 通过跨进程 refresh lock 串行续期并在获得锁后重读凭据，避免 rotating refresh token 被并发覆盖。refresh response 未返回新 refresh token 时继续保留旧加密 token，避免下一次续期能力丢失。API key、token 和 client secret 均未写入日志或 Git。
- allocator 原先没有股票行情 fallback，导致 MSFT 在 Robinhood 认证失效后无法 mark 或执行计划退出。本次将配置好的 Alpaca read-only adapter 接入旧 AI-gated 和 allocator 的股票 entry revalidation、monitor、valuation 与 exit，并追加 `readonly_equity_quote_fallback_used` 审计事件。期权链和期权仓位不使用现货替代，仍 fail closed。
- 修复 09:32 allocator `open_execution` 与已停止新增仓的旧 `ai_gated_technical_v1` 同时争用 `evidence_store` 的调度冲突。open execution 不调用 LLM/证据写入，现在只声明 `allocator_account`；overnight、premarket、pre-open 和 intraday research 仍同时锁定 evidence store。
- 历史 `$2,000` 账本、独立 `$10,000` allocator 账本、订单、成交、PnL、日志和 forward snapshot 均未迁移或改写。所有 fallback 只读，审计字段继续确认 `live_order_tools_called=false`。
- 独立 final review 发现并修复两个 P2：DPAPI 中的 persisted metadata 与运行时发现 metadata 在接收 refresh token 前都重新执行 Robinhood HTTPS host allowlist；child stderr 写入 `runtime_jobs.jsonl` 前与 adapter error 共用 OAuth secret redaction。新增 protocol response、双 worker refresh、恶意 metadata、subprocess stderr、entry fallback 缺失报价和 option-data fail-closed 回归。
- 现场非交互 probe 自动续期成功：refresh token 保留，当前 token endpoint 为 `api.robinhood.com`，Robinhood `get_equity_quotes` 返回 MSFT 可用 bid/ask；随后 healthcheck 为 `operational_status=ok`、`runtime_healthy=true`、`ready_for_full_forward_evaluation=true`。服务端当前公布 67 个工具，原 50 个 baseline 全部存在；17 个新增 crypto、option exercise、SEC/news 工具保持未授权，不进入 runtime allowlist。全量 pytest 为 `430 passed`，`compileall` 与 `git diff --check` 通过；仅有 4 条既有上游 `exchange_calendars` deprecation warning。

## 2026-08-26 (America/Los_Angeles) - Allocator 独立历史验证体系

- 新增三个彼此隔离的证据层。`bullish_equity`、`bullish_call`、`bearish_put` golden fixtures 使用正式 allocator、instrument comparison、deterministic risk、股票/期权 paper broker、fill WAL、position mandate、monitor、exit 和 PnL attribution，但全部写入自动删除的临时 root；MCP `call_tool` 与只读 broker adapter 上安装独立 live-order deny/spy，且核验 concrete broker 均为 paper class，3/3 场景通过、可观察真实 broker write attempts 为 0。这只证明 functional liveness，不作为盈利证据。
- 新增只读 natural strict replay。命令强制显式 `asof`，单次读取并 hash JSONL，验证 immutable snapshot/path/reference hash，按 snapshot lineage 连接 candidate、deep research、decision、proposal、plan、allocation、observed order 和 fill；late、missing、tampered 或断链记录 fail closed，不调用 LLM/current adapter/broker，不创建或改写历史订单。
- 实测截至 `2026-08-27T06:25:50Z` 的 48 小时数据：observed 为 `40 → 33 → 15 → 16 decisions → 7 proposals → 5 allocations → 1 selected → 1 order/fill`；strict unique lineage 为 `12 → 12 → 12 decisions → 6 proposals → 0 admitted allocations/orders/fills`。窗口内 41 份 snapshot 中 16 份通过 exact decision link 取得 cutoff，25 份缺唯一可证明 cutoff，另有 7 条旧 decision 缺 record cutoff，相关后代全部排除；admitted `time_violation_count=0`。
- point-in-time 合约改为默认要求显式 `data_cutoff_time`。新版 immutable snapshot 同时记录 cutoff、retrieval time 和 physical write time；legacy snapshot 只有在 exact path/hash decision link 提供唯一 cutoff 时才可验证。quote/OHLCV/news/option 必要 observation timestamp、ticker/instrument identity 和 decision/plan/allocation/order ID 冲突均 fail closed；新 plan 显式保存 `source_decision_id`、cutoff 和 snapshot reference，有序 revalidation version 可追到 prior plan。
- 新增历史数据完整性审计和 expanding/rolling walk-forward partition contract。OHLCV 还必须声明 corporate-action-safe 与 coverage complete；完整期权链必须证明 contract count、expiration/strike coverage、source/request identity、capture time、pagination 和 query 范围。当前 snapshots 缺完整 OHLCV、PIT option chain 和成熟 outcome labels，因此不能声称 executable equity/option historical PnL；只允许 synthetic option sensitivity。历史 LLM 输出始终标为 recorded-output diagnostic/comparative evidence，并明确 `strategy_reexecution_performed=false`。readiness 将“合同要求严格分区/成熟标签”与“本次真正执行了分区/leakage checks”分开，后者在无标签时为 false。
- 新增 version manifest，保存 strategy/prompt/schema/config/source hashes、provider、model id、显式 data cutoff 和输入 dataset hashes。聚合报告分开输出 functional、historical 和 forward evidence，并禁止写入 `state/` 或 `logs/`。
- golden WAL 不再只检查 transaction 数量，同时逐笔核对 entry/exit order identity；risk trace 要求 allocator eligibility、正数 deterministic risk 和 broker risk gate 均通过。报告对 `$10,000` allocator state/log/snapshot 目录下全部文件类型做运行前后 hash 对比，而非只检查 JSON；该次 182 个文件一致，historical orders created 和 live broker writes 都为 0。version manifest 同时绑定 dirty revision marker、正式策略/验证 runner/report 源码和报告 schema。
- Dashboard 总览新增面向初学者的“三种证据不要混淆”和 walk-forward readiness；功能跑通、严格历史诊断和真实 forward paper 结果独立显示。Dashboard 只读取 schema-valid 的预计算报告，不再因 HTTP refresh 现场运行 replay。报告生成时现有 `$10,000` sleeve 为 0 个 closed trades、1 个 open position、realized PnL `$0.00`，仍为 `insufficient_forward_evidence`。

## 2026-08-26 (America/Los_Angeles) - Issue #3 allocator proposal recall

- 冻结读取 `2026-08-24T08:16:12Z` 至 `2026-08-26T08:16:12Z` 的真实 append-only 日志和 58 份不可变 snapshot。observed audit 漏斗为 `56 candidates → 40 ranking inputs → 20 deep research → 22 structured decisions（含 2 次盘前更新）→ 2 proposals → 1 allocation → 0 selected instruments → 0 orders/fills`；根因仍在 cooldown 与研究/提案转换，不在 paper broker。两份 proposal 是盘中 XPEV 与夜间 INTU；XPEV 的 signed direction 不足，INTU 在窗口结束时仍是等待次日执行门的 conditional plan。
- 58/58 份 snapshot 的 SHA-256 完整，但旧 writer 在网络采集前冻结 envelope cutoff，留下 229 个去重后的 observation-after-cutoff 记录（55 quote、58 news first-seen、58 news retrieved、58 source retrieved）。严格 replay 因此排除全部 58 份 snapshot、22 条关联 decision 和 1 条 allocation，old/new 可重放子集均为 0，不能把 observed audit 漏斗描述成 lookahead-safe 绩效回放。历史文件未改写；新版 live research/premarket snapshot 改用 post-collection cutoff，显式 replay 时间仍固定并拒绝 lookahead。
- 夜间和盘前允许 `propose_trade + entry_now=false`。Python 会把模型误给的 `entry_now=true` 收紧为 `false` 并记录 normalization，不扩大执行权限。等待 fresh quote、spread、remaining move、option chain 和 Python risk gate 明确属于 execution-time gate；只有 thesis 本身依赖未来事件、突破或事实时才 `no_trade`。
- ranking-only 候选不再写 24 小时 event cooldown。deep no-trade、watch、hard veto、active plan 和 executed trade 分别使用 120 分钟、60 分钟、24 小时、6 小时和 24 小时 cooldown；新 event fingerprint 绕过 ticker cooldown。eligibility 保存触发它的 prior transition ID，深研结果保存新的 outcome transition ID，并同时记录 stage、reason、duration 和 expiry。旧日志只能估计 11 次 rank-only cooldown 可能可避免，不能声称精确 `40 → 51`；新版 cycle 保存 candidate snapshot、ranking/deep 标记和两类 transition 关联，供以后做可验证关联。
- allocator Challenge 新增独立 strict schema。关键事实冲突、快照外/时间错误、关键证据 stale、缺必要 primary source、mandate/horizon 无效才是 hard veto；普通不确定性、partial price-in、估值、追高和次要证据缺口为 soft concern。旧无类型 veto 保持 fail-closed。
- 新增 `watch` 结果和独立 `allocator_watches.json`；watch 带明确 expiry，过期后不再出现在 active watch 结果中，也不创建 plan、allocation 或 order。Dashboard 以只读输入签名缓存 replay，分开展示 observed audit funnel 与严格可重放子集，并拆分全部 Issue 指定 blocker。replay 验证 hash，不调用模型或 broker，不生成历史订单。
- allocator、replay 与 Dashboard 定向测试为 `187 passed`；全套 pytest 为 `379 passed`，仅有 4 条既有上游 `exchange_calendars` deprecation warning。Dashboard 使用真实 48 小时输入在桌面 `1440x1000` 与移动端 `390x844` 完成 Playwright 验证，均无横向溢出，console 为 0 error / 0 warning；临时验证服务已关闭，用户原有 forward service/dashboard 未停止。

## 2026-08-21 (America/Los_Angeles) - 48 小时 0-trade 漏斗与 paper 门槛校准

- 读取 2026-08-19 至 2026-08-21 的真实 append-only 运行记录，未改写任何历史账户、订单、成交、PnL 或日志。主 forward 线完成约 3,800 份快照并产生 527 次股票加权候选；期权线完成 1,064 次方向判断，其中 898 次为 `buy_to_open`。两者没有下单是当前权限设计的结果：股票加权为 `shadow_only`，旧方向期权为 `entry_frozen`，只能继续管理旧仓。
- 当前唯一允许新增仓的 `$10,000 ai_instrument_allocator_v1` 在滚动 48 小时内查看 44 个候选，形成 21 次结构化模型决策、1 个交易提案、1 次执行尝试、0 个订单和 0 次成交。自动诊断据此把首要瓶颈定位为“AI 研究与质询到 trade proposal 的转化”，而不是行情扫描不足。
- 44 个候选结果中包含 23 次 ticker/event cooldown、11 次 Challenge 或 Decision 主动 no-trade、5 次 horizon 与持仓天数字段冲突、4 次快照外证据引用，以及 1 次旧版执行前报价适配器失败。cooldown 和证据拒绝继续保留；旧报价参数问题已在合并的 PR #2 中修复。
- `策略表现` 新增“过去 48 小时机会漏斗”，逐级显示候选、模型判断、交易提案、确定性执行、模拟订单与成交，并用中文解释每类阻塞原因。股票和期权 baseline 信号单独标明为只观察/冻结，避免把大量不可执行信号误读为 broker 或风控故障。
- 根据唯一真实 proposal 的 signed buckets，把 allocator 的 paper-only `minimum_direction_mass` 从 `0.55` 调整为 `0.50`；`minimum_direction_margin=0.15`、流动性、spread、成本、Challenge、证据 grounding 和全部确定性风险上限保持不变。该 proposal 的 bullish mass 为 `0.50`、次高 bearish mass 为 `0.30`，在新门槛下可进入后续确定性工具比较，但原始概率仍为 `uncalibrated`，不得用作概率 EV。
- DeepSeek Decision prompt 补充 horizon 与 `max_holding_trading_days` 的精确映射：`intraday_close=0`、`next_close=1`、`two_to_five_days=2..5`。Python 仍严格验证并 fail closed，不替模型静默修改输出。
- 本次没有停止或重启用户运行的 forward service/dashboard，也没有调用真实券商写工具。代码和配置需要由用户在原终端正常重启后才会加载。

## 2026-08-21 (America/Los_Angeles) - PR comment 5368197938 paper fill WAL

- 股票与期权 paper broker 新增共享 `PaperFillTransactionCoordinator`。每次成功成交先以 `fill_id` 写入 namespaced `paper_fill_transactions.json` 的 `prepared` WAL，再应用 account、positions、终态 orders、daily counters、trade lifecycle 和 append-only logs；全部完成后才标记 `committed`。
- 重启和每次订单提交前都会恢复未完成事务。恢复覆盖到已保存的目标快照，不重新执行现金、PnL 或计数器增量；重复提交已持久化终态订单直接返回原订单。成交、trade journal 和 audit JSONL 使用 `fill_transaction_id` 去重，退出 postmortem 也由同一事务补完。
- 创建、提交、取消和过期订单的状态写入使用同一跨进程 fill lock，避免与成交快照互相覆盖。WAL 位于各自 strategy sleeve state 目录，不合并或迁移历史 `$2,000` 主账本与 `$10,000 ai_instrument_allocator_v1` 账本。
- 新增 16 个 crash-injection 回归，覆盖股票和期权的开仓、退出，以及 account、positions、orders、counters 四个中断边界；每个场景验证两次重启和重复提交后现金、持仓、终态订单、交易计数与 fill log 都恰好一次。全套 pytest 为 `364 passed`，仅有 4 条既有上游 `exchange_calendars` deprecation warning。本次没有读取或修改真实券商账户，也没有调用或新增任何真实下单工具。

## 2026-08-21 (America/Los_Angeles) - PR comment 5366684988 提交后恢复与 live observation clock

- allocator 重启恢复现在会把全部已持久化 entry order 状态反向同步到关联 plan：有效 `submitted/open/partially_filled` 和 `filled` 令 plan 进入 `executed`，`rejected/expired/cancelled` 进入对应终态；这一同步发生在 active plan 查询前，因此提交、成交或 mandate reconcile 后崩溃都不会再次执行同一 plan。
- `PositionMandateStore.register_order()` 不再覆盖同 exposure 的 `pending_fill/open` mandate。完全相同的 order/mandate identity 可幂等返回原记录，任何不同 identity 都 fail closed，原 live position mandate 保持不变。
- allocator 的 live 调用保留 `now=None` 到网络采集结束，再用真实 post-fetch wall clock 验证 underlying、option candidate 和全部 holding marks；最终 execution/data cutoff 不早于任何实际使用的报价。显式传入的 replay 时间保持固定，任何晚于 cutoff 的报价都作为 lookahead 拒绝。
- marked NAV 新增持仓对象与 map key 的身份校验：股票 position symbol、期权 contract id、非空 underlying 和 call/put type 任一损坏都会阻止 entry。forward monitor wrapper 和 live EOD 调用不再提前冻结 allocator 时间。
- allocator focused 测试为 `132 passed`，其中新增定向回归 `16 passed`；全套 pytest 为 `348 passed`，仅有 4 条既有上游 `exchange_calendars` deprecation warning。本次仍为严格 paper-only 修改，没有调用 Robinhood 真实下单工具，也没有修改历史账户、订单、成交、PnL、OAuth、state 或 logs。

## 2026-08-21 (America/Los_Angeles) - PR comment 5366041274 fail-closed 恢复与 marked NAV

- `ai_instrument_allocator_v1` 启动或 monitor 时先恢复未完成 entry：`created` 永远取消，`submitted_to_paper_broker`、`open`、`partially_filled` 只有在存在与 order、strategy、ticker、instrument 和 exposure 完全匹配的 pending/open mandate 时才允许重试。取消前先 invalidated 关联 plan，随后关闭 mandate；中途再次崩溃时由下一次 reconcile 继续 fail closed。
- mandate 校验现在绑定实际持仓身份。股票要求 `equity:{ticker}`、`instrument_type=equity` 和正数有限止损；期权要求精确 option exposure id、underlying ticker 与 call/put 类型。缺失、损坏或错配统一形成结构化退出，不再因坏字段抛异常中断 monitor。
- allocator 的仓位比例、股票 notional/计划止损、期权 premium 和共享总风险上限改用同一时点的保守 liquidation NAV：现金加全部股票 bid mark 加全部期权 bid mark。任一必要报价缺失、过期、future、非有限或 identity 错配时禁止新建或重试 entry；成本基准只保留为 deployment diagnostic，不再标记为 NAV。allocation 和 portfolio snapshot 保存估值方法、计算时间及逐持仓 mark 时间。
- `entry_condition` 明确为研究与审计文本，不是订单授权表达式。V1 不解析 LLM 自然语言条件；只有 Python 的新鲜报价、remaining move、流动性、instrument economics 和 deterministic risk gates 可以创建或重试 paper order。
- allocator focused 测试为 `116 passed`，全套 pytest 为 `332 passed`，仅有 4 条既有上游 `exchange_calendars` deprecation warning。没有修改历史账本、state、logs、OAuth 或敏感配置，也没有调用 Robinhood 真实下单工具。

## 2026-08-20 (America/Los_Angeles) - PR comment 5364112198 mandate 语义与冻结止损

- 新建 position mandate 升级为 V2，并持久化精确 `max_holding_trading_days`。注册和 monitor 恢复都会用 XNYS 日历确认 `planned_exit_at` 位于 horizon 对应的正常 session、session 距离与冻结天数一致且 `thesis_valid_until >= planned_exit_at`；可解析但矛盾的记录统一 fail closed。
- 旧 V1 mandate 不迁移、不改写。`intraday_close` 和 `next_close` 仍按固有 session 精确校验，`two_to_five_days` 在 2-5 session 范围内兼容恢复；超出范围或盘前/盘后伪退出时间会安全退出。
- allocator 股票 monitor 现在使用 mandate 中的 `planned_stop_price`，不再按后来修改的 `risk.stop_loss_pct` 重算。旧策略继续使用原百分比止损，allocator 的止盈、DTE/到期/sellout、失效和强平规则保持。
- 新增/受影响 focused 测试为 `14 passed`，全套 pytest 为 `316 passed`，仅有 4 条上游 `exchange_calendars` deprecation warning。Schema 覆盖 V1/V2 兼容、V2 horizon/day 条件和股票/期权 stop 字段约束。

## 2026-08-20 (America/Los_Angeles) - PR comment 5363719068 mandate horizon 修复

- `ai_instrument_allocator_v1` 的最长持仓时间改为以交易所 session 计算并持久化的 `planned_exit_at`。allocator monitor 不再叠加旧策略的 `max_holding_calendar_days` 自然日 time stop；止损、止盈、期权 DTE/到期/sellout、确定性失效和收盘强平继续执行，旧策略默认行为保持不变。
- actionable signal 只有在 `thesis_valid_until >= planned_exit_at` 时才可进入工具分配和 paper order 路径。回归覆盖 `intraday_close`、`next_close` 和 5 个交易日 horizon 的早于/等于/晚于边界，并包含美国独立日休市和周末场景；提前到期的 thesis 会在创建股票或期权订单前返回结构化 `no_trade`。
- mandate state 的根值为 null、单条记录为 null/scalar/list 或时间字段损坏时不再拖垮 monitor。非 dict 记录被忽略，缺失 mandate 或损坏时间统一触发 fail-closed 退出。
- 明确 `invalidation_condition` 在 V1 只是研究和审计自由文本，不由周期性 LLM 自动判定。只有确定性规则、人工动作或 replay 转换设置 `invalidation_triggered` 才会触发 thesis invalidation；Dashboard 的退出计划同步显示这一边界。
- 针对评论的 focused 回归为 `19 passed`，Dashboard 为 `40 passed`，全套 pytest 为 `304 passed`，只有 4 条上游 `exchange_calendars` deprecation warning。Playwright 在 1440x1000 和 390x844 下验证“持仓与订单”视图，无横向溢出且 console 为 0 error / 0 warning；临时 8790 进程已关闭，用户的 8787 Dashboard 未停止。

## 2026-08-20 (America/Los_Angeles) - 全量提交前终审

- 审核本分支全部 allocator、期权定价、schema、测试、文档和 dashboard 改动。独立 Luna Max 审查发现并修复了严格 paper 三态、损坏状态恢复、非有限概率与异常执行时间戳等边界问题。
- `assert_paper_mode` 现在只接受 `paper=true`、`live_readonly=false`、`live_trading=false`；healthcheck 返回配置中的真实三态值，不再硬编码。没有增加或调用任何 Robinhood 写工具。
- allocator 会跳过损坏的 null state record；持久化 signal 不是 object、actionable 字段缺失、概率包含 NaN/Infinity、报价时间无效或计划退出时间无效时，统一返回结构化 `no_trade`，不会继续获取订单或让 scheduler 因解析异常退出。
- dashboard 会忽略 null 订单/持仓记录。只有 `filled`、`cancelled`、`expired` 和 `rejected` 视为已结束，未知状态保留在未完成订单并显示警告；健康页的 broker 边界与顶部使用同一三态判定。
- dashboard focused 测试为 `40 passed`；全套 pytest 为 `285 passed`，只有 4 条上游 `exchange_calendars` deprecation warning。Playwright 在 1440x1000 和 390x844 下逐页验证五个视图，页面无横向溢出，console 为 0 error / 0 warning；专用 8790 验证进程已关闭，用户的 8787 dashboard 未停止。

## 2026-08-19 (America/Los_Angeles) - 五视图模拟交易控制台

- 将原单一长页面重构为 `总览`、`持仓与订单`、`策略表现`、`AI 决策` 和 `系统健康` 五个 tab。默认首屏同时显示旧 `$2,000` 账本与独立 `$10,000 ai_instrument_allocator_v1` sleeve，并把“系统现在在做什么”和当前阻塞事项置于历史统计之前。
- 持仓和订单按主账户、旧 AI sleeve、allocator sleeve 分开聚合；open order 不再以视觉方式混同持仓，最近 20 笔已结束订单默认折叠。旧 `long_directional_options_v2_weighted` 显示为“只管理旧仓”，`ai_gated_technical_v1` 显示为“影子研究 / 管理旧仓”。
- AI 页按候选、Exa、DeepSeek、Challenge 和 deterministic Python risk veto 展示结构化链路。长证据改为短摘要并按需展开，最近深度催化记录限制为 6 条；raw private chain-of-thought、`reasoning_content` 和凭据仍不暴露。
- 健康页把当前 heartbeat/component 状态与最近交易日历史计数分离；unknown 使用中性状态，历史失败不再显示成当前仍在发生。页面支持 URL hash、ARIA tabs、方向键和 Home/End。
- `_read_jsonl` 改为从文件尾部按 64 KiB 块读取最后 N 条有效 JSONL，能忽略正在 append 的半截记录，不再每 5 秒读取完整大日志。轮询改为 15 秒，浏览器隐藏时暂停。HTTP handler 只实现 `GET`、`HEAD` 和 `OPTIONS`，dashboard 仍不导入 broker adapter，也没有真实下单、重启或修改配置入口。
- Luna Max 终审后补齐三元模式校验：只有 `paper=true`、`live_readonly=false`、`live_trading=false` 同时满足才显示 Paper only。空 scheduler 不再显示绿色正常；顶部服务心跳、股票报价和健康页期权报价使用不同时间戳，且长时间间隔改为分钟、小时或天。损坏或未来 heartbeat、未来报价统一按 stale fail-closed 展示，不会被夹成 0 秒后误标新鲜。
- 主账户、旧 AI sleeve 和 allocator sleeve 的 open order 全部保留并全部显示，完成订单先按状态筛选再各自保留最近记录；页面明确显示“最近 20 / 总数”，不再因服务端先截 30 条而静默遗漏历史。News Drift SQLite/JSON 异常现在只令该 metrics 组件降级，不会让 `/api/state` 整体失败；allocator 数量也在写入 `innerHTML` 前转义。
- metrics 按配置、账户状态和相关日志的 mtime/size 签名缓存，文件变化后自动失效；News Drift 同时跟踪 SQLite 主文件、WAL、SHM、模型用量与周期日志，旧 AI directional metrics 同时跟踪其决策日志。现场连续请求从约 506-600 ms 降到约 196-270 ms，响应从约 411 KB 降到约 394 KB。
- “最近交易日”优先采用 heartbeat 中最新 forward exchange session，不再被未滚动的 `daily_counters.date` 锁在前一日。旧 AI sleeve 和 allocator 的累计 PnL 改为当前净值减初始资金，入场数由已平仓加当前持仓确定；旧 AI 明确标为“影子研究 / 管理旧仓”，News Drift 标签不再冒充平仓交易，样本达标但未盈利也会明确显示未通过盈利门槛。
- focused dashboard 测试为 38 passed；全套 pytest 为 278 passed，只有 4 条上游 `exchange_calendars` deprecation warning。Playwright 在 1440x1000 和 390x844 下逐页检查，未发现 console error 或页面横向溢出；AI 桌面页约 2,400px。
- 检修期间没有停止或重启用户的 forward service。浏览器验收使用独立 `127.0.0.1:8790` dashboard 进程，不访问真实下单工具。

## 2026-08-19 (America/Los_Angeles) - AI Instrument Allocator V1 与旧策略有序退出

### PR #2 review 修复

- 修复旧 `ai_gated_technical_v1` 在 `new_entries_enabled=false` 时过早返回的问题。旧持仓和 open order 继续由原 monitor/exit 管理，同时 discovery、Exa、ranking、News、Challenge 和 Decision 继续产生 shadow 对照记录；actionable 结果在执行报价、entry risk 和 broker 之前返回 `shadow_only`，不发布可执行 signal，也不创建 paper order。
- allocator 的 08:00 和 09:25 不再复用完整 `_research_stage`。08:00 只遍历 active plan ticker，只有发现新 URL/event fingerprint/content hash 时才运行 fast News、Challenge、Decision，共 3 次模型调用；09:25 只运行 fast News 和 Challenge，共 2 次，只能保留或否决原方向。两阶段无新证据均为 0 次模型调用，且不再执行 universe discovery、技术 context 或 ranker；09:32 继续保持 0 次模型调用和 fresh executable revalidation。
- 盘前 plan 替换改为一次 state-file 原子写，同时写入“新 plan active”和“旧 plan superseded”，消除重启时双 active plan 窗口。08:00 evidence refresh 失败、08:00/09:25 非预期模型失败都会先 invalidated 旧 plan，再尝试写 decision audit；即使 audit append 失败，执行状态也已 fail closed。09:25 成功完成时还会写入当日 `preopen_revalidated_at` 执行许可（无新证据时仍写入）；状态写失败或该阶段缺席时，09:32 拒绝旧计划。

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
- 最终 `compileall` 通过，pytest 为 `225 passed`，仅有 4 条上游 `exchange_calendars` deprecation warning。只读 healthcheck/readiness 确认 Robinhood 54-tool manifest、期权数据、Exa、DeepSeek、Alpaca fallback 和 Vibe 均 ready，`ready_for_ai_instrument_allocator_paper=true`、`ready_for_full_forward_evaluation=true`、`paper_mode=true`、`live_trading=false`。
- 三套离线 dry run 均为 `used_network=false`、`used_live_order_tools=false`；allocator 的 overnight、premarket、pre-open、09:32 execution、monitor 和 restart 临时目录验证为 6 项通过。执行时钟会推进到本轮最晚的股票或期权报价观察时间，新增回归确认晚到 5 秒的期权报价不会再被误拒为 future quote。Playwright 在 1440x1000 和 390x844 下确认无页面横向溢出、0 console error，并修复浏览器取消刷新时的 `ConnectionAbortedError` 终端 traceback。最终 `graphify update .` 完成为 2,451 nodes / 6,487 edges。
- 测试后没有保留 forward service 或 dashboard 进程。连续 scheduler 配置只有在用户从自己的终端重新启动服务后才会生效。
- 审计发现旧 `--readiness` 会因构造完整 service 而提前初始化空白 allocator state。本次运行已在 19:17 生成新的 `$10,000` 空白 namespace；它没有订单、成交或 PnL，且没有触碰旧账本，因此予以保留。CLI 已改为直接调用无状态 healthcheck，文件哈希验证后续 readiness 前后 state 完全一致。

### 完成度复核与补充修复

- 复核发现夜间 Prompt 正确要求 `entry_now=false`，但 09:32 执行路径曾把这个字段再次当作即时拒绝条件，导致真实 overnight conditional plan 永远不能成交。现在只有 `overnight`、`premarket_update` 或 `preopen_revalidation` 保存的 active plan 可在 09:32-09:37 ET 的 `open_execution` 窗口绕过“研究当下不可下单”的语义；窗口外调用和遗留 intraday plan 均拒绝。intraday 仍必须由模型明确给出 `entry_now=true`。开盘路径仍为 0 次 LLM 调用，并重新获取 quote、重算股票/期权经济性、生成最多 300 秒的授权记录并经过最终 deterministic risk veto。
- 盘前或盘中同 ticker 的新分析现在会把更早 active plan 标记为 `superseded`；若新分析 fail closed 或 no-trade，则旧计划标记为 `invalidated`。因此新出现的反向证据不会与旧方向同时排队，也不会在 09:32 先执行过时计划。
- 成功发送给 ranker 的全部候选事件都会写入 ticker/event cooldown，不限于进入 top-3 deep analysis 的候选；深度分析最终形成 plan 或 no-trade 都不会改变这项登记。同一 event fingerprint 在 cooldown 内不会每小时重复消耗 DeepSeek 调用；不可变 evidence snapshot 和 no-trade 决策日志仍保留。ranking 本身失败时允许后续重试，且仍不会形成计划或订单。
- `$2,000` 股票 counterfactual 的 `proposed_risk_usd` 和 `risk_pct_of_nav` 改为按该账户实际可买的缩放后股数计算，不再错误复用 `$10,000` sleeve 的整笔风险。期权仍以同一离散 contract 的一张实际 premium 风险判断可负担性。
- EOD guard 会调用 allocator monitor，因此 supervisor 的 EOD worker 资源集合补上 `allocator_account`；它现在不会与独立 allocator stage/monitor 并发写同一账户。完整 mock 流程覆盖 overnight、premarket、pre-open、open execution、monitor 和 restart，且确认 research-only 阶段 0 paper order、09:32 为 0 模型调用、所有结果 `live_order_tools_called=false`。

### Forecast anchoring 与 conservative hurdle review 修复

- 首次完整研究后由 Python 固定 `forecast_reference_price/time`；08:00 更新继承同一 reference 和 horizon。09:32 只计算原目标相对最新价的 `remaining_move_pct`，缺失或未来 reference 会 fail closed，不再把旧预测重锚定到最新报价。
- conservative magnitude 改为所选方向内最弱 50% 概率质量的多场景加权结果，dominant bucket 只保留为诊断。股票、long call、long put 和看跌 shadow benchmark 均使用 remaining move；期权继续跨 IV contraction/base/expansion 做重新定价。
- 08:00 fast News、Challenge、Decision 明确接收 prior signal 和仅新增 evidence。Decision 可修订 signed buckets 或 no-trade，但不得改变 ticker、horizon 或 forecast reference；旧证据 URL 仅能从已验证 prior signal 继承。
- allocation 显式记录 nearest desired-direction option 的 horizon-scaled market-implied move、forecast/implied 比率、IV、option id 和比较标志；这些字段只作市场诊断，不形成未校准 probability EV。
- 新增 GitHub Actions Windows/Python 3.13 `pytest` workflow；本地 `compileall` 通过。仅包含本次提交的 staged tree 为 `248 passed`；叠加独立未提交 dashboard 工作线的完整工作区为 `258 passed`，两者都只有 4 条既有上游 deprecation warning。

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
