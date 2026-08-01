import { computed, ref, watchEffect } from 'vue'
import { theme } from 'ant-design-vue'

const STORAGE_KEY = 'bacnet-admin-dark-mode'

export const isDark = ref(localStorage.getItem(STORAGE_KEY) === '1')

// Drawers/modals teleport to <body>, so the toggle has to live on <html> —
// a class on some in-page wrapper wouldn't reach portalled content.
watchEffect(() => {
  document.documentElement.classList.toggle('dark', isDark.value)
  localStorage.setItem(STORAGE_KEY, isDark.value ? '1' : '0')
})

export function toggleDark() {
  isDark.value = !isDark.value
}

export const themeConfig = computed(() => ({
  algorithm: isDark.value ? theme.darkAlgorithm : theme.defaultAlgorithm,
  token: { colorPrimary: '#1890ff', borderRadius: 4 },
}))
