<script setup lang="ts">
import { ref, computed, watch, onUnmounted } from 'vue'
import { message } from 'ant-design-vue'
import { CaretRightOutlined, PauseOutlined, StopOutlined } from '@ant-design/icons-vue'
import { api } from '../api'
import type { Device, ReplayRecording, ReplayPlaybackState } from '../types'

const props = defineProps<{ open: boolean; device: Device | null }>()
const emit = defineEmits<{ 'update:open': [v: boolean] }>()

const SPEEDS = [0.5, 1, 2, 5, 10]

const loading = ref(false)
const recording = ref<ReplayRecording | null>(null)
const state = ref<ReplayPlaybackState | null>(null)

// Polls playback state while the drawer is open so the seek slider tracks
// an actively-playing position -- there is no WS push for this (playback
// position is in-memory-only server state, not persisted/broadcast).
let pollTimer: ReturnType<typeof setInterval> | null = null

function stopPolling() {
  if (pollTimer) { clearInterval(pollTimer); pollTimer = null }
}

async function refreshState() {
  if (!props.device) return
  try {
    state.value = await api.replayPlayback.state(props.device.id)
  } catch (e: unknown) {
    message.error((e as Error).message ?? 'Failed to load playback state')
  }
}

async function load() {
  if (!props.device || props.device.replay_recording_id == null) return
  loading.value = true
  try {
    const [rec] = await Promise.all([
      api.replayRecordings.get(props.device.replay_recording_id),
      refreshState(),
    ])
    recording.value = rec
  } catch (e: unknown) {
    message.error((e as Error).message ?? 'Failed to load recording')
  } finally {
    loading.value = false
  }
}

watch(() => props.open, (v) => {
  stopPolling()
  if (v) {
    load()
    pollTimer = setInterval(refreshState, 500)
  }
})
onUnmounted(stopPolling)

const seekMin = computed(() => recording.value?.sample_index_min ?? 0)
const seekMax = computed(() => recording.value?.sample_index_max ?? 0)

async function play() {
  if (!props.device) return
  try { state.value = await api.replayPlayback.play(props.device.id) }
  catch (e: unknown) { message.error((e as Error).message ?? 'Failed to play') }
}
async function pause() {
  if (!props.device) return
  try { state.value = await api.replayPlayback.pause(props.device.id) }
  catch (e: unknown) { message.error((e as Error).message ?? 'Failed to pause') }
}
async function stop() {
  if (!props.device) return
  try { state.value = await api.replayPlayback.stop(props.device.id) }
  catch (e: unknown) { message.error((e as Error).message ?? 'Failed to stop') }
}
async function onSeek(value: number) {
  if (!props.device) return
  try { state.value = await api.replayPlayback.seek(props.device.id, value) }
  catch (e: unknown) { message.error((e as Error).message ?? 'Failed to seek') }
}
async function onLoopChange(loop: boolean) {
  if (!props.device) return
  try { state.value = await api.replayPlayback.setLoop(props.device.id, loop) }
  catch (e: unknown) { message.error((e as Error).message ?? 'Failed to update loop') }
}
async function onSpeedChange(speed: number) {
  if (!props.device) return
  try { state.value = await api.replayPlayback.setSpeed(props.device.id, speed) }
  catch (e: unknown) { message.error((e as Error).message ?? 'Failed to update speed') }
}
</script>

<template>
  <a-drawer
    :title="device ? `Replay Playback — ${device.name}` : 'Replay Playback'"
    :open="open"
    width="440"
    @close="emit('update:open', false)"
  >
    <a-spin :spinning="loading">
      <div v-if="!recording" style="text-align:center;color:var(--text-placeholder);padding:40px 0;font-size:13px">
        No recording linked to this device
      </div>
      <template v-else>
        <div style="font-size:12px;color:var(--text-muted);margin-bottom:16px">
          Driven by <b>{{ recording.name }}</b> · {{ recording.sample_count }} samples
          · every {{ recording.sample_interval_seconds }}s
        </div>

        <div style="display:flex;justify-content:center;gap:10px;margin-bottom:20px">
          <a-button
            v-if="state?.status !== 'playing'"
            type="primary" shape="circle" size="large" title="Play" @click="play"
          >
            <template #icon><CaretRightOutlined /></template>
          </a-button>
          <a-button v-else shape="circle" size="large" title="Pause" @click="pause">
            <template #icon><PauseOutlined /></template>
          </a-button>
          <a-button shape="circle" size="large" title="Stop (returns to first sample)" @click="stop">
            <template #icon><StopOutlined /></template>
          </a-button>
        </div>

        <a-form-item label="Position" style="margin-bottom:16px">
          <a-slider
            :min="seekMin"
            :max="seekMax"
            :value="state?.current_sample_index ?? seekMin"
            :tip-formatter="(v: number) => `Sample ${v}`"
            @change="onSeek"
          />
        </a-form-item>

        <a-row :gutter="12">
          <a-col :span="12">
            <a-form-item label="Speed">
              <a-select :value="state?.speed ?? 1" @change="onSpeedChange" style="width:100%">
                <a-select-option v-for="s in SPEEDS" :key="s" :value="s">{{ s }}x</a-select-option>
              </a-select>
            </a-form-item>
          </a-col>
          <a-col :span="12">
            <a-form-item label="Loop">
              <a-switch :checked="state?.loop ?? false" @change="onLoopChange" />
            </a-form-item>
          </a-col>
        </a-row>

        <div style="font-size:12px;color:var(--text-secondary);text-align:center">
          Status: <b>{{ state?.status ?? 'stopped' }}</b>
        </div>
      </template>
    </a-spin>

    <template #footer>
      <a-button @click="emit('update:open', false)">Close</a-button>
    </template>
  </a-drawer>
</template>
