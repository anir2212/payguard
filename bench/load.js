// k6 constant-arrival-rate authorization workload; setup traffic is excluded
// from the custom auth_duration trend used in RESULTS.md.
import http from 'k6/http'
import exec from 'k6/execution'
import { Counter, Trend } from 'k6/metrics'

const rate = Number(__ENV.RATE || '100')
const duration = __ENV.DURATION || '15s'
const base = __ENV.API_URL || 'http://127.0.0.1:8086'
const cards = Number(__ENV.CARDS || '50')

export const authDuration = new Trend('auth_duration', true)
export const authApproved = new Counter('auth_approved')
export const authDeclined = new Counter('auth_declined')
export const authErrors = new Counter('auth_errors')

export const options = {
  summaryTrendStats: ['avg', 'min', 'med', 'max', 'p(50)', 'p(95)', 'p(99)'],
  scenarios: {
    authorizations: {
      executor: 'constant-arrival-rate',
      rate,
      timeUnit: '1s',
      duration,
      preAllocatedVUs: Math.max(30, Math.ceil(rate / 3)),
      maxVUs: Math.max(100, rate),
      gracefulStop: '10s',
    },
  },
}

function send(path, payload, phase = 'setup') {
  return http.post(`${base}${path}`, JSON.stringify(payload), {
    headers: { 'Content-Type': 'application/json' }, tags: { phase }, timeout: '5s',
  })
}

export function setup() {
  const runId = String(Date.now())
  const cardIds = []
  for (let index = 0; index < cards; index++) {
    const issued = send('/v1/cards', {
      companyId: `load_company_${runId}`, holder: `Synthetic Load Holder ${index}`,
      currency: 'SGD', limitMinor: 100000000,
      idempotencyKey: `load_issue_${runId}_${index}`,
    })
    if (issued.status !== 201) throw new Error(`card issue failed: ${issued.status} ${issued.body}`)
    const cardId = issued.json('reference')
    const active = http.patch(`${base}/v1/cards/${cardId}/state`, JSON.stringify({
      state: 'ACTIVE', idempotencyKey: `load_activate_${runId}_${index}`,
    }), { headers: { 'Content-Type': 'application/json' }, tags: { phase: 'setup' } })
    if (active.status !== 200) throw new Error(`activation failed: ${active.status} ${active.body}`)
    const controls = http.put(`${base}/v1/cards/${cardId}/controls`, JSON.stringify({
      controls: { velocityCount: 100000, velocitySeconds: 60 },
      idempotencyKey: `load_controls_${runId}_${index}`,
    }), { headers: { 'Content-Type': 'application/json' }, tags: { phase: 'setup' } })
    if (controls.status !== 200) throw new Error(`controls failed: ${controls.status} ${controls.body}`)
    cardIds.push(cardId)
  }
  return { runId, cardIds }
}

export default function (data) {
  const index = exec.scenario.iterationInTest
  const cardId = data.cardIds[index % data.cardIds.length]
  const response = send('/v1/authorizations', {
    authId: `auth_load_${data.runId}_${index}`, cardId,
    merchantId: `load_merchant_${index % 100}`, mcc: '5411', country: 'SG',
    currency: 'SGD', amountMinor: 100, deviceId: 'load_device',
    idempotencyKey: `load_auth_${data.runId}_${index}`,
  }, 'auth')
  authDuration.add(response.timings.duration)
  if (response.status !== 200) { authErrors.add(1); return }
  const status = response.json('status')
  if (status === 'APPROVED') authApproved.add(1)
  else if (status === 'DECLINED') authDeclined.add(1)
  else authErrors.add(1)
}
