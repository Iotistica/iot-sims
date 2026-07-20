<script setup lang="ts">
import { ref, watch } from 'vue'
import { Modal, message } from 'ant-design-vue'
import { KeyOutlined, DeleteOutlined, UserAddOutlined } from '@ant-design/icons-vue'
import type { User } from '../types'
import { api } from '../api'
import { currentUser } from '../auth'

const props = defineProps<{ open: boolean }>()
const emit = defineEmits<{ 'update:open': [val: boolean] }>()

const users = ref<User[]>([])
const loading = ref(false)

async function load() {
  loading.value = true
  try {
    users.value = await api.users.list()
  } catch (e: unknown) {
    message.error((e as Error).message ?? 'Failed to load users')
  } finally {
    loading.value = false
  }
}

// Add-user modal
const addOpen = ref(false)
const addUsername = ref('')
const addPassword = ref('')
const addLoading = ref(false)

function openAdd() {
  addUsername.value = ''
  addPassword.value = ''
  addOpen.value = true
}

async function doAdd() {
  if (!addUsername.value.trim() || addPassword.value.length < 8) {
    message.error('Username required, password must be at least 8 characters')
    return
  }
  addLoading.value = true
  try {
    await api.users.create(addUsername.value.trim(), addPassword.value)
    message.success('User created')
    addOpen.value = false
    await load()
  } catch (e: unknown) {
    message.error((e as Error).message ?? 'Failed to create user')
  } finally {
    addLoading.value = false
  }
}

// Reset-password modal
const resetOpen = ref(false)
const resetUser = ref<User | null>(null)
const resetPassword = ref('')
const resetLoading = ref(false)

function openReset(u: User) {
  resetUser.value = u
  resetPassword.value = ''
  resetOpen.value = true
}

async function doReset() {
  if (!resetUser.value || resetPassword.value.length < 8) {
    message.error('Password must be at least 8 characters')
    return
  }
  resetLoading.value = true
  try {
    await api.users.resetPassword(resetUser.value.id, resetPassword.value)
    message.success(`Password reset for "${resetUser.value.username}"`)
    resetOpen.value = false
  } catch (e: unknown) {
    message.error((e as Error).message ?? 'Failed to reset password')
  } finally {
    resetLoading.value = false
  }
}

function confirmDelete(u: User) {
  Modal.confirm({
    title: `Delete user "${u.username}"?`,
    okType: 'danger',
    okText: 'Delete',
    async onOk() {
      try {
        await api.users.del(u.id)
        message.success('User deleted')
        await load()
      } catch (e: unknown) {
        message.error((e as Error).message ?? 'Failed to delete user')
      }
    },
  })
}

function fmtDate(iso: string | null): string {
  return iso ? new Date(iso).toLocaleString() : 'never'
}

watch(() => props.open, (isOpen) => {
  if (isOpen) load()
})
</script>

<template>
  <a-drawer
    :open="open"
    title="Users"
    width="460"
    @close="emit('update:open', false)"
  >
    <a-button type="primary" style="margin-bottom:16px" @click="openAdd">
      <template #icon><UserAddOutlined /></template>
      Add user
    </a-button>

    <a-spin :spinning="loading">
      <div v-if="!users.length && !loading" style="text-align:center;color:#bbb;padding:60px 0;font-size:14px">
        No users yet
      </div>
      <div
        v-for="u in users"
        :key="u.id"
        style="border:1px solid #e8e8e8;border-radius:6px;padding:12px 14px;margin-bottom:10px;background:white"
      >
        <div style="display:flex;align-items:flex-start;gap:8px">
          <div style="flex:1;min-width:0">
            <div style="font-weight:600;font-size:14px;white-space:nowrap;overflow:hidden;text-overflow:ellipsis">
              {{ u.username }}
              <a-tag v-if="currentUser?.id === u.id" color="blue" style="margin-left:4px;font-size:10px">you</a-tag>
            </div>
            <div style="font-size:11px;color:#bbb;margin-top:4px">
              Created {{ fmtDate(u.created_at) }} · last login {{ fmtDate(u.last_login_at) }}
            </div>
          </div>
          <a-space :size="4">
            <a-button size="small" title="Reset password" @click="openReset(u)">
              <template #icon><KeyOutlined /></template>
            </a-button>
            <a-button size="small" danger title="Delete" :disabled="users.length <= 1" @click="confirmDelete(u)">
              <template #icon><DeleteOutlined /></template>
            </a-button>
          </a-space>
        </div>
      </div>
    </a-spin>

    <template #footer>
      <a-button @click="emit('update:open', false)">Close</a-button>
    </template>

    <!-- Add user modal -->
    <a-modal
      v-model:open="addOpen"
      title="Add user"
      ok-text="Create"
      :confirm-loading="addLoading"
      @ok="doAdd"
    >
      <a-form layout="vertical">
        <a-form-item label="Username">
          <a-input v-model:value="addUsername" @pressEnter="doAdd" />
        </a-form-item>
        <a-form-item label="Password (min 8 characters)">
          <a-input-password v-model:value="addPassword" autocomplete="new-password" @pressEnter="doAdd" />
        </a-form-item>
      </a-form>
    </a-modal>

    <!-- Reset password modal -->
    <a-modal
      v-model:open="resetOpen"
      :title="`Reset password — ${resetUser?.username}`"
      ok-text="Reset"
      :confirm-loading="resetLoading"
      @ok="doReset"
    >
      <a-form layout="vertical">
        <a-form-item label="New password (min 8 characters)">
          <a-input-password v-model:value="resetPassword" autocomplete="new-password" @pressEnter="doReset" />
        </a-form-item>
      </a-form>
    </a-modal>
  </a-drawer>
</template>
