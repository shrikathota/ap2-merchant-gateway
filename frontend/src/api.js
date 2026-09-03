const BASE = '/api'

async function getJSON(path) {
  const res = await fetch(`${BASE}${path}`)
  if (!res.ok) {
    throw new Error(`GET ${path} -> ${res.status}`)
  }
  return res.json()
}

export function fetchLatestFlow() {
  return getJSON('/audit/latest')
}

export function fetchAuditChain(intentId) {
  return getJSON(`/audit/${encodeURIComponent(intentId)}`)
}

export function fetchTransactions() {
  return getJSON('/transact')
}
