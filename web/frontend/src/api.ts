import axios from 'axios'

const api = axios.create({
  baseURL: import.meta.env.VITE_API_URL || '',
  // Send the session cookie on every request (also covers any future
  // cross-origin dev setup). Same-origin would send it anyway.
  withCredentials: true,
})

// Retry transient failures with exponential backoff. Right after a server
// restart the heavy backend import + scheduler start can take many seconds;
// without this the first page loads hit a connection error and render empty
// (the "data disappeared on restart" symptom). We only retry idempotent GETs,
// and only on a missing response (backend unreachable) or 502/503/504 — real
// 4xx/5xx errors still surface immediately.
const MAX_RETRIES = 6

api.interceptors.response.use(undefined, async (error) => {
  const config = error.config
  if (!config) return Promise.reject(error)
  const method = (config.method || 'get').toLowerCase()
  const status = error.response?.status
  // Session expired / not logged in → hard-redirect to the login page. We use
  // window.location (not the router) to keep api.ts dependency-free and avoid an
  // import cycle with router.ts. The /api/auth/* calls are exempt so a failed
  // login surfaces its own error instead of bouncing.
  const url = config.url || ''
  if (status === 401 && !url.includes('/api/auth/')) {
    if (!window.location.pathname.startsWith('/login')) {
      const back = encodeURIComponent(window.location.pathname + window.location.search)
      window.location.assign(`/login?redirect=${back}`)
    }
    return Promise.reject(error)
  }
  const retriable = !error.response || status === 502 || status === 503 || status === 504
  if (method !== 'get' || !retriable) return Promise.reject(error)
  config.__retryCount = (config.__retryCount || 0) + 1
  if (config.__retryCount > MAX_RETRIES) return Promise.reject(error)
  const delay = Math.min(1000 * 2 ** (config.__retryCount - 1), 8000)
  await new Promise((r) => setTimeout(r, delay))
  return api(config)
})

export default api
