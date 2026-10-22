// k6 constant-arrival-rate authorization workload with a mixed card, merchant
// and risk profile, for the full scored path. The mix is deterministic by
// iteration number, so two runs at the same rate send the same requests.
// Cards scale with the offered rate (CARDS defaults to 15 x RATE, one card per
// authorization in a 15-second run), each on its own device and usual merchant
// category. The synthetic scorer treats repeat purchases seconds apart on one
// card as fraud, so only the deliberate exceptions repeat: 4% of traffic is
// small-amount bursts on one card from a new device. About 3% is abroad.
// Setup traffic (card issue, activation, controls) is excluded from
// auth_duration. Run with MODE set to a label such as enforce or off.
import http from 'k6/http'
import exec from 'k6/execution'
import { Counter, Trend } from 'k6/metrics'

const rate = Number(__ENV.RATE || '100')
const duration = __ENV.DURATION || '15s'
const base = __ENV.API_URL || 'http://127.0.0.1:8086'
const cards = Number(__ENV.CARDS || String(15 * rate))

export const authDuration = new Trend('auth_duration', true)
export const authApproved = new Counter('auth_approved')
export const authDeclined = new Counter('auth_declined')
export const authErrors = new Counter('auth_errors')
export const modelApprove = new Counter('model_approve')
export const modelDecline = new Counter('model_decline')
export const rulesFallback = new Counter('rules_fallback')
export const modelOff = new Counter('model_off')

export const options = {
  summaryTrendStats: ['avg', 'min', 'med', 'max', 'p(50)', 'p(95)', 'p(99)'],
  setupTimeout: '600s',
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

const AMOUNTS = [120, 450, 899, 1500, 2350, 4999, 12000, 35000, 80000]
const COUNTRIES = ['SG', 'SG', 'SG', 'SG', 'MY', 'SG', 'ID', 'SG', 'GB', 'SG']
const MCCS = ['5411', '5812', '5999', '4111', '5732', '7995']
const RISKS = [0.05, 0.1, 0.1, 0.2, 0.35, 0.6, 0.9]

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
      companyId: `mix_company_${runId}_${index % 10}`, holder: `Synthetic Mixed Holder ${index}`,
      currency: 'SGD', limitMinor: 100000000, idempotencyKey: `mix_issue_${runId}_${index}`,
    })
    if (issued.status !== 201) throw new Error(`card issue failed: ${issued.status} ${issued.body}`)
    const cardId = issued.json('reference')
    const active = http.patch(`${base}/v1/cards/${cardId}/state`, JSON.stringify({
      state: 'ACTIVE', idempotencyKey: `mix_activate_${runId}_${index}`,
    }), { headers: { 'Content-Type': 'application/json' }, tags: { phase: 'setup' } })
    if (active.status !== 200) throw new Error(`activation failed: ${active.status} ${active.body}`)
    const controls = http.put(`${base}/v1/cards/${cardId}/controls`, JSON.stringify({
      controls: { velocityCount: 100000, velocitySeconds: 60 },
      idempotencyKey: `mix_controls_${runId}_${index}`,
    }), { headers: { 'Content-Type': 'application/json' }, tags: { phase: 'setup' } })
    if (controls.status !== 200) throw new Error(`controls failed: ${controls.status} ${controls.body}`)
    cardIds.push(cardId)
  }
  return { runId, cardIds }
}

export function mixedRequest(runId, cardIds, index) {
  // Blocks of 8 iterations, one block in 25, are small-amount bursts on one card
  // from a new device: the card-testing pattern the scorer is trained to notice.
  const burst = Math.floor(index / 8) % 25 === 0
  const card = burst ? Math.floor(index / 200) % cardIds.length : index % cardIds.length
  const country = index % 10 === 3 ? COUNTRIES[(index / 10) % COUNTRIES.length | 0] : 'SG'
  const abroad = country !== 'SG'
  return {
    authId: `auth_mix_${runId}_${index}`, cardId: cardIds[card],
    merchantId: `mix_merchant_${(index * 13) % 200}`,
    mcc: index % 9 === 0 ? MCCS[index % MCCS.length] : MCCS[card % MCCS.length], country,
    currency: 'SGD', amountMinor: burst ? 100 + (index % 8) : AMOUNTS[(index * 5) % AMOUNTS.length],
    deviceId: burst ? `new_device_${index}` : `device_${card}`,
    latitude: abroad ? 3.14 : 1.35, longitude: abroad ? 101.69 : 103.82,
    merchantRisk: RISKS[(index * 11) % RISKS.length], priorRefundRatio: (index % 17) / 100,
    idempotencyKey: `mix_auth_${runId}_${index}`,
  }
}

// Write the raw end-of-test summary to the file named by SUMMARY (any k6 version).
export function handleSummary(data) {
  return __ENV.SUMMARY ? { [__ENV.SUMMARY]: JSON.stringify(data, null, 2) } : {}
}

export default function (data) {
  const index = exec.scenario.iterationInTest
  const response = send('/v1/authorizations', mixedRequest(data.runId, data.cardIds, index), 'auth')
  authDuration.add(response.timings.duration)
  if (response.status !== 200) { authErrors.add(1); return }
  const body = response.json()
  if (body.status === 'APPROVED') authApproved.add(1)
  else if (body.status === 'DECLINED') authDeclined.add(1)
  else authErrors.add(1)
  if (body.modelAction === 'MODEL_APPROVE') modelApprove.add(1)
  else if (body.modelAction === 'MODEL_DECLINE') modelDecline.add(1)
  else if (body.modelAction === 'RULES_FALLBACK') rulesFallback.add(1)
  else if (body.modelAction === 'OFF') modelOff.add(1)
}
