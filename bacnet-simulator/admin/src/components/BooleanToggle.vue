<script setup lang="ts">
/**
 * Generic, protocol-agnostic on/off toggle for an editable boolean value.
 *
 * Deliberately knows nothing about BACnet, FMI/Modelica, object types, units,
 * or 0/1 numeric representations -- it only ever takes/emits a real boolean.
 * Callers own their own representation conversion (e.g.
 * SimulationModelDrawer.vue converts to/from a 0/1 number at its own call
 * site; BACnet callers already hold a real boolean and bind directly).
 *
 * Wraps the same a-radio-group/a-radio-button visual pattern already used
 * (inline, duplicated) for BACnet binary editing across ObjectsPanel.vue and
 * ObjectDrawer.vue -- not a-segmented, which this app uses for a different
 * concept (Constant/Point/Aggregate source selection).
 */
withDefaults(defineProps<{
  value: boolean
  onLabel?: string
  offLabel?: string
  disabled?: boolean
}>(), {
  onLabel: 'ON',
  offLabel: 'OFF',
  disabled: false,
})

const emit = defineEmits<{
  'update:value': [value: boolean]
}>()

function handleChange(value: boolean) {
  emit('update:value', value)
}
</script>

<template>
  <a-radio-group
    class="boolean-toggle"
    :value="value"
    :disabled="disabled"
    @update:value="handleChange"
  >
    <a-radio-button :value="true">{{ onLabel }}</a-radio-button>
    <a-radio-button :value="false">{{ offLabel }}</a-radio-button>
  </a-radio-group>
</template>

<style scoped>
.boolean-toggle {
  display: flex;
  width: 100%;
}

.boolean-toggle :deep(.ant-radio-button-wrapper) {
  flex: 1;
  text-align: center;
}
</style>
