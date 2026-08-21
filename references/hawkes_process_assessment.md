# Hawkes Process 在本项目中的适用性评估

## 结论

当前不把 Hawkes Process 接入候选评分、期权方向、风险或订单执行。原因不是实现困难，而是本项目尚无适合估计自激点过程的数据。现在强行拟合更可能学习 Exa 轮询节奏和重复报道，而不是市场微观结构中的可交易到达强度。

## 它能解决什么问题

多变量 Hawkes Process 通常把第 `i` 类事件强度写成：

```text
lambda_i(t) = mu_i(t) + sum_j sum_(t_k^j < t) phi_ij(t - t_k^j)
```

`mu_i` 是基线强度，`phi_ij` 描述第 `j` 类历史事件对第 `i` 类未来事件到达率的短期影响。在交易研究中，事件通常是逐笔买卖、限价单提交、撤单、价格跳变或不同盘口状态。它可以研究订单流聚集、买卖方向交叉激发、短期波动和执行风险。

主要参考：

- Bacry、Mastromatteo、Muzy 的金融 Hawkes 综述：[Hawkes processes in finance](https://arxiv.org/abs/1502.04592)。
- Morariu-Patrichi、Pakkanen 对 state-dependent order flow 的研究：[State-dependent Hawkes processes and their application to limit order book modelling](https://arxiv.org/abs/1809.08060)。
- Lallouache、Challet 对显著性和非平稳误判的讨论：[Nonparametric Hawkes Processes and Financial Data](https://papers.ssrn.com/sol3/Delivery.cfm/SSRN_ID2583431_code1821037.pdf?abstractid=2450101&mirid=1)。

## 当前数据为什么不够

本次审计时，新闻 ledger 有 211 个事件、165 个 ticker，单 ticker 最多只有 5 个事件，没有 ticker 达到 10 个事件。Exa discovery 每 15 分钟最多轮询一次，因此 `first_seen_at` 还包含人为采样间隔。

这些数据同时存在四个问题：

1. 单资产事件数远低于可靠估计 kernel 所需规模；
2. 新闻发布时间、事件实际发生时间和项目首次发现时间并不相同；
3. 多家媒体转载同一事件会产生来源聚集，并不等于公司基本面再次发生冲击；
4. 市场开闭、财报季和新闻源覆盖变化造成明显非平稳基线。

在这种条件下，训练得到的高强度区间不能被解释为价格将涨或跌，也不能证明存在扣除 spread/slippage 后的收益。

## 未来允许的最小实验

只有取得 point-in-time 逐笔成交或 order-book event stream 后，才建立独立 `hawkes_order_flow_shadow`，并满足：

1. 事件至少区分 buyer-initiated trade、seller-initiated trade、limit add、cancel 和 price move；
2. 交易所时间戳精度、时区、session 和数据许可可审计；
3. 每个训练资产每天有数千个事件，并按日滚动估计，禁止使用未来事件；
4. 比较指数 kernel、非参数 kernel 和不使用 Hawkes 的简单 order-flow baseline；
5. 检查 branching ratio/stability、time-rescaling residual、goodness-of-fit 和参数漂移；
6. 只输出 shadow feature，不直接输出订单；
7. 使用相同 snapshot、fill model 和成本模型做 out-of-sample 对照；
8. 未显示独立净收益增益时删除实验，不保留无效复杂度。

## 当前工程影响

- 不新增 Hawkes Python dependency。
- 不修改现有 deterministic risk gate。
- 不把稀疏新闻 arrival count 当作交易信号。
- 本评估作为 future readiness contract；它不代表 Hawkes 已实现或已验证。
