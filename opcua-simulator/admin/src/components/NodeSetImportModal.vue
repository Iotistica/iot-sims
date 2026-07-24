<script setup lang="ts">
import { ref, watch, computed } from 'vue'
import { Modal, message } from 'ant-design-vue'
import {
  UploadOutlined, DeleteOutlined, CheckCircleOutlined, WarningOutlined, LeftOutlined,
} from '@ant-design/icons-vue'
import type { NodeSetPreviewResponse, NodeSetImportResponse, NodeSetImportRecord, NodeSetPlanTag } from '../types'
import { api } from '../api'

const props = defineProps<{ open: boolean }>()
const emit = defineEmits<{ 'update:open': [v: boolean]; imported: [] }>()

type Step = 'pick' | 'preview' | 'result'

const step = ref<Step>('pick')
const fileInput = ref<HTMLInputElement>()
const file = ref<File | null>(null)
const name = ref('')
const conflictStrategy = ref<'skip' | 'reject'>('skip')

const previewing = ref(false)
const importing = ref(false)
const preview = ref<NodeSetPreviewResponse | null>(null)
const result = ref<NodeSetImportResponse | null>(null)

const imports = ref<NodeSetImportRecord[]>([])
const importsLoading = ref(false)

function reset() {
  step.value = 'pick'
  file.value = null
  name.value = ''
  conflictStrategy.value = 'skip'
  preview.value = null
  result.value = null
}

watch(() => props.open, (v) => {
  if (!v) return
  reset()
  loadImports()
})

async function loadImports() {
  importsLoading.value = true
  try {
    imports.value = await api.nodesets.imports()
  } catch (e: unknown) {
    message.error((e as Error).message ?? 'Failed to load import history')
  } finally {
    importsLoading.value = false
  }
}

function pickFile() {
  fileInput.value?.click()
}

function onFileChange(e: Event) {
  const f = (e.target as HTMLInputElement).files?.[0]
  if (!f) return
  file.value = f
  if (!name.value.trim()) {
    name.value = f.name.replace(/\.xml$/i, '').replace(/[_-]+/g, ' ').trim()
  }
}

async function doPreview() {
  if (!file.value) { message.error('Choose a NodeSet2 XML file first'); return }
  previewing.value = true
  try {
    preview.value = await api.nodesets.preview(file.value)
    step.value = 'preview'
  } catch (e: unknown) {
    message.error((e as Error).message ?? 'Preview failed')
  } finally {
    previewing.value = false
  }
}

async function doImport() {
  if (!file.value) return
  importing.value = true
  try {
    result.value = await api.nodesets.import_(file.value, name.value.trim(), conflictStrategy.value)
    step.value = 'result'
    emit('imported')
  } catch (e: unknown) {
    message.error((e as Error).message ?? 'Import failed')
  } finally {
    importing.value = false
  }
}

function confirmDeleteImport(rec: NodeSetImportRecord) {
  Modal.confirm({
    title: `Remove import "${rec.source_filename}"?`,
    content: `Deletes the ${rec.device_count} device(s) it created (and their tags). Devices already removed independently are skipped.`,
    okType: 'danger',
    okText: 'Remove',
    async onOk() {
      try {
        await api.nodesets.deleteImport(rec.id)
        message.success('Import removed')
        await loadImports()
        emit('imported')
      } catch (e: unknown) {
        message.error((e as Error).message ?? 'Failed to remove import')
      }
    },
  })
}

function fmtDate(iso: string): string {
  return new Date(iso).toLocaleString()
}

const reportCounts = computed(() => preview.value?.report ?? result.value?.parse_report)
const hasIssues = computed(() => {
  const r = reportCounts.value
  return !!r && (r.warnings.length > 0 || r.errors.length > 0 || r.unsupported_features.length > 0)
})
</script>

<template>
  <a-modal
    :open="open"
    title="Import NodeSet2 XML"
    width="640"
    :footer="null"
    @cancel="emit('update:open', false)"
  >
    <!-- Step 1: pick file -->
    <template v-if="step === 'pick'">
      <a-form layout="vertical">
        <a-form-item label="NodeSet2 XML file" required>
          <input ref="fileInput" type="file" accept="application/xml,text/xml,.xml" style="display:none" @change="onFileChange" />
          <a-button block @click="pickFile">
            <template #icon><UploadOutlined /></template>
            {{ file?.name || 'Choose file…' }}
          </a-button>
        </a-form-item>
        <a-form-item label="Import name" help="Used to label the import batch and as the fallback device name for bare top-level variables.">
          <a-input v-model:value="name" placeholder="e.g. Packaging Line PLC" />
        </a-form-item>
        <a-form-item label="If a device name already exists" style="margin-bottom:8px">
          <a-select v-model:value="conflictStrategy" style="width:100%">
            <a-select-option value="skip">Skip that device, import the rest</a-select-option>
            <a-select-option value="reject">Fail the whole import</a-select-option>
          </a-select>
        </a-form-item>
      </a-form>

      <div style="display:flex;justify-content:flex-end;margin-top:8px">
        <a-button type="primary" :loading="previewing" @click="doPreview">Preview</a-button>
      </div>

      <a-collapse v-if="imports.length" style="margin-top:20px" ghost>
        <a-collapse-panel key="history" :header="`Recent imports (${imports.length})`">
          <a-spin :spinning="importsLoading">
            <div
              v-for="rec in imports" :key="rec.id"
              style="display:flex;align-items:center;justify-content:space-between;padding:8px 0;border-bottom:1px solid #f0f0f0"
            >
              <div style="min-width:0">
                <div style="font-size:13px;font-weight:500;white-space:nowrap;overflow:hidden;text-overflow:ellipsis">
                  {{ rec.source_filename }}
                </div>
                <div style="font-size:11px;color:#aaa">
                  {{ rec.device_count }} device(s) · {{ rec.tag_count }} tag(s)
                  <template v-if="rec.warning_count"> · {{ rec.warning_count }} warning(s)</template>
                  · {{ fmtDate(rec.imported_at) }}
                </div>
              </div>
              <a-button size="small" danger title="Remove this import" @click="confirmDeleteImport(rec)">
                <template #icon><DeleteOutlined /></template>
              </a-button>
            </div>
          </a-spin>
        </a-collapse-panel>
      </a-collapse>
    </template>

    <!-- Step 2: preview -->
    <template v-else-if="step === 'preview' && preview">
      <a-row :gutter="12" style="margin-bottom:16px">
        <a-col :span="6"><a-statistic title="Objects" :value="preview.report.objects" /></a-col>
        <a-col :span="6"><a-statistic title="Variables" :value="preview.report.variables" /></a-col>
        <a-col :span="6"><a-statistic title="Namespaces" :value="preview.report.namespaces" /></a-col>
        <a-col :span="6"><a-statistic title="Max depth" :value="preview.report.max_depth" /></a-col>
      </a-row>

      <a-alert
        type="info" show-icon style="margin-bottom:12px"
        :message="`Will create ${preview.plan.device_count} device(s), ${preview.plan.tag_count} tag(s)`"
      />

      <a-alert
        v-if="hasIssues"
        type="warning" show-icon style="margin-bottom:12px"
        :message="`${preview.report.warnings.length} warning(s), ${preview.report.unsupported_features.length} unsupported construct(s)`"
      >
        <template #description>
          <div style="max-height:120px;overflow:auto;font-size:12px">
            <div v-for="(w, i) in preview.report.warnings" :key="i" style="margin-bottom:2px">{{ w }}</div>
          </div>
        </template>
      </a-alert>

      <a-collapse ghost>
        <a-collapse-panel
          v-for="d in preview.plan.devices" :key="d.name"
          :header="`${d.name} (${d.tag_count} tag${d.tag_count !== 1 ? 's' : ''})`"
        >
          <a-table
            :data-source="d.tags"
            :pagination="false"
            size="small"
            row-key="name"
            :columns="[
              { title: 'Tag', dataIndex: 'name', key: 'name' },
              { title: 'Type', dataIndex: 'data_type', key: 'data_type', width: 90 },
              { title: 'Writable', dataIndex: 'writable', key: 'writable', width: 80 },
            ]"
          >
            <template #bodyCell="{ column, record }">
              <template v-if="column.key === 'writable'">
                {{ (record as NodeSetPlanTag).writable ? 'Yes' : '—' }}
              </template>
            </template>
          </a-table>
          <div v-if="d.tag_count > d.tags.length" style="font-size:12px;color:#aaa;margin-top:6px">
            + {{ d.tag_count - d.tags.length }} more not shown in this preview
          </div>
        </a-collapse-panel>
      </a-collapse>

      <div style="display:flex;justify-content:space-between;margin-top:16px">
        <a-button @click="step = 'pick'">
          <template #icon><LeftOutlined /></template>
          Back
        </a-button>
        <a-button type="primary" :loading="importing" @click="doImport">
          Import {{ preview.plan.device_count }} device(s)
        </a-button>
      </div>
    </template>

    <!-- Step 3: result -->
    <template v-else-if="step === 'result' && result">
      <a-result status="success" title="Import complete">
        <template #subTitle>
          {{ result.devices_created.length }} device(s) created, {{ result.tags_created }} tag(s) total
          <template v-if="result.devices_skipped.length">
            · {{ result.devices_skipped.length }} device(s) skipped (already existed)
          </template>
        </template>
      </a-result>

      <a-alert
        v-if="result.warnings.length"
        type="warning" show-icon style="margin-bottom:12px"
        :message="`${result.warnings.length} warning(s) during import`"
      >
        <template #description>
          <div style="max-height:120px;overflow:auto;font-size:12px">
            <div v-for="(w, i) in result.warnings" :key="i" style="margin-bottom:2px">{{ w }}</div>
          </div>
        </template>
      </a-alert>

      <div v-for="d in result.devices_created" :key="d.id" style="font-size:13px;padding:4px 0">
        <CheckCircleOutlined style="color:#52c41a;margin-right:6px" />
        {{ d.name }} — {{ d.tag_count }} tag(s)
      </div>
      <div v-for="n in result.devices_skipped" :key="n" style="font-size:13px;padding:4px 0;color:#aaa">
        <WarningOutlined style="margin-right:6px" />
        {{ n }} — skipped, already existed
      </div>

      <div style="display:flex;justify-content:flex-end;margin-top:16px">
        <a-button type="primary" @click="emit('update:open', false)">Done</a-button>
      </div>
    </template>
  </a-modal>
</template>
