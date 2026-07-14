"use client";

import { useEffect, useMemo, useRef, useState } from "react";

function fmt(value) {
  return value === null || value === undefined || value === "" ? "--" : String(value);
}

function money(value) {
  const num = Number(value);
  return Number.isFinite(num) ? num.toFixed(2) : "--";
}

function mmss(seconds) {
  if (seconds === null || seconds === undefined || Number.isNaN(Number(seconds))) return "--";
  const value = Math.max(0, Math.floor(Number(seconds)));
  return `${String(Math.floor(value / 60)).padStart(2, "0")}:${String(value % 60).padStart(2, "0")}`;
}

function brTime(value) {
  if (!value) return "--";
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return "--";
  return date.toLocaleTimeString("pt-BR", {
    timeZone: "America/Sao_Paulo",
    hour: "2-digit",
    minute: "2-digit",
    second: "2-digit"
  });
}

function secondsLabel(value) {
  if (value === null || value === undefined || Number.isNaN(Number(value))) return "--";
  const seconds = Math.max(0, Number(value));
  return seconds < 60 ? `${seconds.toFixed(seconds < 10 ? 1 : 0)}s` : `${Math.floor(seconds / 60)}m ${Math.floor(seconds % 60)}s`;
}

function wsHealthText(ws) {
  if (!ws?.enabled) return "desligado";
  if (ws.last_error) return "erro";
  if (ws.last_message_age === null || ws.last_message_age === undefined) return "conectando";
  return Number(ws.last_message_age) > 15 ? "stale" : "ok";
}

function wsHealthClass(ws) {
  const status = wsHealthText(ws);
  return status === "ok" ? "ok" : status === "desligado" ? "" : "bad";
}

function levelText(level) {
  if (!level) return "--";
  return `${level.price} / ${level.size}`;
}

function opRealValue(op) {
  const value = op.gross_profit_final ?? op.gross_profit_worst ?? op.gross_profit_expected;
  const num = Number(value);
  return Number.isFinite(num) ? num : null;
}

function opRealLabel(op) {
  if (op.gross_profit_final !== undefined && op.gross_profit_final !== null) {
    if (op.final_status === "unwound_previsao") return "revendido na Previsao";
    if (op.hedge_overfilled || Number(op.extra_hedge_final_pnl || 0)) return "final capado";
    return "final";
  }
  if (op.hedge_underfilled) return "hedge parcial";
  if (op.hedge_overfilled) return "overhedge";
  return "aberto";
}

function outcomeText(value) {
  const label = String(value || "").toLowerCase();
  if (label === "up") return "SOBE";
  if (label === "down") return "DESCE";
  return fmt(value);
}

function signedUsdc(value) {
  const num = Number(value);
  if (!Number.isFinite(num)) return "--";
  return `${num >= 0 ? "+" : ""}${num.toFixed(2)} USDC`;
}

function operationFlow(op) {
  const previsaoCost = money(op.previsao_cost);
  const polyCost = money(op.polymarket_cost_real || op.polymarket_cost_max);
  if (op.final_status === "unwound_previsao") {
    return {
      previsao: `Comprou ${previsaoCost} USDC de ${outcomeText(op.previsao)} e vendeu de volta por ${money(op.previsao_unwind_proceeds)} USDC`,
      polymarket: "Não comprou na Polymarket, porque ficou abaixo do mínimo de 1 USDC",
    };
  }
  return {
    previsao: `Comprou ${previsaoCost} USDC de ${outcomeText(op.previsao)}`,
    polymarket: `Comprou ${polyCost} USDC de ${outcomeText(op.polymarket)}`,
  };
}

function Books({ title, books }) {
  return (
    <section className="panel span-6">
      <div className="kicker">{title}</div>
      <table>
        <thead>
          <tr><th>Lado</th><th>Compram</th><th>Vendem</th></tr>
        </thead>
        <tbody>
          {["up", "down"].map((outcome) => {
            const book = books?.[outcome] || {};
            return (
              <tr key={outcome}>
                <td><span className="pill">{outcome === "up" ? "SOBE" : "DESCE"}</span></td>
                <td>{levelText(book.bid)}</td>
                <td>{levelText(book.ask)}</td>
              </tr>
            );
          })}
        </tbody>
      </table>
    </section>
  );
}

export default function Page() {
  const [password, setPassword] = useState("");
  const [authenticated, setAuthenticated] = useState(false);
  const [snapshot, setSnapshot] = useState(null);
  const [error, setError] = useState("");
  const [margin, setMargin] = useState("15");
  const [maxOrder, setMaxOrder] = useState("2");
  const [minSeconds, setMinSeconds] = useState("20");
  const [draftMargin, setDraftMargin] = useState("15");
  const [draftMaxOrder, setDraftMaxOrder] = useState("2");
  const [draftMinSeconds, setDraftMinSeconds] = useState("20");
  const [configTouched, setConfigTouched] = useState(false);
  const [configStatus, setConfigStatus] = useState("");
  const [botEnabled, setBotEnabled] = useState(true);
  const [now, setNow] = useState(Date.now());
  const configTouchedRef = useRef(false);

  function touchConfig() {
    configTouchedRef.current = true;
    setConfigTouched(true);
  }

  async function login(event) {
    event.preventDefault();
    setError("");
    const res = await fetch("/api/login", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ password })
    });
    if (!res.ok) {
      setError("Senha errada.");
      return;
    }
    setAuthenticated(true);
  }

  async function load() {
    const res = await fetch("/api/snapshot", { cache: "no-store" });
    if (res.status === 401) {
      setAuthenticated(false);
      return;
    }
    const data = await res.json();
    setSnapshot(data);
    if (data.account?.config && !configTouchedRef.current) {
      const config = data.account.config;
      setMargin(String(config.margin_pct ?? 15));
      setMaxOrder(String(config.max_order_usdc ?? 2));
      setMinSeconds(String(config.min_seconds_left ?? 20));
      setDraftMargin(String(config.margin_pct ?? 15));
      setDraftMaxOrder(String(config.max_order_usdc ?? 2));
      setDraftMinSeconds(String(config.min_seconds_left ?? 20));
      setBotEnabled(config.bot_enabled !== false);
    }
    setAuthenticated(true);
  }

  useEffect(() => {
    load().catch(() => setAuthenticated(false));
  }, []);

  useEffect(() => {
    if (!authenticated) return;
    const timer = setInterval(() => load().catch((err) => setError(err.message)), 2000);
    return () => clearInterval(timer);
  }, [authenticated]);

  useEffect(() => {
    const timer = setInterval(() => setNow(Date.now()), 1000);
    return () => clearInterval(timer);
  }, []);

  async function confirmControls() {
    setConfigStatus("Salvando...");
    const res = await fetch("/api/config", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        margin_pct: draftMargin || "15",
        max_order_usdc: draftMaxOrder || "2",
        min_seconds_left: draftMinSeconds || "20",
        bot_enabled: botEnabled
      })
    });
    if (!res.ok) {
      setConfigStatus("Erro ao salvar");
      return;
    }
    const data = await res.json();
    const config = data.config || {};
    setMargin(String(config.margin_pct ?? draftMargin ?? 15));
    setMaxOrder(String(config.max_order_usdc ?? draftMaxOrder ?? 2));
    setMinSeconds(String(config.min_seconds_left ?? draftMinSeconds ?? 20));
    setDraftMargin(String(config.margin_pct ?? draftMargin ?? 15));
    setDraftMaxOrder(String(config.max_order_usdc ?? draftMaxOrder ?? 2));
    setDraftMinSeconds(String(config.min_seconds_left ?? draftMinSeconds ?? 20));
    setBotEnabled(config.bot_enabled !== false);
    configTouchedRef.current = false;
    setConfigTouched(false);
    setConfigStatus("Confirmado");
  }

  async function toggleBot() {
    const next = !botEnabled;
    setBotEnabled(next);
    setConfigStatus(next ? "Ligando..." : "Pausando...");
    const res = await fetch("/api/config", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        margin_pct: margin,
        max_order_usdc: maxOrder,
        min_seconds_left: minSeconds,
        bot_enabled: next
      })
    });
    if (!res.ok) {
      setBotEnabled(!next);
      setConfigStatus("Erro ao mudar bot");
      return;
    }
    const data = await res.json();
    setBotEnabled(data.config?.bot_enabled !== false);
    setConfigStatus(data.config?.bot_enabled === false ? "Bot pausado" : "Bot ligado");
  }

  const account = snapshot?.account || {};
  const market = snapshot?.market || {};
  const pre = market.previsao || {};
  const poly = market.polymarket || {};
  const balance = Array.isArray(account.balance) ? account.balance.find((row) => row.currency === "USDC") : null;
  const polyAccount = account.polymarket || {};
  const orders = account.open_orders || [];
  const operations = account.operations || [];
  const health = account.health || {};
  const ws = health.ws || {};
  const userEvents = account.polymarket_user_events || [];
  const lastPolyUserEvent = userEvents.length ? userEvents[userEvents.length - 1] : null;
  const secondsLeft = pre.closesAt ? Math.floor((Date.parse(pre.closesAt) - now) / 1000) : market.seconds_left;
  const canQuote = Number(secondsLeft) > Number(minSeconds || 20);
  const openOrderCost = useMemo(() => {
    return orders.reduce((sum, order) => sum + Number(order.price || 0) * Number(order.amountRemaining ?? order.amount ?? 0), 0);
  }, [orders]);
  const pnl = useMemo(() => {
    return operations.reduce((summary, op) => {
      const real = opRealValue(op);
      if (real === null) return summary;
      summary.total += real;
      summary.count += 1;
      if (op.gross_profit_final !== undefined && op.gross_profit_final !== null) {
        summary.final += real;
        summary.finalCount += 1;
      } else {
        summary.open += real;
        summary.openCount += 1;
      }
      return summary;
    }, { total: 0, final: 0, open: 0, count: 0, finalCount: 0, openCount: 0 });
  }, [operations]);

  if (!authenticated) {
    return (
      <main className="login">
        <form className="login-card" onSubmit={login}>
          <h1>Bot BTC 5 min</h1>
          <p>Painel privado.</p>
          <input
            autoFocus
            type="password"
            value={password}
            onChange={(event) => setPassword(event.target.value)}
            placeholder="Senha"
          />
          <button type="submit">Entrar</button>
          {error ? <div className="bad">{error}</div> : null}
        </form>
      </main>
    );
  }

  return (
    <>
      <header>
        <div className="topbar">
          <div>
            <h1>Bot BTC 5 min</h1>
            <div className="small">Atualiza sozinho a cada 2 segundos</div>
          </div>
          <div className="controls">
            <label>Desconto % <input value={draftMargin} onChange={(e) => { touchConfig(); setDraftMargin(e.target.value); }} /></label>
            <label>Máx por aposta <input value={draftMaxOrder} onChange={(e) => { touchConfig(); setDraftMaxOrder(e.target.value); }} /></label>
            <label>Parar faltando <input type="number" min="20" value={draftMinSeconds} onChange={(e) => { touchConfig(); setDraftMinSeconds(e.target.value); }} /></label>
            <button onClick={confirmControls}>Confirmar</button>
            <button className={botEnabled ? "danger" : ""} onClick={toggleBot}>{botEnabled ? "Pausar bot" : "Ligar bot"}</button>
            <span className="small">{configStatus}</span>
          </div>
        </div>
      </header>

      <main className="dashboard">
        <section className="panel">
          <div className="market-line">
            <div>
              <div className="kicker">Mercado</div>
              <b>{pre.title || poly.question || "Bitcoin 5 min"}</b>
              <div className="small">
                {pre.url ? <a href={pre.url} target="_blank">Abrir Previsao</a> : null}
                {pre.url && poly.url ? " · " : ""}
                {poly.url ? <a href={poly.url} target="_blank">Abrir Polymarket</a> : null}
              </div>
            </div>
            <div className="small">{brTime(snapshot?.generated_at)}</div>
          </div>
        </section>

        <section className="grid">
          <div className="panel span-4"><div className="kicker">Preço inicial</div><div className="value">{fmt(pre.initialPrice)}</div></div>
          <div className="panel span-4"><div className="kicker">Preço atual</div><div className="value">{fmt(pre.currentPrice)}</div></div>
          <div className="panel span-4"><div className="kicker">Min / seg</div><div className="value">{mmss(secondsLeft)}</div><div className="small">{botEnabled ? (canQuote ? "Bot pode colocar ordem" : "Bot parou esta rodada") : "Bot pausado"}</div></div>
        </section>

        <section className="grid">
          <div className="panel span-3"><div className="kicker">Saldo Previsao</div><div className="value">{balance ? money(balance.amount) : "--"}</div><div className="small">USDC livre</div></div>
          <div className="panel span-3"><div className="kicker">Saldo Polymarket</div><div className="value">{polyAccount.balance_usdc ? `${polyAccount.balance_usdc}` : "--"}</div><div className="small">USDC</div></div>
          <div className="panel span-6"><div className="kicker">Ordens</div><div className="value">{orders.length}</div><div className="small">abertas na Previsao · {money(openOrderCost)} USDC travado</div></div>
        </section>

        <section className="grid">
          <div className="panel span-3">
            <div className="kicker">Previsao WS</div>
            <div className={`value ${wsHealthClass(ws.previsao_ws)}`}>{wsHealthText(ws.previsao_ws)}</div>
            <div className="small">msg há {secondsLabel(ws.previsao_ws?.last_message_age)} · trades {fmt(ws.previsao_ws?.hedge_triggers)}</div>
          </div>
          <div className="panel span-3">
            <div className="kicker">Poly market WS</div>
            <div className={`value ${wsHealthClass(ws.polymarket_market_ws)}`}>{wsHealthText(ws.polymarket_market_ws)}</div>
            <div className="small">msg há {secondsLabel(ws.polymarket_market_ws?.last_message_age)} · books {fmt(ws.polymarket_market_ws?.books)}</div>
          </div>
          <div className="panel span-3">
            <div className="kicker">Poly user WS</div>
            <div className={`value ${wsHealthClass(ws.polymarket_user_ws)}`}>{wsHealthText(ws.polymarket_user_ws)}</div>
            <div className="small">ordem há {secondsLabel(ws.polymarket_user_ws?.last_order_event_age)} · trade há {secondsLabel(ws.polymarket_user_ws?.last_trade_event_age)}</div>
          </div>
          <div className="panel span-3">
            <div className="kicker">Hedge</div>
            <div className={Number(health.pending_hedge_count || 0) ? "value bad" : "value ok"}>{fmt(health.pending_hedge_count ?? account.pending_hedges?.length ?? 0)}</div>
            <div className="small">último {health.last_hedge ? `${brTime(health.last_hedge.hedged_at)} · ${fmt(health.last_hedge.gross_profit_worst)} USDC` : "sem hedge"}</div>
          </div>
        </section>

        <section className="panel">
          <div className="market-line">
            <div>
              <div className="kicker">Último evento Polymarket</div>
              <b>{lastPolyUserEvent ? `${fmt(lastPolyUserEvent.event_type)} · ${fmt(lastPolyUserEvent.status || lastPolyUserEvent.type)}` : "Sem evento user WS persistido"}</b>
              <div className="small">
                {lastPolyUserEvent ? `${brTime(lastPolyUserEvent.received_at)} · ordem ${fmt(lastPolyUserEvent.order_id || lastPolyUserEvent.id || lastPolyUserEvent.taker_order_id)} · ${fmt(lastPolyUserEvent.size || lastPolyUserEvent.size_matched)} @ ${fmt(lastPolyUserEvent.price)}` : "Aguardando ordem/trade do canal privado"}
              </div>
            </div>
          </div>
        </section>

        <section className="grid">
          <Books title="Compra/venda Previsao" books={market.books?.previsao} />
          <Books title="Compra/venda Polymarket" books={market.books?.polymarket} />
        </section>

        <section className="panel">
          <div className="kicker">Ordens abertas</div>
          <table>
            <thead><tr><th>ID</th><th>Lado</th><th>Preço</th><th>Falta</th></tr></thead>
            <tbody>
              {orders.length ? orders.map((order) => (
                <tr key={order.id}>
                  <td>{fmt(order.id)}</td>
                  <td>{fmt(order.side)} {fmt(order.selectionId)}</td>
                  <td>{fmt(order.price)}</td>
                  <td>{fmt(order.amountRemaining ?? order.amount)}</td>
                </tr>
              )) : <tr><td colSpan="4" className="subtle">Nenhuma ordem aberta.</td></tr>}
            </tbody>
          </table>
        </section>

        <section className="panel">
          <div className="kicker">Lucro/prejuízo</div>
          <div className="value">{operations.length ? `${operations.length} operações` : "Aguardando operação"}</div>
          <div className="small">esperado vem da cotação; real final aparece depois que a rodada resolve</div>
          <div className="pnl-grid">
            <div className="pnl-box">
              <div className="small">Consolidado</div>
              <b className={pnl.total >= 0 ? "ok" : "bad"}>{pnl.count ? `${pnl.total >= 0 ? "+" : ""}${pnl.total.toFixed(2)} USDC` : "--"}</b>
            </div>
            <div className="pnl-box">
              <div className="small">Finalizado</div>
              <b className={pnl.final >= 0 ? "ok" : "bad"}>{pnl.finalCount ? `${pnl.final >= 0 ? "+" : ""}${pnl.final.toFixed(2)} USDC` : "--"}</b>
            </div>
            <div className="pnl-box">
              <div className="small">Aberto</div>
              <b className={pnl.open >= 0 ? "ok" : "bad"}>{pnl.openCount ? `${pnl.open >= 0 ? "+" : ""}${pnl.open.toFixed(2)} USDC` : "--"}</b>
            </div>
          </div>
          <table className="ops-table">
            <thead><tr><th>Hora</th><th>O que aconteceu</th><th>Resultado</th></tr></thead>
            <tbody>
              {operations.length ? operations.slice(0, 20).map((op) => {
                const real = opRealValue(op);
                const flow = operationFlow(op);
                return (
                  <tr key={op.trade_id}>
                    <td>{brTime(op.hedged_at)}</td>
                    <td>
                      <div className="op-flow">
                        <div><span>Previsao</span>{flow.previsao}</div>
                        <div><span>Polymarket</span>{flow.polymarket}</div>
                      </div>
                      <div className="small">
                        {fmt(op.shares)} cotas · Previsao {outcomeText(op.previsao)} @ {fmt(op.previsao_price)}
                        {op.final_status === "unwound_previsao" ? "" : ` · Polymarket ${outcomeText(op.polymarket)} até ${fmt(op.polymarket_max_price)}`}
                      </div>
                    </td>
                    <td>
                      <div className="result-lines">
                        <div>
                          <span>Esperado</span>
                          <b className={Number(op.gross_profit_expected) >= 0 ? "ok" : "bad"}>{signedUsdc(op.gross_profit_expected)}</b>
                        </div>
                        <div>
                          <span>Real</span>
                          <b className={real !== null && real >= 0 ? "ok" : "bad"}>{real === null ? "--" : signedUsdc(real)}</b>
                        </div>
                      </div>
                      <div className="small">{opRealLabel(op)}</div>
                      {op.gross_profit_final_uncapped !== undefined && op.gross_profit_final_uncapped !== null && Number(op.gross_profit_final_uncapped) !== real ? (
                        <div className="small">bruto {fmt(op.gross_profit_final_uncapped)} USDC</div>
                      ) : null}
                    </td>
                  </tr>
                );
              }) : <tr><td colSpan="3" className="subtle">Ainda não tem operação com hedge neste servidor.</td></tr>}
            </tbody>
          </table>
        </section>
      </main>
    </>
  );
}
