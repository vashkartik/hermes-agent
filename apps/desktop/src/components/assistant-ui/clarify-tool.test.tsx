import type { ToolCallMessagePartProps } from '@assistant-ui/react'
import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { afterEach, describe, expect, it, vi } from 'vitest'

import type { HermesGateway } from '@/hermes'
import { setClarifyRequest } from '@/store/clarify'
import { $gateway } from '@/store/gateway'
import { $activeSessionId } from '@/store/session'

import { ClarifyTool } from './clarify-tool'

vi.mock('@/lib/haptics', () => ({
  triggerHaptic: vi.fn()
}))

// ClarifyTool gates the interactive panel on the live message's running state
// via useAuiState, which needs a full thread/message provider tree. These
// tests exercise the yes/no-choice logic directly, so answer the selector
// with a running message instead of mounting a whole Thread.
vi.mock('@assistant-ui/react', async importOriginal => {
  const mod = await importOriginal<typeof import('@assistant-ui/react')>()

  return {
    ...mod,
    useAuiState: (selector: (state: unknown) => unknown) =>
      selector({ message: { status: { type: 'running' } }, thread: { isRunning: true } })
  }
})

function mockGateway() {
  const request = vi.fn().mockResolvedValue({ ok: true })
  $gateway.set({ request } as unknown as HermesGateway)

  return request
}

function renderPendingClarify(args: Record<string, unknown>) {
  const props = {
    args,
    result: undefined,
    toolName: 'clarify',
    type: 'tool-call'
  } as unknown as ToolCallMessagePartProps

  return render(<ClarifyTool {...props} />)
}

afterEach(() => {
  cleanup()
  $activeSessionId.set(null)
  $gateway.set(null)
})

describe('ClarifyTool', () => {
  it('renders tappable Yes/No choices for yes-no clarify prompts without explicit choices', async () => {
    const request = mockGateway()
    $activeSessionId.set('sess-1')
    setClarifyRequest({
      requestId: 'req-1',
      question: 'PR #567 is green. Do you want me to owner-bypass via GitHub admin/squash merge?',
      choices: null,
      sessionId: 'sess-1'
    })

    renderPendingClarify({
      question: 'PR #567 is green. Do you want me to owner-bypass via GitHub admin/squash merge?',
      choices: null
    })

    expect(screen.getByRole('button', { name: /Yes$/ })).toBeTruthy()
    expect(screen.getByRole('button', { name: /No$/ })).toBeTruthy()
    // The tappable chips lead; freeform stays available only as the "Other" escape hatch.
    expect((screen.getByRole('textbox') as HTMLTextAreaElement).placeholder).toMatch(/other/i)

    // Picking a choice selects it; Continue confirms and sends the answer.
    fireEvent.click(screen.getByRole('button', { name: /No$/ }))
    fireEvent.click(screen.getByRole('button', { name: /Continue/ }))

    await waitFor(() => {
      expect(request).toHaveBeenCalledWith('clarify.respond', {
        request_id: 'req-1',
        answer: 'No'
      })
    })
  })

  it('keeps open-ended clarify prompts in freeform input mode', () => {
    mockGateway()
    $activeSessionId.set('sess-1')
    setClarifyRequest({
      requestId: 'req-2',
      question: 'Which deployment target should I use?',
      choices: null,
      sessionId: 'sess-1'
    })

    renderPendingClarify({ question: 'Which deployment target should I use?', choices: null })

    expect(screen.queryByRole('button', { name: /Yes$/ })).toBeNull()
    expect(screen.queryByRole('button', { name: /No$/ })).toBeNull()
    expect(screen.getByRole('textbox')).toBeTruthy()
  })
})
