<script setup lang="ts">
import { computed, onMounted, ref } from 'vue'
import { Modal, message } from 'ant-design-vue'
import { PlusOutlined, DeleteOutlined } from '@ant-design/icons-vue'
import { api } from '../../api'
import type { FunctionalTest, Meta } from '../../types'
import FunctionalTestBuilder from './FunctionalTestBuilder.vue'

const meta = ref<Meta | null>(null)
const tests = ref<FunctionalTest[]>([])
const loading = ref(false)
const mode = ref<'list' | 'builder'>('list')
const activeTest = ref<FunctionalTest | null>(null)

const equipmentTypeLabel = computed(() => {
  const map: Record<string, string> = {}
  for (const o of meta.value?.equipment_types ?? []) map[o.value] = o.label
  return map
})

const columns = [
  { title: 'Name', dataIndex: 'name', key: 'name' },
  { title: 'Equipment Type', key: 'equipment_type' },
  { title: '', key: 'actions', width: 60 },
]

async function load() {
  loading.value = true
  try {
    const [m, t] = await Promise.all([api.meta(), api.functionalTests.list()])
    meta.value = m
    tests.value = t
  } catch {
    message.error('Failed to load functional tests')
  } finally {
    loading.value = false
  }
}

function openNew() {
  activeTest.value = null
  mode.value = 'builder'
}

function openTest(test: FunctionalTest) {
  activeTest.value = test
  mode.value = 'builder'
}

function confirmDelete(test: FunctionalTest) {
  Modal.confirm({
    title: `Delete "${test.name}"?`,
    content: 'This cannot be undone.',
    okType: 'danger',
    onOk: async () => {
      await api.functionalTests.del(test.id)
      message.success('Functional test deleted')
      await load()
    },
  })
}

function onSaved(saved: FunctionalTest) {
  activeTest.value = saved
  load()
}

function backToList() {
  mode.value = 'list'
  activeTest.value = null
}

onMounted(load)
</script>

<template>
  <div style="height:100%">
    <div v-if="mode === 'list'" style="padding:20px;overflow:auto;height:100%">
      <div style="display:flex;align-items:center;gap:12px;margin-bottom:16px">
        <h2 style="margin:0;font-size:16px">Functional Tests</h2>
        <div style="flex:1" />
        <a-button type="primary" @click="openNew">
          <template #icon><PlusOutlined /></template>
          New Test
        </a-button>
      </div>

      <a-table
        :columns="columns"
        :data-source="tests"
        :loading="loading"
        :show-sorter-tooltip="false"
        row-key="id"
        size="small"
        :pagination="{ pageSize: 25 }"
        :custom-row="(record: FunctionalTest) => ({ onClick: () => openTest(record), style: 'cursor:pointer' })"
      >
        <template #bodyCell="{ column, record }">
          <template v-if="column.key === 'equipment_type'">
            {{ equipmentTypeLabel[(record as FunctionalTest).equipment_type] ?? (record as FunctionalTest).equipment_type }}
          </template>
          <template v-else-if="column.key === 'actions'">
            <a-button size="small" type="text" danger @click.stop="confirmDelete(record as FunctionalTest)">
              <template #icon><DeleteOutlined /></template>
            </a-button>
          </template>
        </template>
        <template #emptyText>
          <div style="padding:24px;color:var(--text-placeholder)">
            No functional tests yet — click "New Test" to build one.
          </div>
        </template>
      </a-table>
    </div>

    <FunctionalTestBuilder
      v-else-if="meta"
      :test="activeTest"
      :meta="meta"
      @saved="onSaved"
      @back="backToList"
    />
  </div>
</template>
