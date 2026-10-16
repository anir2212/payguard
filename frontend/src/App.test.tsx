// @vitest-environment jsdom
import { afterEach, expect, test, vi } from 'vitest'
import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import App from './App'

afterEach(() => {
  cleanup()
  vi.unstubAllGlobals()
})

test('analyst can inspect evidence, draft a cited case, and record review without a card action', async () => {
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
    if (path.endsWith('/cases') && !init?.method) return Response.json([{
      ...caseRow, status: reviewed ? 'APPROVED' : 'OPEN', draft: drafted ? draft : null,
    }])
    if (path.endsWith('/case_1') && !init?.method) return Response.json({
      ...caseRow, status: reviewed ? 'APPROVED' : 'OPEN', draft: drafted ? draft : null,
      cardHistory: [evidence], reasonCodes: ['count_1m'],
      merchantProfile: { risk: 0.8, observedAuths: 1 }, linkedCards: [], similarCases: [],
    })
    if (path.endsWith('/case_1/draft') && init?.method === 'POST') {
      drafted = true
      return Response.json(draft)
    }
    if (path.endsWith('/case_1/review') && init?.method === 'POST') {
      expect(JSON.parse(String(init.body))).toMatchObject({ decision: 'APPROVED', analyst: 'local_analyst' })
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
  fireEvent.click(screen.getByRole('button', { name: 'Approve recommendation' }))
  await waitFor(() => expect(paths).toContain('POST /api/cases/cases/case_1/review'))
  await waitFor(() => expect(screen.queryByRole('button', { name: 'Approve recommendation' })).toBeNull())
  expect(paths.every(path => !path.includes('/cards/'))).toBe(true)
})
