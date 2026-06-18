<template>
  <div class="login-wrap">
    <n-card class="login-card" :bordered="false">
      <div class="login-brand">
        <img src="/logo.svg" alt="TradingAgents-Studio" class="login-logo" />
      </div>
      <p class="login-sub">{{ t('login.subtitle') }}</p>
      <n-form @submit.prevent="onSubmit">
        <n-form-item :label="t('login.username')" :show-feedback="false" style="margin-bottom: 16px">
          <n-input v-model:value="username" :placeholder="t('login.username')" @keydown.enter="onSubmit" />
        </n-form-item>
        <n-form-item :label="t('login.password')" :show-feedback="false" style="margin-bottom: 8px">
          <n-input
            v-model:value="password"
            type="password"
            show-password-on="click"
            :placeholder="t('login.password')"
            @keydown.enter="onSubmit"
          />
        </n-form-item>
        <p v-if="errorMsg" class="login-error">{{ errorMsg }}</p>
        <n-button type="primary" block :loading="loading" attr-type="submit" style="margin-top: 8px" @click="onSubmit">
          {{ t('login.submit') }}
        </n-button>
      </n-form>
    </n-card>
  </div>
</template>

<script setup lang="ts">
import { ref } from 'vue'
import { useI18n } from 'vue-i18n'
import { useRouter, useRoute } from 'vue-router'
import { useAuthStore } from '../stores/auth'

const { t } = useI18n()
const router = useRouter()
const route = useRoute()
const auth = useAuthStore()

const username = ref('admin')
const password = ref('')
const loading = ref(false)
const errorMsg = ref('')

async function onSubmit() {
  if (loading.value) return
  errorMsg.value = ''
  loading.value = true
  try {
    await auth.login(username.value, password.value)
    const redirect = (route.query.redirect as string) || '/'
    // Use replace so the login page isn't left in history.
    await router.replace(redirect)
  } catch (e: any) {
    errorMsg.value = e?.response?.data?.detail || t('login.error')
  } finally {
    loading.value = false
  }
}
</script>

<style scoped>
.login-wrap {
  height: 100vh;
  display: flex;
  align-items: center;
  justify-content: center;
  background: #f5f6f8;
}
.login-card {
  width: 360px;
  max-width: 90vw;
  box-shadow: 0 6px 28px rgba(0, 0, 0, 0.08);
  border-radius: 12px;
}
.login-brand {
  display: flex;
  justify-content: center;
  margin: 4px 0 6px;
}
.login-logo {
  height: 40px;
  width: auto;
}
.login-sub {
  text-align: center;
  color: #909090;
  font-size: 13px;
  margin: 0 0 22px;
}
.login-error {
  color: #d03050;
  font-size: 13px;
  margin: 4px 0 0;
}
</style>
