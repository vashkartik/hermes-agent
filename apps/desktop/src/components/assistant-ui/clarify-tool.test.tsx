import type { ToolCallMessagePartProps } from '@assistant-ui/react'
import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import type { ReactNode } from 'react'
import { afterEach, describe, expect, it, vi } from 'vitest'

import type { HermesGateway } from '@/hermes'
import { I18nProvider } from '@/i18n'
import { clearClarifyRequest, setClarifyRequest } from '@/store/clarify'
import { $gateway } from '@/store/gateway'
import { $activeSessionId } from '@/store/session'

import { ClarifyTool, readClarifyResult } from './clarify-tool'

vi.mock('@/lib/haptics', () => ({
  triggerHaptic: vi.fn()
}))

let mockMessageRunning = true

// ClarifyTool reads the live message's running state via useAuiState, which
// needs a full thread/message provider tree. Most tests answer the selector
// with a running message instead of mounting a whole Thread; the remount test
// flips it to complete to cover backend-authoritative recovery.
vi.mock('@assistant-ui/react', async importOriginal => {
  const mod = await importOriginal<Record<string, unknown>>()

  return {
    ...mod,
    useAuiState: (selector: (state: unknown) => unknown) =>
      selector({
        message: { status: { type: mockMessageRunning ? 'running' : 'complete' } },
        thread: { isRunning: mockMessageRunning }
      })
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

function renderClarify(ui: ReactNode) {
  return render(
    <I18nProvider configClient={null} initialLocale="en">
      {ui}
    </I18nProvider>
  )
}

function settledClarifyProps(
  args: ToolCallMessagePartProps['args'],
  result: ToolCallMessagePartProps['result'],
  toolCallId: string
): ToolCallMessagePartProps {
  return {
    addResult: vi.fn(),
    args,
    argsText: JSON.stringify(args),
    isError: false,
    result,
    resume: vi.fn(),
    status: { type: 'complete' },
    toolCallId,
    toolName: 'clarify',
    type: 'tool-call'
  }
}

afterEach(() => {
  cleanup()
  mockMessageRunning = true
  clearClarifyRequest()
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

  it('reopens a backend-pending question after the transcript remounts as complete', () => {
    mockGateway()
    mockMessageRunning = false
    $activeSessionId.set('sess-1')
    setClarifyRequest({
      requestId: 'req-remount',
      question: 'Which option should I keep?',
      choices: ['Alpha', 'Beta'],
      sessionId: 'sess-1'
    })

    renderPendingClarify({ question: 'Which option should I keep?', choices: ['Alpha', 'Beta'] })

    expect(screen.getByText('Which option should I keep?')).toBeTruthy()
    expect(screen.getByRole('button', { name: /Alpha$/ })).toBeTruthy()
    expect(screen.getByRole('button', { name: /Beta$/ })).toBeTruthy()
  })
})

describe('readClarifyResult', () => {
  it('reads question + user_response from the tool JSON payload', () => {
    expect(
      readClarifyResult({
        question: 'Which target?',
        choices_offered: ['staging', 'prod'],
        user_response: 'staging'
      })
    ).toEqual({
      question: 'Which target?',
      answer: 'staging',
      error: undefined
    })
  })

  it('parses a JSON string result the same way as an object', () => {
    expect(
      readClarifyResult(
        JSON.stringify({
          question: 'Ship it?',
          user_response: 'yes'
        })
      )
    ).toEqual({
      question: 'Ship it?',
      answer: 'yes',
      error: undefined
    })
  })

  it('keeps an empty user_response so Skip can render as skipped', () => {
    expect(readClarifyResult({ question: 'Ok?', user_response: '' })).toEqual({
      question: 'Ok?',
      answer: '',
      error: undefined
    })
  })
})

describe('ClarifyTool settled view', () => {
  it('keeps the question and answer visible after the tool completes', () => {
    renderClarify(
      <ClarifyTool
        {...settledClarifyProps(
          { question: 'Which deployment target?', choices: ['staging', 'prod'] },
          {
            question: 'Which deployment target?',
            choices_offered: ['staging', 'prod'],
            user_response: 'staging'
          },
          'clarify-1'
        )}
      />
    )

    expect(screen.getByText('Which deployment target?')).toBeTruthy()
    expect(screen.getByText('staging')).toBeTruthy()
    expect(document.querySelector('[data-clarify-settled]')).toBeTruthy()
    expect(document.querySelector('[data-clarify-answer]')?.textContent).toBe('staging')
  })

  it('labels an empty response as Skipped', () => {
    renderClarify(
      <ClarifyTool
        {...settledClarifyProps({ question: 'Anything else?' }, { question: 'Anything else?', user_response: '' }, 'clarify-2')}
      />
    )

    expect(screen.getByText('Anything else?')).toBeTruthy()
    expect(screen.getByText('Skipped')).toBeTruthy()
  })
})
