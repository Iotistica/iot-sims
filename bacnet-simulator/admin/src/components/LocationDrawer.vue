<script setup lang="ts">
import { ref, reactive, computed, watch, h } from 'vue'
import { Modal, message } from 'ant-design-vue'
import { api } from '../api'
import { buildLocationTreeOptions } from '../locationTree'
import type { Location, Meta, LocationDeletionImpact } from '../types'

const props = defineProps<{
  open: boolean
  location: Location | null
  locations: Location[]
  meta: Meta
  /** Preselects Parent Location when opening for a fresh Add (e.g. invoked
   * from a location row's contextual "+" action) -- ignored when editing an
   * existing location. */
  defaultParentLocationId?: number | null
}>()
const emit = defineEmits<{
  'update:open': [v: boolean]
  saved: []
}>()

const loading = ref(false)
const deleting = ref(false)
const form = reactive({
  name: '',
  description: '',
  parent_location_id: null as number | null,
  kind: null as string | null,
})

watch(() => props.open, (v) => {
  if (!v) return
  if (props.location) {
    Object.assign(form, {
      name: props.location.name,
      description: props.location.description ?? '',
      parent_location_id: props.location.parent_location_id,
      kind: props.location.kind ?? null,
    })
  } else {
    Object.assign(form, { name: '', description: '', parent_location_id: props.defaultParentLocationId ?? null, kind: null })
  }
})

// Editing an existing location must not offer itself or its own descendants
// as a parent — that would create a cycle (the backend refuses it too, but
// there's no reason to let the picker offer an option it knows is invalid).
const treeOptions = computed(() => buildLocationTreeOptions(props.locations, props.location?.id ?? null))

async function save() {
  if (!form.name.trim()) { message.error('Name is required'); return }
  loading.value = true
  const body = { name: form.name, description: form.description, parent_location_id: form.parent_location_id, kind: form.kind }
  try {
    if (props.location) {
      await api.locations.update(props.location.id, body)
      message.success('Location updated')
    } else {
      await api.locations.create(body)
      message.success('Location created')
    }
    emit('update:open', false)
    emit('saved')
  } catch (e: unknown) {
    message.error((e as Error).message)
  } finally {
    loading.value = false
  }
}

function pluralize(n: number, noun: string): string {
  return `${n} ${noun}${n === 1 ? '' : 's'}`
}

function runDelete(id: number, cascade: boolean) {
  deleting.value = true
  return api.locations.del(id, { cascade })
    .then(() => {
      message.success('Location deleted')
      emit('update:open', false)
      emit('saved')
    })
    .catch((e: unknown) => {
      message.error((e as Error).message ?? 'Failed to delete location')
    })
    .finally(() => {
      deleting.value = false
    })
}

async function doDelete() {
  if (!props.location) return
  const loc = props.location

  let impact: LocationDeletionImpact
  deleting.value = true
  try {
    impact = await api.locations.deletionImpact(loc.id)
  } catch (e: unknown) {
    message.error((e as Error).message ?? 'Failed to check location contents')
    deleting.value = false
    return
  }
  deleting.value = false

  // Empty location -- unchanged fast path, no impact to report.
  if (impact.sub_location_count + impact.device_count + impact.equipment_count === 0) {
    Modal.confirm({
      title: `Delete "${loc.name}"?`,
      okType: 'danger',
      okText: 'Delete',
      onOk: () => runDelete(loc.id, false),
    })
    return
  }

  // A point inside is still an aggregate-mapping member (MAX/MIN/weighted
  // average source) -- deleting it would silently change that aggregate's
  // result, so this is a hard block, not a warning. No destructive action
  // is offered.
  if (impact.blocked) {
    const [first, ...rest] = impact.blocking_points
    Modal.warning({
      title: `Can't delete "${loc.name}" yet`,
      okText: 'Got it',
      content: h('div', [
        h('p', 'This location contains a point that a simulation model still relies on, so it can’t be deleted automatically.'),
        h('p', `"${first.point_name}" on device "${first.device_name}" is a source for ${first.model_name}’s ${first.variable}.`),
        rest.length ? h('p', pluralize(rest.length, 'more point') + ' inside are affected the same way.') : null,
        h('p', `Remove ${rest.length ? 'these points' : 'this point'} from the simulation model, then try deleting the location again.`),
      ]),
    })
    return
  }

  const contentParts = [
    pluralize(impact.sub_location_count, 'sub-location'),
    pluralize(impact.device_count, 'device'),
    pluralize(impact.equipment_count, 'piece of equipment'),
  ].filter((_, i) => [impact.sub_location_count, impact.device_count, impact.equipment_count][i] > 0)
  let containsLine = `This location contains ${contentParts.join(', ')}`
  if (impact.point_count > 0) containsLine += ` and ${pluralize(impact.point_count, 'point')}`
  containsLine += '. Deleting it will delete everything inside.'

  const warningLines: string[] = []
  if (impact.affected_simulation_models.length) {
    const names = impact.affected_simulation_models.slice(0, 5).map((m) => m.name)
    const more = impact.affected_simulation_models.length - names.length
    warningLines.push(
      `${pluralize(impact.affected_simulation_models.length, 'simulation model')} (${names.join(', ')}${more > 0 ? `, +${more} more` : ''}) reference points inside this location and will lose those mappings.`,
    )
  }
  if (impact.affected_custom_graphs.length) {
    const names = impact.affected_custom_graphs.slice(0, 5).map((g) => g.name)
    const more = impact.affected_custom_graphs.length - names.length
    warningLines.push(
      `${pluralize(impact.affected_custom_graphs.length, 'saved graph')} (${names.join(', ')}${more > 0 ? `, +${more} more` : ''}) reference points inside this location and will show them as missing.`,
    )
  }

  Modal.confirm({
    title: `Delete "${loc.name}" and everything inside it?`,
    okType: 'danger',
    okText: 'Delete All',
    width: 480,
    content: h('div', [
      h('p', containsLine),
      ...warningLines.map((line) => h('p', { style: 'color:#faad14' }, line)),
    ]),
    onOk: () => runDelete(loc.id, true),
  })
}
</script>

<template>
  <a-drawer
    :title="location ? 'Edit Location' : 'Add Location'"
    :open="open"
    width="440"
    @close="emit('update:open', false)"
  >
    <a-form layout="vertical" :colon="false">
      <a-form-item label="Name" required>
        <a-input v-model:value="form.name" placeholder="Building A" />
      </a-form-item>

      <a-form-item label="Description">
        <a-input v-model:value="form.description" placeholder="Optional" />
      </a-form-item>

      <a-form-item label="Parent Location">
        <a-tree-select
          v-model:value="form.parent_location_id"
          :tree-data="treeOptions"
          allow-clear
          tree-default-expand-all
          placeholder="Top level"
          style="width: 100%"
        />
      </a-form-item>

      <a-form-item label="Semantic Type" help="Describes what this location represents in the building.">
        <a-select
          v-model:value="form.kind"
          show-search
          allow-clear
          placeholder="Not classified"
          :options="meta.location_kinds"
        />
      </a-form-item>
    </a-form>

    <template #footer>
      <div style="display:flex;justify-content:space-between;align-items:center">
        <a-button v-if="location" danger :loading="deleting" @click="doDelete">Delete</a-button>
        <div v-else />
        <a-space>
          <a-button @click="emit('update:open', false)">Cancel</a-button>
          <a-button type="primary" :loading="loading" @click="save">
            {{ location ? 'Save' : 'Create' }}
          </a-button>
        </a-space>
      </div>
    </template>
  </a-drawer>
</template>
