import { Box, ScrollBox, Text, useInput } from '@astralnexus/ink'
import { useEffect, useState } from 'react'
import type { GatewayClient } from '../gatewayClient.js'
import { asRpcResult, rpcErrorMessage } from '../lib/rpc.js'

export type InspectionKind = 'plan' | 'evidence' | 'delivery' | 'changes'

export function RunInspection({gw, sessionId, kind, onClose}: {
  gw: GatewayClient; sessionId: string | null; kind: InspectionKind; onClose: () => void
}) {
  const [text, setText] = useState('正在读取当前会话的权威状态…')
  useInput((_input, key) => { if (key.escape) onClose() })
  useEffect(() => {
    let active = true
    let timer: ReturnType<typeof setTimeout>
    async function refresh() {
      try {
        const response = await gw.request<{text: string}>('inspection.get', {session_id: sessionId, kind})
        const result = asRpcResult<{text: string}>(response)
        if (active) setText(result?.text ?? '状态不可用；未保留旧成功结论。')
      } catch (error) {
        if (active) setText(`状态读取失败：${rpcErrorMessage(error)}`)
      } finally {
        if (active) timer = setTimeout(refresh, 3000)
      }
    }
    void refresh()
    return () => {active = false; clearTimeout(timer)}
  }, [gw, sessionId, kind])
  return <Box flexDirection="column" flexGrow={1}>
    <Text bold>{kind === 'plan' ? '计划与阻塞' : kind === 'evidence' ? '来源与验收证据' : kind === 'changes' ? '文件变更审查' : '最终交付'} · Esc 返回</Text>
    <ScrollBox flexGrow={1}><Text>{text}</Text></ScrollBox>
  </Box>
}
