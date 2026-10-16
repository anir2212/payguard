import { useCallback, useEffect, useMemo, useState } from 'react'
import { api, type AuthEvent, type CaseDetail, type CaseRow, type SettlementReport } from './api'

type Tab = 'overview' | 'cases' | 'transactions' | 'settlement'

const nav: { id: Tab; label: string; glyph: string }[] = [
  { id: 'overview', label: 'Overview', glyph: '◫' },
  { id: 'cases', label: 'Case queue', glyph: '◎' },
  { id: 'transactions', label: 'Authorizations', glyph: '⇄' },
  { id: 'settlement', label: 'Settlement', glyph: '≡' },
]

function formatMoney(value: number | undefined, currency = 'SGD') {
  if (value === undefined) return '—'
  return new Intl.NumberFormat('en-SG', { style: 'currency', currency }).format(value / 100)
}

function formatTime(value: string) {
  return new Date(value).toLocaleString('en-SG', { dateStyle: 'medium', timeStyle: 'short' })
}

function statusClass(status: string) {
  return status === 'APPROVED' || status === 'MATCHED' ? 'good' :
    status === 'OPEN' || status === 'PENDING_ANALYST' ? 'warn' : 'bad'
}

export default function App() {
  const [tab, setTab] = useState<Tab>('overview')
  const [events, setEvents] = useState<AuthEvent[]>([])
  const [cases, setCases] = useState<CaseRow[]>([])
  const [selectedId, setSelectedId] = useState<string | null>(null)
  const [detail, setDetail] = useState<CaseDetail | null>(null)
  const [filter, setFilter] = useState('')
  const [analyst, setAnalyst] = useState('local_analyst')
  const [note, setNote] = useState('')
  const [error, setError] = useState('')
  const [busy, setBusy] = useState(false)
  const [lastUpdated, setLastUpdated] = useState<Date | null>(null)
  const [date, setDate] = useState(new Date().toISOString().slice(0, 10))
  const [csv, setCsv] = useState('')
  const [settlement, setSettlement] = useState<SettlementReport | null>(null)

  const refresh = useCallback(async () => {
    try {
      const [nextEvents, nextCases] = await Promise.all([api.events(), api.cases()])
      setEvents(nextEvents)
      setCases(nextCases)
      setLastUpdated(new Date())
      setError('')
    } catch (cause) { setError(cause instanceof Error ? cause.message : String(cause)) }
  }, [])

  useEffect(() => {
    void refresh()
    const timer = window.setInterval(() => { void refresh() }, 5000)
    return () => window.clearInterval(timer)
  }, [refresh])

  const openCase = useCallback(async (id: string) => {
    setSelectedId(id)
    setTab('cases')
    try { setDetail(await api.case(id)); setError('') }
    catch (cause) { setError(cause instanceof Error ? cause.message : String(cause)) }
  }, [])

  async function draftCase() {
    if (!selectedId) return
    setBusy(true)
    try { await api.draft(selectedId); await openCase(selectedId); await refresh() }
    catch (cause) { setError(cause instanceof Error ? cause.message : String(cause)) }
    finally { setBusy(false) }
  }

  async function review(decision: 'APPROVED' | 'REJECTED') {
    if (!selectedId || !analyst.trim()) return
    setBusy(true)
    try {
      await api.review(selectedId, decision, analyst.trim(), note)
      await openCase(selectedId)
      await refresh()
    } catch (cause) { setError(cause instanceof Error ? cause.message : String(cause)) }
    finally { setBusy(false) }
  }

  const visibleEvents = useMemo(() => events.filter(event =>
    [event.request.authId, event.request.cardId, event.request.merchantId].some(value =>
      value.toLowerCase().includes(filter.toLowerCase())),
  ), [events, filter])
  const declines = events.filter(event => event.result.status === 'DECLINED').length
  const pending = cases.filter(row => row.status === 'OPEN').length
  const modelDeclines = events.filter(event => event.result.modelAction === 'MODEL_DECLINE').length

  function jumpToTransaction(id: string) { setFilter(id); setTab('transactions') }

  async function loadSample() {
    setBusy(true)
    try { setCsv(await api.sample(date)); setSettlement(null); setError('') }
    catch (cause) { setError(cause instanceof Error ? cause.message : String(cause)) }
    finally { setBusy(false) }
  }

  async function reconcile() {
    setBusy(true)
    try { setSettlement(await api.reconcile(date, csv)); setError('') }
    catch (cause) { setError(cause instanceof Error ? cause.message : String(cause)) }
    finally { setBusy(false) }
  }

  return <div className="shell">
    <aside className="sidebar">
      <div className="brand"><div className="brand-icon">P<span>G</span></div><div><strong>PayGuard</strong><small>ANALYST WORKSPACE</small></div></div>
      <div className="workspace-label">WORKSPACE</div>
      <nav aria-label="Primary navigation">{nav.map(item =>
        <button key={item.id} className={`nav-item ${tab === item.id ? 'active' : ''}`} onClick={() => setTab(item.id)}>
          <span className="nav-glyph">{item.glyph}</span>{item.label}
          {item.id === 'cases' && pending > 0 && <b className="nav-count">{pending}</b>}
        </button>,
      )}</nav>
      <div className="sidebar-bottom"><div className="live-dot" /> LOCAL DEMO <span>·</span> SYNTHETIC DATA</div>
    </aside>

    <main className="main">
      <header className="topbar"><div className="breadcrumb">PAYGUARD <span>/</span> {nav.find(item => item.id === tab)?.label.toUpperCase()}</div>
        <div className="top-actions"><span className="updated">{lastUpdated ? `Updated ${lastUpdated.toLocaleTimeString()}` : 'Connecting…'}</span><button className="refresh" onClick={() => void refresh()}>↻ Refresh</button><div className="avatar">A</div></div>
      </header>
      {error && <div className="error" role="alert">{error}</div>}

      {tab === 'overview' && <div className="content">
        <div className="page-head"><div><p className="eyebrow">RISK OPERATIONS / LIVE VIEW</p><h1>Good visibility starts here.</h1><p className="subtle">Monitor authorizations, review cited investigations, and verify settlement.</p></div><div className="date-chip">◷ &nbsp; {new Date().toLocaleDateString('en-SG', { weekday: 'short', day: 'numeric', month: 'long', year: 'numeric' })}</div></div>
        <div className="stats">
          <div className="stat"><span className="stat-icon mint">⇄</span><p>RECENT AUTHORIZATIONS</p><strong>{events.length}</strong><small>latest 100 events</small></div>
          <div className="stat"><span className="stat-icon rose">⊘</span><p>DECLINED</p><strong>{declines}</strong><small>{events.length ? `${Math.round(declines / events.length * 100)}% of visible events` : 'No events yet'}</small></div>
          <div className="stat"><span className="stat-icon amber">◎</span><p>OPEN CASES</p><strong>{pending}</strong><small>awaiting analyst review</small></div>
          <div className="stat"><span className="stat-icon blue">✦</span><p>MODEL DECLINES</p><strong>{modelDeclines}</strong><small>enforce-mode decisions</small></div>
        </div>
        <div className="two-columns">
          <section className="panel"><div className="panel-head"><div><p className="eyebrow">ACTIVITY</p><h2>Recent authorizations</h2></div><button className="text-button" onClick={() => setTab('transactions')}>View all →</button></div>
            <div className="compact-list">{events.slice(0, 6).map(event => <div className="activity" key={event.request.authId}><div className={`activity-icon ${statusClass(event.result.status)}`}>{event.result.status === 'APPROVED' ? '✓' : '!'}</div><div className="activity-main"><strong>{event.request.merchantId}</strong><small>{event.request.authId}</small></div><div className="activity-end"><strong>{formatMoney(event.request.amountMinor, event.request.currency)}</strong><span className={`pill ${statusClass(event.result.status)}`}>{event.result.status}</span></div></div>)}{!events.length && <p className="empty">No authorization events yet.</p>}</div>
          </section>
          <section className="panel"><div className="panel-head"><div><p className="eyebrow">INVESTIGATIONS</p><h2>Case queue</h2></div><button className="text-button" onClick={() => setTab('cases')}>Open queue →</button></div>
            <div className="compact-list">{cases.slice(0, 6).map(row => <button className="case-preview" key={row.case_id} onClick={() => void openCase(row.case_id)}><div className="case-mark">◎</div><div className="activity-main"><strong>{row.case_id}</strong><small>{row.merchant_id} · {formatTime(row.created_at)}</small></div><span className={`pill ${statusClass(row.status)}`}>{row.status}</span></button>)}{!cases.length && <p className="empty">No cases opened yet.</p>}</div>
          </section>
        </div>
        <div className="model-banner"><div><p className="eyebrow">MEASURED MODEL SNAPSHOT</p><h2>Synthetic LightGBM</h2><p>PR-AUC <strong>0.9574</strong> · amount-weighted recall <strong>93.00%</strong> · false-positive rate <strong>0.319%</strong> at the validation-selected cost threshold.</p><small>Held-out synthetic test; these values do not predict real-world fraud performance. See RESULTS.md.</small></div><div className="model-symbol">✳</div></div>
      </div>}

      {tab === 'cases' && <div className="content"><div className="page-head"><div><p className="eyebrow">HUMAN-IN-THE-LOOP</p><h1>Investigation queue</h1><p className="subtle">Review evidence and choose whether to accept each recommendation.</p></div><span className="summary-chip">{pending} pending</span></div>
        <div className="case-layout"><section className="panel queue"><div className="panel-head"><h2>Cases</h2><span className="count">{cases.length}</span></div><div className="queue-list">{cases.map(row => <button key={row.case_id} onClick={() => void openCase(row.case_id)} className={`queue-row ${selectedId === row.case_id ? 'selected' : ''}`}><div><strong>{row.case_id}</strong><small>{row.merchant_id}</small><small>{formatTime(row.created_at)}</small></div><span className={`pill ${statusClass(row.status)}`}>{row.status}</span></button>)}{!cases.length && <p className="empty">High-scoring events will appear here.</p>}</div></section>
          <section className="panel case-detail">{detail ? <><div className="panel-head"><div><p className="eyebrow">CASE DETAIL</p><h2>{detail.case_id}</h2></div><span className={`pill ${statusClass(detail.status)}`}>{detail.status}</span></div>
            <div className="detail-grid"><div><small>CARD</small><strong>{detail.card_id}</strong></div><div><small>MERCHANT</small><strong>{detail.merchant_id}</strong></div><div><small>OBSERVED AUTHS</small><strong>{detail.cardHistory.length}</strong></div><div><small>MERCHANT RISK HINT</small><strong>{detail.merchantProfile.risk.toFixed(2)}</strong></div></div>
            <div className="section-title">Model reason codes</div><div className="tags">{detail.reasonCodes.length ? detail.reasonCodes.map(code => <span className="tag" key={code}>{code}</span>) : <span className="muted">No model codes</span>}</div>
            <div className="section-title">Recent evidence</div><div className="evidence-list">{detail.cardHistory.map(row => <button key={row.transactionId} onClick={() => jumpToTransaction(row.transactionId)}><span>{row.transactionId}</span><b>{formatMoney(row.amountMinor)}</b><small>{row.status}</small></button>)}</div>
            {detail.draft ? <div className="draft-box"><div className="draft-head"><div><p className="eyebrow">AGENT DRAFT · {detail.draft.draftSource.toUpperCase()}</p><h3>{detail.draft.pattern.replaceAll('_', ' ')}</h3></div><span className="confidence">{Math.round(detail.draft.confidence * 100)}% confidence</span></div><p>{detail.draft.summary}</p>{detail.draft.claims.map((claim, index) => <div className="claim" key={index}>{claim.text}<div>{claim.citations.map(id => <button key={id} onClick={() => jumpToTransaction(id)}>{id}</button>)}</div></div>)}<div className="recommendation"><span>RECOMMENDED ACTION</span><strong>{detail.draft.recommendedAction.replaceAll('_', ' ')}</strong></div></div> : <button className="primary" disabled={busy} onClick={() => void draftCase()}>Generate cited draft</button>}
            {detail.draft && detail.status === 'OPEN' && <div className="review-box"><p>Record your review. This console does not execute a freeze or contact the customer.</p><div className="review-inputs"><input aria-label="Analyst" value={analyst} onChange={event => setAnalyst(event.target.value)} placeholder="Analyst name" /><input aria-label="Review note" value={note} onChange={event => setNote(event.target.value)} placeholder="Review note" /></div><div className="review-actions"><button className="primary" disabled={busy || !analyst.trim()} onClick={() => void review('APPROVED')}>Approve recommendation</button><button className="secondary" disabled={busy || !analyst.trim()} onClick={() => void review('REJECTED')}>Reject</button></div></div>}
          </> : <div className="empty-state"><span>◎</span><h2>Select a case</h2><p>Open a case from the queue to inspect its evidence and review a draft.</p></div>}</section></div>
      </div>}

      {tab === 'transactions' && <div className="content"><div className="page-head"><div><p className="eyebrow">PAYMENT DECISIONS</p><h1>Authorizations</h1><p className="subtle">The latest events committed to the PostgreSQL outbox.</p></div><span className="summary-chip">{events.length} visible</span></div><section className="panel"><div className="table-tools"><h2>Event stream</h2><input className="search" aria-label="Filter authorizations" placeholder="Search auth, card, merchant…" value={filter} onChange={event => setFilter(event.target.value)} /></div><div className="table-wrap"><table><thead><tr><th>TIME</th><th>AUTHORIZATION</th><th>MERCHANT</th><th>AMOUNT</th><th>MODEL SCORE</th><th>DECISION</th></tr></thead><tbody>{visibleEvents.map(event => <tr key={event.request.authId}><td>{formatTime(event.timestamp)}</td><td className="mono">{event.request.authId}</td><td>{event.request.merchantId}</td><td>{formatMoney(event.request.amountMinor, event.request.currency)}</td><td>{event.result.modelScore == null ? '—' : event.result.modelScore.toFixed(3)}</td><td><span className={`pill ${statusClass(event.result.status)}`}>{event.result.status}</span><small className="mode">{event.result.modelAction}</small></td></tr>)}</tbody></table>{!visibleEvents.length && <p className="empty">No matching events.</p>}</div></section></div>}

      {tab === 'settlement' && <div className="content"><div className="page-head"><div><p className="eyebrow">LEDGER CONTROLS</p><h1>Settlement reconciliation</h1><p className="subtle">Compare a synthetic network file with captured billing amounts for a UTC day.</p></div></div><section className="panel settlement-panel"><div className="panel-head"><h2>Network file</h2><div className="settlement-controls"><input type="date" aria-label="Settlement date" value={date} onChange={event => setDate(event.target.value)} /><button className="secondary" disabled={busy} onClick={() => void loadSample()}>Load sample</button><button className="primary" disabled={busy || !csv.trim()} onClick={() => void reconcile()}>Reconcile</button></div></div><p className="hint">CSV header: <code>capture_id,amount_minor,currency</code>. Edit the sample to explore duplicates, missing rows, and mismatches.</p><textarea aria-label="Network settlement CSV" value={csv} onChange={event => setCsv(event.target.value)} placeholder="capture_id,amount_minor,currency" spellCheck={false} />
          {settlement && <div className="settlement-results"><div className="result-stats"><div><strong>{settlement.matched}</strong><span>MATCHED</span></div><div><strong>{settlement.missingNetwork}</strong><span>MISSING NETWORK</span></div><div><strong>{settlement.missingLedger}</strong><span>MISSING LEDGER</span></div><div><strong>{settlement.amountMismatch}</strong><span>MISMATCH</span></div><div><strong>{settlement.duplicate}</strong><span>DUPLICATE</span></div></div><div className="table-wrap"><table><thead><tr><th>CAPTURE</th><th>STATUS</th><th>LEDGER</th><th>NETWORK</th></tr></thead><tbody>{settlement.items.map((item, index) => <tr key={`${item.captureId}-${index}`}><td className="mono">{item.captureId}</td><td><span className={`pill ${statusClass(item.status)}`}>{item.status}</span></td><td>{formatMoney(item.expectedMinor)}</td><td>{formatMoney(item.observedMinor)}</td></tr>)}</tbody></table></div></div>}
        </section></div>}
    </main>
  </div>
}
