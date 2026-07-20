<script setup lang="ts">
import { ref, onMounted } from 'vue'
import { message } from 'ant-design-vue'
import IotisticaLogo from './IotisticaLogo.vue'
import { api } from '../api'
import { setToken, currentUser } from '../auth'
import type { AuthResponse } from '../types'

const emit = defineEmits<{ authenticated: [] }>()

const checking = ref(true)
const mode = ref<'setup' | 'login'>('login')
const submitting = ref(false)
const errorMsg = ref('')

const username = ref('')
const password = ref('')
const confirmPassword = ref('')

onMounted(async () => {
  try {
    const { setup_required } = await api.auth.setupRequired()
    mode.value = setup_required ? 'setup' : 'login'
  } catch {
    errorMsg.value = 'Could not reach the simulator API'
  } finally {
    checking.value = false
  }
})

function applyAuth(res: AuthResponse) {
  setToken(res.access_token)
  currentUser.value = res.user
  emit('authenticated')
}

async function submit() {
  errorMsg.value = ''
  if (mode.value === 'setup' && password.value !== confirmPassword.value) {
    errorMsg.value = 'Passwords do not match'
    return
  }
  submitting.value = true
  try {
    const res = mode.value === 'setup'
      ? await api.auth.setup(username.value.trim(), password.value)
      : await api.auth.login(username.value.trim(), password.value)
    applyAuth(res)
    message.success(mode.value === 'setup' ? 'Admin account created' : 'Signed in')
  } catch (e: unknown) {
    errorMsg.value = (e as Error).message ?? 'Something went wrong'
  } finally {
    submitting.value = false
  }
}
</script>

<template>
  <div style="height:100vh;display:flex;align-items:center;justify-content:center;background:#f5f5f5">
    <div style="width:340px;padding:32px;background:white;border-radius:8px;box-shadow:0 2px 12px rgba(0,0,0,0.08)">
      <div style="display:flex;align-items:center;gap:10px;margin-bottom:24px">
        <IotisticaLogo :size="28" />
        <div>
          <div style="font-size:15px;font-weight:600">Iotistica</div>
          <div style="font-size:12px;color:#999">BACnet Simulator</div>
        </div>
      </div>

      <a-spin v-if="checking" style="display:block;text-align:center;padding:24px 0" />

      <template v-else>
        <div style="font-size:16px;font-weight:600;margin-bottom:4px">
          {{ mode === 'setup' ? 'Create the admin account' : 'Sign in' }}
        </div>
        <div v-if="mode === 'setup'" style="font-size:12px;color:#999;margin-bottom:16px">
          No accounts exist yet — this account will be the first login for this simulator instance.
        </div>
        <div v-else style="margin-bottom:16px" />

        <a-form layout="vertical" @submit.prevent="submit">
          <a-form-item label="Username" style="margin-bottom:12px">
            <a-input v-model:value="username" autofocus autocomplete="username" @pressEnter="submit" />
          </a-form-item>
          <a-form-item :label="mode === 'setup' ? 'Password (min 8 characters)' : 'Password'" style="margin-bottom:12px">
            <a-input-password v-model:value="password" autocomplete="new-password" @pressEnter="submit" />
          </a-form-item>
          <a-form-item v-if="mode === 'setup'" label="Confirm password" style="margin-bottom:12px">
            <a-input-password v-model:value="confirmPassword" autocomplete="new-password" @pressEnter="submit" />
          </a-form-item>

          <a-alert v-if="errorMsg" :message="errorMsg" type="error" show-icon style="margin-bottom:12px" />

          <a-button
            type="primary" block :loading="submitting"
            :disabled="!username.trim() || !password"
            @click="submit"
          >
            {{ mode === 'setup' ? 'Create account & sign in' : 'Sign in' }}
          </a-button>
        </a-form>
      </template>
    </div>
  </div>
</template>
