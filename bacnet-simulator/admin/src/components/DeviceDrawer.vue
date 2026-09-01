<script setup lang="ts">
import { ref, reactive, computed, watch } from 'vue'
import { Modal, message } from 'ant-design-vue'
import { ExperimentOutlined, UploadOutlined } from '@ant-design/icons-vue'
import { api } from '../api'
import type { SimulationProviderCatalogEntry, SimulationProviderType } from '../api'
import { buildLocationTreeOptions } from '../locationTree'
import { nextFreeInstance } from '../deviceInstance'
import type { Device, Meta, Location, Equipment, Template } from '../types'

const props = defineProps<{
  open: boolean
  device: Device | null
  meta: Meta
  locations?: Location[]
  equipment?: Equipment[]
  existingInstances?: number[]
  draftMode?: boolean
  draftDevice?: Record<string, any> | null
  /** Preselects Location when opening for a fresh Add (e.g. invoked from a
   * location row's contextual "+" action) -- ignored when editing an
   * existing device. */
  defaultLocationId?: number | null
  /** Set when opened from an Equipment panel's "Assign Controller" action
   * with no controller yet available to assign -- both Location and
   * Controls are already fully determined in that case (this equipment's
   * own location; controls this equipment), so the form hides those two
   * fields entirely and wires them up silently on save, rather than asking
   * the user to re-state facts the flow already knows. Ignored when editing
   * an existing device. */
  lockedEquipmentId?: number | null
}>()
const emit = defineEmits<{
  'update:open': [v: boolean]
  /** deviceId is the created/updated device's id -- omitted after a delete
   * (nothing left to focus). Lets a caller like App.vue's
   * openAddControllerForEquipment flow switch focus to the device that was
   * actually just created, instead of leaving selection wherever it was. */
  saved: [deviceId?: number]
  'draft-saved': [data: Record<string, any>]
  'simulation-model': [device: Device]
}>()

// Single source of truth for a new device's default Vendor/Model -- also
// reused in the fresh-add reset below, so there's one place to change them.
const DEFAULT_VENDOR_NAME = 'Vendor'
const DEFAULT_MODEL_NAME = 'Model'

const loading = ref(false)
const deleting = ref(false)
const form = reactive({
  device_instance: 1001,
  name: '',
  description: '',
  vendor_name: DEFAULT_VENDOR_NAME,
  model_name: DEFAULT_MODEL_NAME,
  enabled: true,
  firmware_revision: 'N/A',
  protocol_revision: 22,
  max_apdu_length_accepted: 1024,
  segmentation_supported: 'segmented-both',
  location_id: null as number | null,
  equipment_type: null as string | null,
  can_receive_event_notifications: null as boolean | null,
})

const locationTreeOptions = computed(() => buildLocationTreeOptions(props.locations ?? []))

// ── Controls (Controller -> Equipment semantic relationship) ────────────────
// Reuses the existing `controls` predicate the Equipment panel's "Controlled
// By"/"Assign Controller" already read/write from the other direction --
// see src/semantics/mirror.py's sync_controller_entity for why a device only
// ever gets a Controller semantic entity through an explicit action (device
// creation via "+ Add > Controller", or -- new here -- an explicit Controls
// selection on an existing device).
const controlsEquipmentIds = ref<number[]>([])
const initialControlsEquipmentIds = ref<number[]>([])
const controllerEntityId = ref<number | null>(null)
const relationshipIdByEquipmentId = ref<Record<number, number>>({})

const equipmentOptionGroups = computed(() => {
  const all = props.equipment ?? []
  const toOption = (e: Equipment) => ({ value: e.id, label: e.name })
  if (form.location_id == null) {
    return [{ label: 'Equipment', options: all.map(toOption) }]
  }
  const here = all.filter(e => e.location_id === form.location_id)
  const elsewhere = all.filter(e => e.location_id !== form.location_id)
  const groups = []
  if (here.length) groups.push({ label: 'In this location', options: here.map(toOption) })
  if (elsewhere.length) groups.push({ label: 'Other equipment', options: elsewhere.map(toOption) })
  return groups
})


// ── Simulation provider -------------------------------------------------------
// Built-in is the fallback/default provider. A controller does not persist a
// single provider field because explicit System/FMU/AI models may map
// points across multiple controllers. This selector exposes the available
// provider families and reflects enabled models created from this controller.
// Choosing a non-built-in provider is the starting context for Add Model; point
// ownership does not change until a model is actually configured/mapped.
const simulationProviders = ref<SimulationProviderCatalogEntry[]>([])
const simulationProvidersLoading = ref(false)
const simulationProviderType = ref<SimulationProviderType>('builtin')
const activeProviderTypesForController = ref<SimulationProviderType[]>([])

const simulationProviderOptions = computed(() =>
  simulationProviders.value.map(provider => ({
    value: provider.provider_type,
    label: provider.label,
    disabled: !provider.available || (
      provider.provider_type !== 'builtin'
      && provider.persistent_model_required
      && !activeProviderTypesForController.value.includes(provider.provider_type)
    ),
  }))
)

const selectedSimulationProvider = computed(() =>
  simulationProviders.value.find(
    provider => provider.provider_type === simulationProviderType.value,
  )
)
const simulationSummary = computed(() => {
  if (activeProviderTypesForController.value.length === 0) return 'Built-in point behaviors'
  const labels = activeProviderTypesForController.value.map(type =>
    simulationProviders.value.find(provider => provider.provider_type === type)?.label ?? type,
  )
  return labels.join(', ')
})

async function loadSimulationProviderState() {
  simulationProvidersLoading.value = true

  try {
    const catalog = await api.simulationProviders.catalog()

    // Defensive fallback keeps the Controller drawer usable against an older
    // backend while the API/router rollout is being completed.
    simulationProviders.value = catalog.length
      ? catalog
      : [{
          provider_type: 'builtin',
          label: 'Built-in',
          available: true,
          persistent_model_required: false,
          description: 'Default per-point behavior provider.',
        }]

    simulationProviderType.value = 'builtin'
    activeProviderTypesForController.value = []

    if (!props.draftMode && props.device) {
      const models = await api.simulationModels.list(props.device.id)

      const activeProviderTypes = [
        ...new Set(
          models
            .filter(model => model.enabled)
            .map(model => model.provider_type),
        ),
      ]

          activeProviderTypesForController.value = activeProviderTypes

      // Today Add Model normally creates one provider family from the
      // controller context. If several exist, leave the selector on Built-in
      // rather than pretending the controller itself has one owner.
      if (activeProviderTypes.length === 1) {
        simulationProviderType.value = activeProviderTypes[0]
      }
    }
  } catch {
    simulationProviders.value = [{
      provider_type: 'builtin',
      label: 'Built-in',
      available: true,
      persistent_model_required: false,
      description: 'Default per-point behavior provider.',
    }]
    simulationProviderType.value = 'builtin'
  } finally {
    simulationProvidersLoading.value = false
  }
}

function onSimulationProviderChange(value: SimulationProviderType) {
  const selected = simulationProviders.value.find(
    item => item.provider_type === value,
  )
  if (
    !selected
    || !selected.available
    || (
      selected.provider_type !== 'builtin'
      && selected.persistent_model_required
      && !activeProviderTypesForController.value.includes(selected.provider_type)
    )
  ) {
    return
  }

  simulationProviderType.value = value

  if (value !== 'builtin') {
    const provider = selected

    if (provider?.available) {
      message.info(
        `${provider.label} selected. Add/configure a model to map points and activate this provider.`,
      )
    }
  }
}

// External devices: "read-only toward the physical device" means
// device_instance/vendor/model/BACnet-info/Enabled stay locked (they mirror
// the real device or simulator ownership) -- everything else (name,
// description, location, semantic type, event-notification override) is
// project-local metadata and stays fully editable. Mirrors the backend's
// reject_external_source_mutation() field list.
const isExternal = computed(() => props.device?.source_type === 'external-bacnet')

// Mirrors _effective_can_receive_events() in src/simulator.py: untagged
// devices (no equipment_type — workstations, BMS servers, gateways have no
// equipment class in this vocabulary) default to "can receive"; devices
// tagged as a piece of physical equipment default to "cannot receive".
const inferredCanReceiveEvents = computed(() => form.equipment_type === null)

// ── Import points from EDE (only offered when adding a new, non-draft device) ──

const edeFile = ref<File | null>(null)
const edeFileName = ref('')
const edeFileInput = ref<HTMLInputElement>()

// ── From Template (only offered when adding a new, non-draft device) ──────────
// Populating a fresh controller's starting objects from a template, folded
// into creation itself instead of a separate step afterward (previously
// only reachable from ObjectsPanel.vue's own "From Template", which needs a
// real deviceId to apply against -- doesn't exist yet here, so the chosen
// template's objects are only actually created in save()'s create branch,
// once deviceId is known).
const allTemplates = ref<Template[]>([])
const chosenTemplateId = ref<number | null>(null)

// Known only when opened from an Equipment panel's "Add Controller" action
// (see lockedEquipmentId doc above) -- narrows the picker to templates
// tagged for this equipment's own type; falls back to every template
// (built-in + user, tagged or not) when the type isn't known, matching
// ObjectsPanel's own From Template picker's default (nothing filtered).
const lockedEquipmentType = computed(() => {
  if (props.lockedEquipmentId == null) return null
  return props.equipment?.find(e => e.id === props.lockedEquipmentId)?.equipment_type ?? null
})

const templateOptions = computed(() => {
  const type = lockedEquipmentType.value
  const matching = type ? allTemplates.value.filter(t => t.equipment_types?.includes(type)) : allTemplates.value
  return matching.map(t => ({ value: t.id, label: t.label }))
})

async function loadTemplates() {
  try {
    allTemplates.value = await api.templates.list()
  } catch {
    allTemplates.value = []
  }
}

function onEdeFileChange(e: Event) {
  const file = (e.target as HTMLInputElement).files?.[0]
  ;(e.target as HTMLInputElement).value = ''
  if (!file) return

  const reader = new FileReader()
  reader.onload = () => {
    const text = reader.result as string
    const lines = text.split(/\r?\n/).filter(l => l.trim() && !l.trim().startsWith('#'))
    if (lines.length < 2) { message.error('No data rows found in that EDE file'); return }

    const header = lines[0].split(';').map(h => h.trim().toLowerCase())
    const idx = header.indexOf('device-instance')
    const instances = new Set<number>()
    if (idx !== -1) {
      for (const line of lines.slice(1)) {
        const n = Number(line.split(';')[idx]?.trim())
        if (Number.isFinite(n)) instances.add(n)
      }
    }
    if (instances.size > 1) {
      message.warning('This EDE file covers multiple devices — add each device separately, or use project-level EDE import (Open Project → Import → EDE) instead.')
      return
    }

    edeFile.value = file
    edeFileName.value = file.name

    const [instance] = instances
    if (instance !== undefined) {
      if ((props.existingInstances ?? []).includes(instance)) {
        message.warning(`Device instance ${instance} from the file is already in use — keeping ${form.device_instance}.`)
      } else {
        form.device_instance = instance
      }
    }
    if (!form.name.trim()) {
      form.name = file.name.replace(/\.[^.]+$/, '').replace(/[_-]+/g, ' ').trim()
    }
  }
  reader.readAsText(file)
}

// ── Vendor / model picker ─────────────────────────────────────────────────────

interface VendorModel {
  name: string
  type?: string
  typeLabel?: string
  pics_url?: string
  listing_url?: string
  certificate_url?: string
  object_types?: Record<string, number | boolean>
  pics_error?: string
}
interface Vendor { name: string; models: VendorModel[] }

const vendors = ref<Vendor[]>([])
const vendorsLoading = ref(false)

const vendorOptions = computed(() =>
  vendors.value.map(v => ({ value: v.name, label: v.name }))
)

const selectedVendor = computed(() => vendors.value.find(v => v.name === form.vendor_name))

const modelOptions = computed(() => {
  const v = selectedVendor.value
  if (!v) return []
  return v.models.map(m => ({
    value: m.name,
    label: m.typeLabel ? `${m.name} (${m.typeLabel})` : m.name,
  }))
})

// Scraped BTL catalog info (device profile, PICS datasheet, supported object
// types) for whichever vendor/model is currently selected — surfaced so the
// user can see what was scraped without leaving the form.
const selectedModel = computed(() =>
  selectedVendor.value?.models.find(m => m.name === form.model_name)
)

const selectedModelObjectTypes = computed(() => {
  const types = selectedModel.value?.object_types
  if (!types) return []
  return Object.entries(types).map(([code, count]) => ({
    code,
    label: typeof count === 'number' ? `${code} ×${count}` : code,
  }))
})

function filterOption(input: string, opt: { value?: string | number; label?: string }) {
  return (opt.label ?? String(opt.value ?? '')).toLowerCase().includes(input.toLowerCase())
}

async function loadVendors() {
  if (vendors.value.length || vendorsLoading.value) return
  vendorsLoading.value = true
  try {
    const res = await fetch('/bacnet-vendors.json')
    if (res.ok) vendors.value = (await res.json()).vendors ?? []
  } catch { } finally {
    vendorsLoading.value = false
  }
}

// ─────────────────────────────────────────────────────────────────────────────

async function loadControls() {
  controlsEquipmentIds.value = []
  initialControlsEquipmentIds.value = []
  controllerEntityId.value = null
  relationshipIdByEquipmentId.value = {}

  if (props.draftMode || !props.device) return

  const controllerEntities = await api.semanticEntities.list({ device_id: props.device.id, entity_kind: 'controller' })
  const entity = controllerEntities[0]
  if (!entity) return
  controllerEntityId.value = entity.id

  const [targets, relationships] = await Promise.all([
    api.semanticEntities.related(entity.id, 'controls', 'out'),
    api.semanticRelationships.list({ source_entity_id: entity.id, predicate: 'controls' }),
  ])

  const relIdByTargetEntityId: Record<number, number> = {}
  for (const rel of relationships) relIdByTargetEntityId[rel.target_entity_id] = rel.id

  const ids: number[] = []
  const relByEquipmentId: Record<number, number> = {}
  for (const t of targets) {
    if (t.equipment_id == null) continue
    ids.push(t.equipment_id)
    const relId = relIdByTargetEntityId[t.id]
    if (relId != null) relByEquipmentId[t.equipment_id] = relId
  }
  controlsEquipmentIds.value = ids
  initialControlsEquipmentIds.value = [...ids]
  relationshipIdByEquipmentId.value = relByEquipmentId
}

watch(() => props.open, (v) => {
  if (!v) return
  loadVendors()
  loadControls()
  loadSimulationProviderState()
  edeFile.value = null
  edeFileName.value = ''
  chosenTemplateId.value = null
  loadTemplates()
  const src = props.draftMode ? props.draftDevice : props.device
  if (src) {
    Object.assign(form, {
      device_instance: src.device_instance,
      name: src.name,
      description: src.description ?? '',
      vendor_name: src.vendor_name,
      model_name: src.model_name,
      enabled: !!src.enabled,
      firmware_revision: src.firmware_revision ?? 'N/A',
      protocol_revision: src.protocol_revision ?? 22,
      max_apdu_length_accepted: src.max_apdu_length_accepted ?? 1024,
      segmentation_supported: src.segmentation_supported ?? 'segmented-both',
      location_id: src.location_id ?? null,
      equipment_type: src.equipment_type ?? null,
      can_receive_event_notifications: src.can_receive_event_notifications ?? null,
    })
  } else {
    // Opened pre-wired to an equipment (see lockedEquipmentId doc above) -- both the
    // equipment's own name and its location are already known facts at this point, so
    // suggest a real Name/Description instead of making the user restate them from the
    // generic placeholder examples.
    const lockedEquipment = props.lockedEquipmentId != null
      ? props.equipment?.find(e => e.id === props.lockedEquipmentId)
      : null
    const lockedLocationName = lockedEquipment?.location_id != null
      ? props.locations?.find(l => l.id === lockedEquipment.location_id)?.name
      : null
    Object.assign(form, {
      device_instance: nextFreeInstance(props.existingInstances ?? []),
      name: lockedEquipment ? `${lockedEquipment.name} Controller` : '',
      description: lockedLocationName ? `${lockedLocationName} BACnet router` : '',
      vendor_name: DEFAULT_VENDOR_NAME, model_name: DEFAULT_MODEL_NAME, enabled: true,
      firmware_revision: 'N/A', protocol_revision: 22, max_apdu_length_accepted: 1024, segmentation_supported: 'segmented-both',
      location_id: props.defaultLocationId ?? null,
      equipment_type: null,
      can_receive_event_notifications: null,
    })
    // loadControls() (called above) already reset controlsEquipmentIds/
    // initialControlsEquipmentIds to [] synchronously for a fresh Add (it
    // returns before its first await when !props.device) -- safe to set the
    // locked target here without it getting clobbered afterward. Keeping
    // initialControlsEquipmentIds at [] means save()'s syncControlsRelationships
    // sees this as a genuinely new relationship to create, same as if the
    // user had picked it in the (now-hidden) Controls field themselves.
    if (props.lockedEquipmentId != null) {
      controlsEquipmentIds.value = [props.lockedEquipmentId]
    }
  }
})

// Reconciles this Controller's `controls` edges against controlsEquipmentIds
// -- only ever touches `controls` relationships sourced from THIS
// controller entity; every other predicate/entity is untouched.
async function syncControlsRelationships(deviceId: number) {
  const finalIds = controlsEquipmentIds.value
  const initialIds = initialControlsEquipmentIds.value
  if (finalIds.length === 0 && initialIds.length === 0) return

  let entityId = controllerEntityId.value
  if (entityId == null) {
    const entities = await api.semanticEntities.list({ device_id: deviceId, entity_kind: 'controller' })
    entityId = entities[0]?.id ?? null
  }
  if (entityId == null) return

  const toAdd = finalIds.filter(id => !initialIds.includes(id))
  const toRemove = initialIds.filter(id => !finalIds.includes(id))

  for (const equipmentId of toAdd) {
    const targets = await api.semanticEntities.list({ entity_kind: 'equipment', equipment_id: equipmentId })
    const target = targets[0]
    if (!target) continue
    await api.semanticRelationships.create({ source_entity_id: entityId, predicate: 'controls', target_entity_id: target.id })
  }
  for (const equipmentId of toRemove) {
    const relId = relationshipIdByEquipmentId.value[equipmentId]
    if (relId != null) await api.semanticRelationships.del(relId)
  }
}

async function save() {
  if (!form.name.trim()) { message.error('Name is required'); return }
  if (props.draftMode) {
    emit('draft-saved', { ...form })
    emit('update:open', false)
    return
  }
  loading.value = true
  const body = { ...form, enabled: form.enabled ? 1 : 0 }
  // Tracks whether ANY semantic step (Controller-role sync, Controls
  // relationship sync) failed -- the primary device save is authoritative
  // and is never rolled back for this, but the user must be able to tell
  // "fully succeeded" apart from "device saved, semantic sync failed"
  // rather than getting an unconditional success toast either way.
  let semanticSyncFailed = false
  try {
    let deviceId: number
    let createdNew: boolean
    if (props.device) {
      await api.devices.update(props.device.id, body)
      deviceId = props.device.id
      createdNew = false
      // Case 3 (legacy device, never explicitly made a Controller): stop
      // here -- editing/saving must never by itself grant the Controller
      // semantic role. Case 2 (already a Controller): keep its semantic
      // entity's name in sync, same as any other idempotent upsert. NEW:
      // an explicit Controls selection on a legacy device is itself an
      // explicit "make this a Controller" action, same principle as
      // Case 1 below -- so it also triggers the upgrade.
      if (props.device.has_controller_entity || controlsEquipmentIds.value.length > 0) {
        try {
          await api.devices.markAsController(deviceId)
        } catch {
          semanticSyncFailed = true
        }
      }
    } else {
      const created = await api.devices.create(body)
      deviceId = created.id
      createdNew = true
      // Case 1: the only entry path is "+ Add > Controller" -- unambiguous
      // explicit Controller creation, always mark it.
      try {
        await api.devices.markAsController(deviceId)
      } catch {
        semanticSyncFailed = true
      }
      if (edeFile.value) {
        try {
          const result = await api.devices.importEde(deviceId, edeFile.value)
          message.success(`${result.objects_imported} object${result.objects_imported !== 1 ? 's' : ''} imported from EDE`)
        } catch (e: unknown) {
          message.error(`Device created, but EDE import failed: ${(e as Error).message}`)
        }
      }
      if (chosenTemplateId.value) {
        const tpl = allTemplates.value.find(t => t.id === chosenTemplateId.value)
        if (tpl) {
          let createdCount = 0
          for (const obj of tpl.objects) {
            try {
              await api.objects.create(deviceId, { ...obj, enabled: 1 })
              createdCount++
            } catch {
              // Skip genuine per-object failures (e.g. a stray conflict with
              // an EDE-imported object at the same object_type+instance) --
              // matches TemplatePickerModal.applyTemplate()'s own tolerance,
              // reported in aggregate below rather than per-object.
            }
          }
          if (createdCount === tpl.objects.length) {
            message.success(`Applied "${tpl.label}" — ${createdCount} object${createdCount !== 1 ? 's' : ''} created`)
          } else {
            message.warning(`Applied "${tpl.label}" — ${createdCount}/${tpl.objects.length} objects created`)
          }
        }
      }
    }

    try {
      await syncControlsRelationships(deviceId)
    } catch {
      semanticSyncFailed = true
    }

    if (semanticSyncFailed) {
      message.warning('Controller saved, but Equipment relationships could not be updated.')
    } else {
      message.success(createdNew ? 'Device created' : 'Device updated')
    }

    emit('update:open', false)
    emit('saved', deviceId)
  } catch (e: unknown) {
    message.error((e as Error).message)
  } finally {
    loading.value = false
  }
}

function doDelete() {
  if (!props.device) return
  const dev = props.device
  const external = isExternal.value
  Modal.confirm({
    title: external ? `Remove "${dev.name}" from this project?` : `Delete "${dev.name}"?`,
    content: external
      ? 'Removes this device and its discovered objects from the project inventory. The physical device on the network is unaffected.'
      : 'This also deletes all its objects and cannot be undone.',
    okType: 'danger',
    okText: external ? 'Remove' : 'Delete',
    onOk: async () => {
      deleting.value = true
      try {
        await api.devices.del(dev.id)
        message.success(external ? 'Device removed from project' : 'Device deleted')
        emit('update:open', false)
        emit('saved')
      } catch (e: unknown) {
        message.error((e as Error).message ?? 'Failed to delete device')
      } finally {
        deleting.value = false
      }
    },
  })
}
</script>

<template>
  <a-drawer
    :title="device ? 'Edit Controller' : 'Add Controller'"
    :open="open"
    width="440"
    @close="emit('update:open', false)"
  >
    <a-form layout="vertical" :colon="false">
      <a-row :gutter="12">
        <a-col :span="16">
          <a-form-item label="Name" required>
            <a-input v-model:value="form.name" placeholder="AHU-1-Controller" />
          </a-form-item>
        </a-col>
        <a-col :span="8">
          <a-form-item label="Device Instance" required>
            <a-input-number v-model:value="form.device_instance" :disabled="isExternal" :min="1" :max="4194302" style="width:100%" />
          </a-form-item>
        </a-col>
      </a-row>

      <a-form-item label="Description">
        <a-input v-model:value="form.description" placeholder="3rd floor BACnet router" />
      </a-form-item>

      <a-row :gutter="12">
        <a-col :span="12">
          <a-form-item label="Manufacturer">
            <a-auto-complete
              v-model:value="form.vendor_name"
              :disabled="isExternal"
              :options="vendorOptions"
              :filter-option="filterOption"
              allow-clear
              placeholder="e.g. Siemens"
              @change="() => { form.model_name = '' }"
            />
          </a-form-item>
        </a-col>
        <a-col :span="12">
          <a-form-item label="Model">
            <!-- a-select when vendor is in BTL list (click-to-open dropdown) -->
            <a-select
              v-if="modelOptions.length"
              v-model:value="form.model_name"
              :disabled="isExternal"
              show-search
              allow-clear
              placeholder="Select model…"
              :options="modelOptions"
              :filter-option="filterOption"
            />
            <!-- plain input for custom vendors not in list -->
            <a-input
              v-else
              v-model:value="form.model_name"
              :disabled="isExternal"
              placeholder="e.g. BACnet Simulator"
            />
          </a-form-item>
        </a-col>
      </a-row>

      <a-form-item
        v-if="!device && !draftMode"
        label="From Template"
        :help="lockedEquipmentType && !templateOptions.length ? `No templates tagged for ${lockedEquipmentType} yet` : undefined"
      >
        <a-select
          v-model:value="chosenTemplateId"
          allow-clear
          show-search
          placeholder="No template"
          :options="templateOptions"
        />
      </a-form-item>

      <div
        v-if="selectedModel && (selectedModel.typeLabel || selectedModel.pics_url || selectedModelObjectTypes.length)"
        style="background:var(--surface-alt);border:1px solid var(--border);border-radius:6px;padding:10px 12px;margin-bottom:16px;font-size:12px"
      >
        <div v-if="selectedModel.typeLabel" style="margin-bottom:4px">
          <span style="color:var(--text-muted)">Device Profile:</span> {{ selectedModel.typeLabel }}
        </div>
        <div v-if="selectedModel.pics_url" style="margin-bottom:4px">
          <div style="color:var(--text-muted);white-space:nowrap">PICS Datasheet:</div>
          <a :href="selectedModel.pics_url" target="_blank" rel="noopener noreferrer" style="word-break:break-all">{{ selectedModel.pics_url }}</a>
        </div>
        <div v-if="selectedModelObjectTypes.length">
          <span style="color:var(--text-muted)">Supported Object Types:</span>
          <a-space :size="[4, 4]" wrap style="margin-left:4px">
            <a-tag v-for="t in selectedModelObjectTypes" :key="t.code" style="margin:0">{{ t.label }}</a-tag>
          </a-space>
        </div>
        <div v-if="selectedModel.pics_error" style="color:#faad14;margin-top:4px">
          PICS parsing note: {{ selectedModel.pics_error }}
        </div>
      </div>

      <a-form-item v-if="!lockedEquipmentId" label="Location">
        <a-tree-select
          v-model:value="form.location_id"
          :tree-data="locationTreeOptions"
          allow-clear
          tree-default-expand-all
          placeholder="Top level"
          style="width: 100%"
        />
      </a-form-item>

      <a-form-item v-if="!lockedEquipmentId" label="Controls" help="Select equipment controlled by this controller.">
        <a-select
          v-model:value="controlsEquipmentIds"
          mode="multiple"
          show-search
          allow-clear
          placeholder="No equipment selected"
          :options="equipmentOptionGroups"
          :filter-option="filterOption"
        />
      </a-form-item>


      <a-form-item v-if="!isExternal && device" label="Simulation Model">
        <div style="display:flex;align-items:center;gap:8px;border:1px solid var(--border);border-radius:6px;padding:8px 10px;background:var(--surface-alt)">
          <ExperimentOutlined style="color:#52c41a" />
          <div style="flex:1;min-width:0">
            <div style="font-size:12px;font-weight:600">{{ simulationSummary }}</div>
            <div style="font-size:11px;color:var(--text-muted)">Provider, model, parameters, defaults, and point mappings are configured in Simulation Model.</div>
          </div>
          <a-button size="small" @click="emit('simulation-model', device)">Configure</a-button>
        </div>
      </a-form-item>

      <a-form-item label="Event Notification Reception">
        <a-select
          v-model:value="form.can_receive_event_notifications"
          :options="[
            { value: null, label: `Auto (${inferredCanReceiveEvents ? 'can' : 'cannot'} receive, based on Equipment Type)` },
            { value: true, label: 'Yes — can receive Event Notifications' },
            { value: false, label: 'No — cannot receive Event Notifications' },
          ]"
        />
        <div style="font-size:11px;color:var(--text-muted);margin-top:4px">
          Real BACnet devices vary here (e.g. field controllers often can't receive alarms the way an operator workstation can) — this decides whether this device shows up as a viable Notification Class recipient.
        </div>
      </a-form-item>

      <a-collapse v-if="!isExternal" ghost style="margin-top:16px">
        <a-collapse-panel key="device-info" header="BACnet Device Info (advanced)">
          <div style="font-size:11px;color:var(--text-muted);margin-bottom:10px">
            Informational/cosmetic Device object properties some BACnet clients check — not enforced by the simulator itself.
          </div>
          <a-row :gutter="12">
            <a-col :span="12">
              <a-form-item label="Firmware Revision">
                <a-input v-model:value="form.firmware_revision" placeholder="N/A" />
              </a-form-item>
            </a-col>
            <a-col :span="12">
              <a-form-item label="Protocol Revision">
                <a-input-number v-model:value="form.protocol_revision" :min="0" :max="255" style="width:100%" />
              </a-form-item>
            </a-col>
          </a-row>
          <a-row :gutter="12">
            <a-col :span="12">
              <a-form-item label="Max APDU Length Accepted">
                <a-input-number v-model:value="form.max_apdu_length_accepted" :min="50" :max="1476" style="width:100%" />
              </a-form-item>
            </a-col>
            <a-col :span="12">
              <a-form-item label="Segmentation Supported">
                <a-select v-model:value="form.segmentation_supported">
                  <a-select-option v-for="s in meta.segmentation_options" :key="s" :value="s">{{ s }}</a-select-option>
                </a-select>
              </a-form-item>
            </a-col>
          </a-row>
        </a-collapse-panel>
      </a-collapse>

      <a-form-item
        v-if="!device && !draftMode"
        label="Import points from EDE (optional)"
        style="margin-top:16px;margin-bottom:0"
      >
        <input ref="edeFileInput" type="file" accept=".ede,.csv,text/csv" style="display:none" @change="onEdeFileChange" />
        <a-button block @click="edeFileInput?.click()">
          <template #icon><UploadOutlined /></template>
          {{ edeFileName || 'Choose EDE file…' }}
        </a-button>
      </a-form-item>

    </a-form>

    <template #footer>
      <div style="display:flex;justify-content:space-between;align-items:center">
        <div style="display:flex;align-items:center;gap:16px">
          <div v-if="!isExternal" style="display:flex;align-items:center;gap:8px">
            <a-switch v-model:checked="form.enabled" />
            <span style="font-size:12.5px;color:var(--text-secondary)">
              {{ form.enabled ? 'Enabled' : 'Disabled' }}
            </span>
          </div>
          <a-button v-if="device && !draftMode" danger :loading="deleting" @click="doDelete">
            {{ isExternal ? 'Remove' : 'Delete' }}
          </a-button>
        </div>
        <a-space>
          <a-button @click="emit('update:open', false)">Cancel</a-button>
          <a-button type="primary" :loading="loading" @click="save">
            {{ device ? 'Save' : 'Create' }}
          </a-button>
        </a-space>
      </div>
    </template>
  </a-drawer>
</template>
