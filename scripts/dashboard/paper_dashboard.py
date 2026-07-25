"""Local, read-only GUI for the paper/shadow trading audit trail.

This server never imports a broker adapter and exposes no mutation endpoint.
It is deliberately separate from the scheduler so it can inspect a live service
without competing for its process lock or current-user OAuth credentials.
"""

from __future__ import annotations

import argparse
import json
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from scripts.core.config import load_runtime_config
from scripts.evaluation.calculate_metrics import calculate_metrics


def _read_json(path: Path, default: Any) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return default


def _read_jsonl(path: Path, limit: int = 400) -> list[dict[str, Any]]:
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return []
    records: list[dict[str, Any]] = []
    for line in lines[-limit:]:
        if not line.strip():
            continue
        try:
            record = json.loads(line)
        except json.JSONDecodeError:
            # The service can be appending while the dashboard reads. Ignore a
            # single incomplete line and retry on the next browser refresh.
            continue
        if isinstance(record, dict):
            records.append(record)
    return records


def _last(records: list[dict[str, Any]], predicate: Any) -> dict[str, Any] | None:
    for record in reversed(records):
        if predicate(record):
            return record
    return None


def _technical_reasons(decision: dict[str, Any], snapshot: dict[str, Any]) -> list[str]:
    reasons: list[str] = []
    regime = decision.get("regime", {})
    technical = decision.get("technical", {})
    if not regime.get("eligible", False):
        reasons.extend(regime.get("reasons") or ["market regime is not eligible"])
    if technical.get("reasons"):
        reasons.extend(str(value) for value in technical["reasons"])
        if technical.get("chase_allowed") is False:
            reasons.append("chase score exceeds the configured entry cap")
        return reasons
    if snapshot.get("market_session") != "regular":
        reasons.append("outside the regular NYSE session")
    if not technical.get("quote_valid", False):
        reasons.append(str(technical.get("quote_reason", "quote is not valid")))
    if float(technical.get("relative_strength_20d", 0)) < 0.5:
        reasons.append("20-day relative strength is below 0.5 percentage points")
    if float(technical.get("price_change_5d_pct", 0)) <= 0:
        reasons.append("5-day price change is not positive")
    if technical.get("volume_ratio") is None:
        reasons.append("intraday volume confirmation is unavailable")
    elif float(technical.get("volume_ratio", 0)) < 0.8:
        reasons.append("volume ratio is below 0.8")
    return reasons or ["all deterministic entry conditions passed"]


def _candidate_rows(decision_records: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], dict[str, dict[str, Any]]]:
    latest: dict[str, dict[str, Any]] = {}
    by_snapshot: dict[str, dict[str, Any]] = {}
    for record in reversed(decision_records):
        if record.get("event") != "baseline_decision":
            continue
        decision = record.get("decision")
        snapshot = record.get("snapshot")
        if not isinstance(decision, dict) or not isinstance(snapshot, dict):
            continue
        ticker = str(decision.get("ticker", ""))
        snapshot_id = str(decision.get("snapshot_id", ""))
        item = {
            "asof": record.get("ts"),
            "ticker": ticker,
            "action": decision.get("action", "no_trade"),
            "regime": decision.get("regime", {}),
            "technical": decision.get("technical", {}),
            "reasons": _technical_reasons(decision, snapshot),
            "snapshot_id": snapshot_id,
        }
        if ticker and ticker not in latest:
            latest[ticker] = item
        if snapshot_id and snapshot_id not in by_snapshot:
            by_snapshot[snapshot_id] = item
    return sorted(latest.values(), key=lambda item: item["ticker"]), by_snapshot


def _exit_rows(decision_records: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    latest: dict[str, dict[str, Any]] = {}
    for record in reversed(decision_records):
        if record.get("event") != "exit_evaluation":
            continue
        symbol = str(record.get("symbol", ""))
        if symbol and symbol not in latest:
            latest[symbol] = {"asof": record.get("ts"), **dict(record.get("decision", {}))}
    return latest


def _option_decision_rows(decision_records: list[dict[str, Any]]) -> list[dict[str, Any]]:
    latest: dict[str, dict[str, Any]] = {}
    for record in reversed(decision_records):
        if record.get("event") != "option_strategy_decision" or not isinstance(record.get("decision"), dict):
            continue
        decision = dict(record["decision"])
        ticker = str(decision.get("ticker", ""))
        if ticker and ticker not in latest:
            latest[ticker] = {"asof": record.get("ts"), **decision}
    return sorted(latest.values(), key=lambda item: item["ticker"])


def _safe_shadow_record(record: dict[str, Any]) -> dict[str, Any]:
    """Expose structured evidence, never raw provider reasoning_content."""
    decision = dict(record.get("decision") or {})
    challenge = dict(record.get("challenge") or {}) if isinstance(record.get("challenge"), dict) else None
    news = dict(record.get("news") or {}) if isinstance(record.get("news"), dict) else None
    return {
        "asof": record.get("ts"),
        "ticker": record.get("ticker"),
        "snapshot_id": record.get("snapshot_id"),
        "action": record.get("action"),
        "fail_closed": bool(record.get("fail_closed", False)),
        "risk_approved": bool(record.get("risk_approved", False)),
        "risk_reason": record.get("risk_reason"),
        "guardrail_actions": record.get("guardrail_actions", []),
        "model_calls": record.get("model_calls", 0),
        "decision": {
            key: decision.get(key)
            for key in ("thesis", "supporting_evidence", "contrary_evidence", "no_trade_reason", "confidence")
        },
        "challenge": None
        if challenge is None
        else {key: challenge.get(key) for key in ("recommendation", "veto_recommended", "objections", "contradictions", "missing_evidence", "stale_evidence")},
        "news": None
        if news is None
        else {key: news.get(key) for key in ("direction", "confidence", "source", "published_at", "events", "data_gaps")},
    }


def build_dashboard_state(root: str | Path) -> dict[str, Any]:
    """Build a bounded, JSON-safe view of existing local state and logs."""
    root_path = Path(root).resolve()
    state_dir = root_path / "state"
    logs_dir = root_path / "logs"
    runtime = load_runtime_config(root_path)
    audit_records = _read_jsonl(logs_dir / "audit.jsonl")
    decision_records = _read_jsonl(logs_dir / "decisions.jsonl")
    shadow_records = _read_jsonl(logs_dir / "shadow_decisions.jsonl", limit=100)
    candidates, decisions_by_snapshot = _candidate_rows(decision_records)
    exit_by_symbol = _exit_rows(decision_records)
    option_decisions = _option_decision_rows(decision_records)
    shadow_by_snapshot: dict[str, dict[str, Any]] = {}
    for record in reversed(shadow_records):
        snapshot_id = str(record.get("snapshot_id", ""))
        if snapshot_id and snapshot_id not in shadow_by_snapshot:
            shadow_by_snapshot[snapshot_id] = _safe_shadow_record(record)

    orders_raw = _read_json(state_dir / "paper_orders.json", {})
    orders = list(orders_raw.values()) if isinstance(orders_raw, dict) else []
    orders.sort(key=lambda item: str(item.get("submitted_at") or item.get("created_at") or ""), reverse=True)
    for order in orders:
        snapshot_id = str(order.get("decision_id", ""))
        order["baseline_explanation"] = decisions_by_snapshot.get(snapshot_id)
        order["shadow_explanation"] = shadow_by_snapshot.get(snapshot_id)

    positions_raw = _read_json(state_dir / "paper_positions.json", {})
    positions = list(positions_raw.values()) if isinstance(positions_raw, dict) else []
    for position in positions:
        position["exit_evaluation"] = exit_by_symbol.get(str(position.get("symbol", "")))

    option_orders_raw = _read_json(state_dir / "paper_option_orders.json", {})
    option_orders = list(option_orders_raw.values()) if isinstance(option_orders_raw, dict) else []
    option_orders.sort(key=lambda item: str(item.get("submitted_at") or item.get("created_at") or ""), reverse=True)
    option_positions_raw = _read_json(state_dir / "paper_option_positions.json", {})
    option_positions = list(option_positions_raw.values()) if isinstance(option_positions_raw, dict) else []

    latest_cycle = _last(audit_records, lambda item: item.get("event") in {"forward_cycle_complete", "forward_cycle_exit_only", "forward_cycle_skipped", "forward_cycle_failed_closed"})
    last_shadow = _safe_shadow_record(shadow_records[-1]) if shadow_records else None
    paper = runtime["paper"]
    risk = runtime["risk"]
    thinking = runtime["llm"].get("api", {}).get("thinking")
    return {
        "mode": {"paper": bool(paper.get("mode", {}).get("paper", False)), "live_trading": bool(paper.get("mode", {}).get("live_trading", False))},
        "heartbeat": _read_json(state_dir / "runtime_heartbeat.json", {}),
        "account": _read_json(state_dir / "paper_account.json", {}),
        "daily_counters": _read_json(state_dir / "daily_counters.json", {}),
        "positions": positions,
        "option_positions": option_positions,
        "orders": orders[:30],
        "option_orders": option_orders[:30],
        "latest_cycle": latest_cycle,
        "candidates": candidates,
        "option_decisions": option_decisions,
        "metrics": calculate_metrics(root_path),
        "last_shadow_decision": last_shadow,
        "safety": {
            "allow_options": bool(risk.get("allow_options", False)),
            "allow_fractional_shares": bool(risk.get("allow_fractional_shares", False)),
            "fractional_share_increment": risk.get("fractional_share_increment"),
            "max_order_pct_of_equity": risk.get("max_order_pct_of_equity"),
            "max_open_positions": risk.get("max_open_positions"),
            "max_daily_trades": risk.get("max_daily_trades"),
            "options_risk": runtime.get("options_risk", {}),
            "shared_risk": runtime.get("shared_risk", {}),
            "thinking": thinking,
        },
    }


_PAGE = """<!doctype html>
<html lang="zh-CN"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Paper Trading Control Room</title>
<style>
:root{color-scheme:dark;--bg:#0b1020;--panel:#151d35;--line:#293550;--text:#e9eefb;--muted:#a5b1cc;--ok:#55dba6;--warn:#ffcb6b;--bad:#ff7272;--blue:#82aaff}*{box-sizing:border-box}body{font:14px/1.45 Inter,Segoe UI,Arial,sans-serif;background:var(--bg);color:var(--text);margin:0;padding:22px}h1{font-size:24px;margin:0}h2{font-size:16px;margin:0 0 10px}p{margin:5px 0;color:var(--muted)}.grid{display:grid;gap:14px;grid-template-columns:repeat(auto-fit,minmax(240px,1fr));margin:14px 0}.panel{border:1px solid var(--line);background:var(--panel);border-radius:8px;padding:15px;overflow:auto}.wide{grid-column:1/-1}.metric{font-size:22px;font-weight:650}.tag{display:inline-block;border-radius:999px;padding:2px 8px;background:#263352;color:var(--text);margin:2px}.ok{color:var(--ok)}.warn{color:var(--warn)}.bad{color:var(--bad)}table{border-collapse:collapse;width:100%;min-width:740px}th,td{text-align:left;vertical-align:top;border-bottom:1px solid var(--line);padding:8px}th{color:var(--muted);font-weight:600}ul{margin:6px 0;padding-left:18px}.mono{font-family:Consolas,monospace;font-size:12px}.small{font-size:12px;color:var(--muted)}button{background:#415a9d;color:white;border:0;border-radius:6px;padding:8px 12px;cursor:pointer}details{margin:6px 0}summary{cursor:pointer;color:var(--blue)}#updated{color:var(--muted);font-size:12px;margin-left:10px}@media(max-width:640px){body{padding:12px}.wide{grid-column:auto}}</style></head>
<body><header><h1>Paper Trading Control Room <span id="updated"></span></h1><p>只读本地审计界面；没有下单按钮，也不会访问 Robinhood 交易工具。</p></header><main id="app">加载中…</main>
<script>
const esc=v=>String(v??'').replace(/[&<>'"]/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;',"'":'&#39;','"':'&quot;'}[c]));
const pct=v=>v===undefined||v===null?'—':Number(v).toFixed(4)+'%'; const money=v=>v===undefined||v===null?'—':'$'+Number(v).toFixed(2);
const list=v=>Array.isArray(v)&&v.length?'<ul>'+v.map(x=>'<li>'+esc(x)+'</li>').join('')+'</ul>':'—';
const lineStatus=l=>`<span class="tag ${l.promotion_eligible?'ok':'warn'}">${esc(l.profitability||'insufficient_forward_evidence')}</span>`;
function cycle(d){let c=d.latest_cycle||{},clock=c.clock||{};return `<div class="panel"><h2>最近周期</h2><p><b>${esc(c.event||'尚无周期')}</b> · ${esc(clock.asof||'—')}</p><p>行情 ${esc(c.quotes??'—')} · 快照 ${esc(c.snapshots??'—')} · 通过筛选 ${esc(c.baseline_candidates??'—')}</p><p>入选：${list(c.selected_candidates||[])}</p><p class="small">订单 ${esc((c.orders||[]).length)} · 退出 ${esc((c.exits||[]).length)}</p></div>`}
function portfolio(d){let a=d.account||{},pos=d.positions||[],c=d.daily_counters||{},m=d.metrics||{},lines=m.lines||{};return `<div class="panel"><h2>共享纸面账户</h2><div class="metric">${money(a.cash)} 现金</div><p>净值 ${money(m.ending_equity)} · 已实现 ${money(a.realized_pnl)}</p><p>今日共 ${esc(c.trades??0)} 笔；股票 ${esc(c.equity_trades??0)} / 期权 ${esc(c.option_trades??0)}</p><p>股票 PnL ${money((lines.equity||{}).net_pnl)} · 期权 PnL ${money((lines.options||{}).net_pnl)}</p><p>股票证据：${lineStatus(lines.equity||{})}</p><h3>股票持仓</h3>${pos.length?pos.map(p=>`<details open><summary>${esc(p.symbol)} · ${esc(p.quantity)} 股 @ ${money(p.average_price)}</summary><p>开仓：${esc(p.opened_at)}</p><p>退出判断：${esc((p.exit_evaluation||{}).reason||'尚无记录')}</p></details>`).join(''):'<p>无股票持仓</p>'}</div>`}
function safety(d){let s=d.safety||{},t=s.thinking||{},o=s.options_risk||{},r=s.shared_risk||{};return `<div class="panel"><h2>边界与模型</h2><p><span class="tag ok">纸面模式</span> <span class="tag ${s.allow_options?'ok':'bad'}">期权纸面：${s.allow_options?'启用':'禁止'}</span></p><p>共享总占用 ≤ ${Number(r.max_total_deployed_pct_of_equity||0)*100}%；股票 ≤ ${Number(r.max_equity_deployed_pct_of_equity||0)*100}%；期权 ≤ ${Number(r.max_options_deployed_pct_of_equity||0)*100}%</p><p>期权仅长 call/put；单笔权利金风险 ≤ ${Number(o.max_order_risk_pct_of_equity||0)*100}%；禁止卖方、保证金、行权</p><p>分数股：${s.allow_fractional_shares?'允许，每 '+esc(s.fractional_share_increment)+' 股递增':'禁止'}</p><p>Thinking：新闻 ${esc((((t.agents||{}).news_agent||t.default||{}).type||'默认'))}；质询 ${esc((((t.agents||{}).challenge_agent||t.default||{}).type||'默认'))}；决策 ${esc((((t.agents||{}).decision_manager||t.default||{}).type||'默认'))}</p></div>`}
function optionLine(d){let pos=d.option_positions||[],orders=d.option_orders||[],dec=d.option_decisions||[],line=((d.metrics||{}).lines||{}).options||{},g=(((d.latest_cycle||{}).portfolio||{}).option_greeks||{});let ds=dec.map(x=>`<tr><td>${esc(x.ticker)}</td><td>${esc(x.option_type||'—')}</td><td class="${x.action==='buy_to_open'?'ok':'warn'}">${esc(x.action)}</td><td>${list(x.reasons)}</td></tr>`).join('');return `<section class="panel wide"><h2>期权策略线 · ${esc(line.closed_trade_count||0)} 笔平仓 · 胜率 ${pct(Number(line.win_rate||0)*100)} · PnL ${money(line.net_pnl)}</h2><p>独立证据：${lineStatus(line)} · 不借用股票交易样本</p><p>组合 Greeks：Delta ${esc(g.delta??'—')} · Gamma ${esc(g.gamma??'—')} · Theta/日 ${esc(g.theta_per_day??'—')} · Vega/1% ${esc(g.vega_per_vol_point??'—')}</p><p>${pos.length?pos.map(p=>{let c=p.contract||{};return `${esc(c.underlying)} ${esc(c.expiration_date)} ${esc(c.strike_price)}${esc((c.option_type||'')[0]||'')} · ${esc(p.quantity)} 张 @ ${money(p.average_price)}`}).join('<br>'):'无期权持仓'}</p><table><thead><tr><th>标的</th><th>方向</th><th>决策</th><th>原因</th></tr></thead><tbody>${ds||'<tr><td colspan="4">尚无期权筛选记录</td></tr>'}</tbody></table><h3>期权订单</h3>${orders.length?orders.map(o=>{let c=o.contract||{};return `<details><summary>${esc(c.underlying)} ${esc(c.expiration_date)} ${esc(c.strike_price)}${esc((c.option_type||'')[0]||'')} · ${esc(o.intent)} · ${esc(o.status)}</summary><p>${esc(o.quantity)} 张 @ ${money(o.average_fill_price||o.limit_price)}；${esc(o.reject_reason||o.thesis||'')}</p></details>`}).join(''):'<p>尚无期权订单</p>'}</section>`}
function candidates(d){let rows=(d.candidates||[]).map(x=>{let t=x.technical||{},r=x.regime||{};return `<tr><td>${esc(x.ticker)}</td><td class="${x.action==='buy'?'ok':'warn'}">${esc(x.action)}</td><td>${esc(r.status)} / ${r.eligible?'可':'否'}</td><td>${pct(t.relative_strength_20d)}</td><td>${pct(t.price_change_5d_pct)}</td><td>${Number(t.volume_ratio??0).toFixed(2)}</td><td>${list(x.reasons)}</td></tr>`}).join('');return `<section class="panel wide"><h2>十标的硬筛选：为什么买 / 为什么不买</h2><table><thead><tr><th>标的</th><th>结果</th><th>市场状态</th><th>20日相对强度</th><th>5日变化</th><th>量比</th><th>可审计原因</th></tr></thead><tbody>${rows||'<tr><td colspan="7">尚无基线决策记录</td></tr>'}</tbody></table></section>`}
function orders(d){let rows=(d.orders||[]).map(o=>{let b=o.baseline_explanation||{},sh=o.shadow_explanation||{},t=(b.technical||{});let shadow=sh.decision||{};return `<details><summary>${esc(o.symbol)} ${esc(o.side)} ${esc(o.filled_quantity||o.quantity)} 股 · ${esc(o.status)} · ${money(o.average_fill_price||o.limit_price)}</summary><p>订单 ID：<span class="mono">${esc(o.order_id)}</span></p><p>基线策略：${esc(o.thesis)}。当时 RS20 ${pct(t.relative_strength_20d)}，5日 ${pct(t.price_change_5d_pct)}，量比 ${esc(t.volume_ratio??'—')}。</p><p>影子判断：${esc(sh.action||'未运行')}；风控：${esc(sh.risk_reason||'—')}；模型调用 ${esc(sh.model_calls??'—')}。</p><p>结构化投资论点：${esc(shadow.thesis||'—')}</p><p>支持证据：${list(shadow.supporting_evidence)} 反证：${list(shadow.contrary_evidence)}</p><p>质询：${list((sh.challenge||{}).objections)}；保护动作：${list(sh.guardrail_actions)}</p></details>`}).join('');return `<section class="panel wide"><h2>订单与可解释链路</h2>${rows||'<p>尚无纸面订单。</p>'}</section>`}
function shadow(d){let x=d.last_shadow_decision;if(!x)return '<section class="panel wide"><h2>最近影子研究</h2><p>尚无影子决策。</p></section>';let z=x.decision||{};return `<section class="panel wide"><h2>最近影子研究（结构化理由，不显示原始私有 CoT）</h2><p>${esc(x.ticker)}：<b>${esc(x.action)}</b> · 风控 ${esc(x.risk_reason)} · ${x.fail_closed?'失败关闭':'正常完成'}</p><p>论点：${esc(z.thesis)}；置信度：${esc(z.confidence)}</p><p>支持：${list(z.supporting_evidence)} 反证：${list(z.contrary_evidence)}</p><p>质询：${list((x.challenge||{}).objections)}；缺失证据：${list((x.challenge||{}).missing_evidence)}</p></section>`}
function render(d){document.getElementById('updated').textContent='刷新 '+new Date().toLocaleTimeString();let h=d.heartbeat||{};document.getElementById('app').innerHTML=`<div class="grid"><div class="panel"><h2>运行状态</h2><div class="metric ${h.status==='ok'?'ok':'warn'}">${esc(h.status||'未知')}</div><p>${esc(h.last_heartbeat_at||'没有心跳')}</p><p>${esc((h.payload||{}).event||'—')}</p></div>${portfolio(d)}${safety(d)}${cycle(d)}</div><div class="grid">${candidates(d)}${optionLine(d)}${orders(d)}${shadow(d)}</div>`}
async function refresh(){try{render(await fetch('/api/state',{cache:'no-store'}).then(r=>r.json()))}catch(e){document.getElementById('app').textContent='读取本地状态失败：'+e}}refresh();setInterval(refresh,5000);
</script></body></html>"""


def make_handler(root: Path) -> type[BaseHTTPRequestHandler]:
    class DashboardHandler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:  # noqa: N802
            path = urlparse(self.path).path
            if path == "/api/state":
                body = json.dumps(build_dashboard_state(root), ensure_ascii=False).encode("utf-8")
                self.send_response(HTTPStatus.OK)
                self.send_header("Content-Type", "application/json; charset=utf-8")
            elif path == "/":
                body = _PAGE.encode("utf-8")
                self.send_response(HTTPStatus.OK)
                self.send_header("Content-Type", "text/html; charset=utf-8")
            else:
                body = b"Not found"
                self.send_response(HTTPStatus.NOT_FOUND)
                self.send_header("Content-Type", "text/plain; charset=utf-8")
            self.send_header("Cache-Control", "no-store")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, _format: str, *_args: Any) -> None:
            return

    return DashboardHandler


def main() -> None:
    parser = argparse.ArgumentParser(description="Serve the read-only paper-trading dashboard.")
    parser.add_argument("--root", default=str(Path(__file__).resolve().parents[2]))
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8787)
    args = parser.parse_args()
    root = Path(args.root).resolve()
    server = ThreadingHTTPServer((args.host, args.port), make_handler(root))
    print(f"Paper Trading Control Room: http://{args.host}:{args.port}", flush=True)
    print("Read-only dashboard; Ctrl+C stops only this dashboard server.", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
