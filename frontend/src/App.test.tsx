// @vitest-environment jsdom
import { afterEach, expect, test, vi } from 'vitest'
import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import App from './App'

afterEach(() => {
  cleanup()
  vi.unstubAllGlobals()
})

const LEAD = { name: 'lee', role: 'lead', permissions: ['audit', 'draft', 'review', 'view'], mode: 'proxy' }
const ANALYST = { name: 'ana', role: 'analyst', permissions: ['draft', 'view'], mode: 'proxy' }

test('lead can inspect evidence, draft a cited case, and record review without a card action', async () => {
  let drafted = false
  let reviewed = false
  const paths: string[] = []
  const evidence = {
    transactionId: 'auth_demo_1', amountMinor: 150, deviceId: 'new_device',
    status: 'DECLINED', eventAt: '2026-10-11T00:00:00Z',
  }
  const draft = {
    caseId: 'case_1', pattern: 'card_testing', recommendedAction: 'freeze_card',
    confidence: 0.9, claims: [{ text: 'Observed card testing signals.', citations: ['auth_demo_1'] }],
    summary: 'Observed card testing signals.', draftSource: 'ollama', approvalStatus: 'PENDING_ANALYST',
  }
  const caseRow = {
    case_id: 'case_1', auth_id: 'auth_demo_1', card_id: 'card_1', merchant_id: 'shop',
    status: reviewed ? 'APPROVED' : 'OPEN', draft: drafted ? draft : null,
    analyst: null, analyst_note: null, created_at: '2026-10-11T00:00:00Z',
  }
  vi.stubGlobal('fetch', vi.fn(async (input: string | URL | Request, init?: RequestInit) => {
    const path = String(input)
    paths.push(`${init?.method ?? 'GET'} ${path}`)
    if (path.endsWith('/authorizations/recent')) return Response.json([])
    if (path.endsWith('/api/cases/me')) return Response.json(LEAD)
    if (path.endsWith('/cases') && !init?.method) return Response.json([{
      ...caseRow, status: reviewed ? 'APPROVED' : 'OPEN', draft: drafted ? draft : null,
    }])
    if (path.endsWith('/case_1') && !init?.method) return Response.json({
      ...caseRow, status: reviewed ? 'APPROVED' : 'OPEN', draft: drafted ? draft : null,
      cardHistory: [evidence], reasonCodes: ['count_1m'],
      merchantProfile: { risk: 0.8, observedAuths: 1 }, linkedCards: [], similarCases: [],
      reviews: reviewed ? [{ decision: 'APPROVED', analyst: 'lee', role: 'lead', note: '', reviewed_at: '2026-10-11T00:05:00Z' }] : [],
    })
    if (path.endsWith('/case_1/draft') && init?.method === 'POST') {
      drafted = true
      return Response.json(draft)
    }
    if (path.endsWith('/case_1/review') && init?.method === 'POST') {
      expect(JSON.parse(String(init.body))).toEqual({ decision: 'APPROVED', note: '' })
      reviewed = true
      return Response.json({ status: 'APPROVED' })
    }
    throw new Error(`unexpected request ${path}`)
  }))

  render(<App />)
  fireEvent.click(await screen.findByRole('button', { name: /case_1/ }))
  expect(await screen.findByText('auth_demo_1')).toBeTruthy()
  fireEvent.click(screen.getByRole('button', { name: 'Generate cited draft' }))
  expect((await screen.findAllByText('Observed card testing signals.')).length).toBe(2)
  expect(screen.getByRole('button', { name: 'auth_demo_1' })).toBeTruthy()
  expect(await screen.findByText('Recording as lee (lead)')).toBeTruthy()
  fireEvent.click(screen.getByRole('button', { name: 'Approve recommendation' }))
  await waitFor(() => expect(paths).toContain('POST /api/cases/cases/case_1/review'))
  await waitFor(() => expect(screen.queryByRole('button', { name: 'Approve recommendation' })).toBeNull())
  expect((await screen.findByRole('list', { name: 'Review history' })).textContent).toContain('APPROVED by lee (lead)')
  expect(screen.queryByLabelText('Analyst')).toBeNull()
  expect(paths.every(path => !path.includes('/cards/'))).toBe(true)
})

test('analyst role can draft but the review buttons stay disabled', async () => {
  const draft = {
    caseId: 'case_2', pattern: 'unclassified', recommendedAction: 'contact_customer', confidence: 0.4,
    claims: [], summary: 'Needs review.', draftSource: 'rules_fallback', approvalStatus: 'PENDING_ANALYST',
  }
  const row = {
    case_id: 'case_2', auth_id: 'auth_2', card_id: 'card_2', merchant_id: 'shop', status: 'OPEN',
    draft, analyst: null, analyst_note: null, created_at: '2026-10-11T00:00:00Z',
  }
  const posts: string[] = []
  vi.stubGlobal('fetch', vi.fn(async (input: string | URL | Request, init?: RequestInit) => {
    const path = String(input)
    if (init?.method === 'POST') posts.push(path)
    if (path.endsWith('/authorizations/recent')) return Response.json([])
    if (path.endsWith('/api/cases/me')) return Response.json(ANALYST)
    if (path.endsWith('/cases')) return Response.json([row])
    if (path.endsWith('/case_2')) return Response.json({
      ...row, cardHistory: [], reasonCodes: [], merchantProfile: { risk: 0.1, observedAuths: 1 },
      linkedCards: [], similarCases: [], reviews: [],
    })
    throw new Error(`unexpected request ${path}`)
  }))

  render(<App />)
  fireEvent.click(await screen.findByRole('button', { name: /case_2/ }))
  expect(await screen.findByText('Recording as ana (analyst)')).toBeTruthy()
  expect(screen.getByText(/a lead must decide/)).toBeTruthy()
  const approve = screen.getByRole('button', { name: 'Approve recommendation' }) as HTMLButtonElement
  expect(approve.disabled).toBe(true)
  fireEvent.click(approve)
  expect(posts).toEqual([])
})

test('settlement view shows capture-to-journal discrepancies the network file cannot see', async () => {
  const report = {
    settlementDate: '2026-01-15', matched: 1, missingNetwork: 0, missingLedger: 0, amountMismatch: 0, duplicate: 0,
    items: [{ captureId: 'cap_ok', status: 'MATCHED', expectedMinor: 400, observedMinor: 400 }],
    journalIntegrity: {
      settlementDate: '2026-01-15', capturesChecked: 2, posted: 1, missingJournal: 1, duplicateJournal: 0,
      wrongCurrencyJournal: 0, journalAmountMismatch: 0,
      items: [
        { captureId: 'cap_ok', status: 'JOURNAL_POSTED', captureMinor: 400, captureCurrency: 'SGD', journalCount: 1, postedMinor: 400, postedCurrencies: ['SGD'] },
        { captureId: 'cap_lost', status: 'MISSING_JOURNAL', captureMinor: 500, captureCurrency: 'SGD', journalCount: 0, postedMinor: 0, postedCurrencies: [] },
      ],
    },
  }
  vi.stubGlobal('fetch', vi.fn(async (input: string | URL | Request, init?: RequestInit) => {
    const path = String(input)
    if (path.endsWith('/authorizations/recent') || path.endsWith('/cases')) return Response.json([])
    if (path.includes('/settlements/reconcile') && init?.method === 'POST') return Response.json(report)
    throw new Error(`unexpected request ${path}`)
  }))

  render(<App />)
  fireEvent.click(screen.getByRole('button', { name: /Settlement/ }))
  fireEvent.change(screen.getByLabelText('Network settlement CSV'), {
    target: { value: 'capture_id,amount_minor,currency\ncap_ok,400,SGD' },
  })
  fireEvent.click(screen.getByRole('button', { name: 'Reconcile' }))
  const audit = await screen.findByRole('table', { name: 'Journal audit discrepancies' })
  expect(audit.textContent).toContain('cap_lost')
  expect(audit.textContent).toContain('MISSING_JOURNAL')
  expect(audit.textContent).not.toContain('cap_ok')
})
