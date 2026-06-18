import { defineStore } from 'pinia'
import { ref } from 'vue'
import api from '../api'

/** Session-auth state. `authRequired` mirrors the server: false when no web
 *  password is configured (auth disabled). */
export const useAuthStore = defineStore('auth', () => {
  const authRequired = ref(false)
  const authenticated = ref(false)
  const loaded = ref(false)

  /** Ask the server whether auth is on and whether this session is valid. */
  async function refresh() {
    try {
      const { data } = await api.get('/api/auth/status')
      authRequired.value = !!data.auth_required
      authenticated.value = !!data.authenticated
    } catch {
      // /api/auth/status is open; a failure means the backend is unreachable.
      // Treat as "not authenticated" so the guard can hold at the login page.
      authenticated.value = false
    }
    loaded.value = true
  }

  async function login(username: string, password: string) {
    await api.post('/api/auth/login', { username, password })
    authenticated.value = true
    authRequired.value = true
  }

  async function logout() {
    try {
      await api.post('/api/auth/logout')
    } catch {
      // ignore — clear local state regardless
    }
    authenticated.value = false
  }

  return { authRequired, authenticated, loaded, refresh, login, logout }
})
