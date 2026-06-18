import { createRouter, createWebHistory } from 'vue-router'
import { useAuthStore } from './stores/auth'

const routes = [
  { path: '/login', name: 'login', component: () => import('./pages/Login.vue'), meta: { public: true } },
  { path: '/', name: 'dashboard', component: () => import('./pages/Dashboard.vue') },
  { path: '/analyze', name: 'analyze', component: () => import('./pages/NewAnalysis.vue') },
  { path: '/screener', name: 'screener', component: () => import('./pages/Screener.vue') },
  { path: '/progress/:id', name: 'progress', component: () => import('./pages/AnalysisProgress.vue') },
  { path: '/holdings', name: 'holdings', component: () => import('./pages/Holdings.vue') },
  { path: '/schedule', name: 'schedule', component: () => import('./pages/Schedule.vue') },
  { path: '/paper', name: 'paper', component: () => import('./pages/Paper.vue') },
  { path: '/backtest', name: 'backtest', component: () => import('./pages/Backtest.vue') },
  { path: '/quality', name: 'quality', component: () => import('./pages/Quality.vue') },
  { path: '/history', name: 'history', component: () => import('./pages/History.vue') },
  { path: '/report/:id', name: 'report', component: () => import('./pages/ReportDetail.vue') },
  { path: '/settings', name: 'settings', component: () => import('./pages/Settings.vue') },
]

const router = createRouter({
  history: createWebHistory(),
  routes,
})

// Auth gate: load the session status once, then keep unauthenticated users on
// the login page. When auth is disabled server-side (no web password), every
// route is allowed.
router.beforeEach(async (to) => {
  const auth = useAuthStore()
  if (!auth.loaded) await auth.refresh()

  if (to.meta.public) {
    // Already logged in? Skip the login page.
    if (to.name === 'login' && (!auth.authRequired || auth.authenticated)) {
      return { path: '/' }
    }
    return true
  }
  if (auth.authRequired && !auth.authenticated) {
    return { name: 'login', query: { redirect: to.fullPath } }
  }
  return true
})

export default router
