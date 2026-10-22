export type AuthEvent = {
  request: { authId: string; cardId: string; merchantId: string; amountMinor: number; currency: string }
  result: { status: string; modelScore?: number | null; modelAction?: string | null; reasons: string[] }
  timestamp: string
}

export type CaseRow = {
  case_id: string; auth_id: string; card_id: string; merchant_id: string
  status: string; draft: string | Draft | null; analyst: string | null
  analyst_note: string | null; created_at: string
}

export type Evidence = {
  transactionId: string; amountMinor: number; deviceId: string; status: string; eventAt: string
}

export type Draft = {
  caseId: string; pattern: string; recommendedAction: string; confidence: number
  claims: { text: string; citations: string[] }[]
  summary: string; draftSource: string; approvalStatus: string
}

export type Review = { decision: string; analyst: string; role: string; note: string; reviewed_at: string }

export type CaseDetail = CaseRow & {
  cardHistory: Evidence[]; reasonCodes: string[]; merchantProfile: { risk: number; observedAuths: number }
  linkedCards: string[]; similarCases: string[]; draft: Draft | null; reviews: Review[]
}

export type Analyst = { name: string; role: string; permissions: string[]; mode: string }

export type SettlementReport = {
  settlementDate: string; matched: number; missingNetwork: number; missingLedger: number
  amountMismatch: number; duplicate: number
  items: { captureId: string; status: string; expectedMinor?: number; observedMinor?: number }[]
  journalIntegrity: JournalIntegrityReport
}

export type JournalIntegrityReport = {
  settlementDate: string; capturesChecked: number; posted: number; missingJournal: number
  duplicateJournal: number; wrongCurrencyJournal: number; journalAmountMismatch: number
  items: { captureId: string; status: string; captureMinor: number; captureCurrency: string
    journalCount: number; postedMinor: number; postedCurrencies: string[] }[]
}

async function request<T>(path: string, init?: RequestInit): Promise<T> {
  const response = await fetch(path, init)
  if (!response.ok) {
    let detail = `${response.status} ${response.statusText}`
    try {
      const body = await response.json()
      detail = body.error ?? body.detail ?? detail
    } catch { /* plain-text error */ }
    throw new Error(detail)
  }
  return response.json() as Promise<T>
}

export const api = {
  events: () => request<AuthEvent[]>('/api/payments/v1/authorizations/recent'),
  me: () => request<Analyst>('/api/cases/me'),
  cases: () => request<CaseRow[]>('/api/cases/cases'),
  case: (id: string) => request<CaseDetail>(`/api/cases/cases/${encodeURIComponent(id)}`),
  draft: (id: string) => request<Draft>(`/api/cases/cases/${encodeURIComponent(id)}/draft`, { method: 'POST' }),
  // The analyst's identity comes from the server (proxy headers or local mode), never the body.
  review: (id: string, decision: 'APPROVED' | 'REJECTED', note: string) =>
    request<{ status: string; analyst: string }>(`/api/cases/cases/${encodeURIComponent(id)}/review`, {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ decision, note }),
    }),
  sample: async (date: string) => {
    const response = await fetch(`/api/payments/v1/settlements/sample?date=${date}`)
    if (!response.ok) throw new Error(`${response.status} ${response.statusText}`)
    return response.text()
  },
  reconcile: (date: string, csv: string) => request<SettlementReport>(
    `/api/payments/v1/settlements/reconcile?date=${date}`,
    { method: 'POST', headers: { 'Content-Type': 'text/csv' }, body: csv },
  ),
}
