import { ref } from 'vue'
import type { User } from './types'

const TOKEN_KEY = 'bacnet_sim_token'

export const authToken  = ref<string | null>(localStorage.getItem(TOKEN_KEY))
export const currentUser = ref<User | null>(null)

export function setToken(token: string | null) {
  authToken.value = token
  if (token) localStorage.setItem(TOKEN_KEY, token)
  else localStorage.removeItem(TOKEN_KEY)
}

export function logout() {
  setToken(null)
  currentUser.value = null
}
