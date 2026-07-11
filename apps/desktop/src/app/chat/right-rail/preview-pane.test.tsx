import { act, cleanup, render } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import { $connection } from '@/store/session'

import { embeddedPreviewFrameSandbox, isAceEmbeddedRenderer, PreviewPane } from './preview-pane'

describe('PreviewPane console state', () => {
  beforeEach(() => {
    window.history.replaceState({}, '', '/')
    vi.stubGlobal('requestAnimationFrame', (callback: FrameRequestCallback) =>
      window.setTimeout(() => callback(Date.now()), 0)
    )
    vi.stubGlobal('cancelAnimationFrame', (id: number) => window.clearTimeout(id))
  })

  afterEach(() => {
    cleanup()
    $connection.set(null)
    vi.unstubAllGlobals()
  })

  it('does not watch backend-only remote filesystem previews locally', () => {
    const watchPreviewFile = vi.fn(async () => ({ id: 'watch-1', path: '/remote/file.txt' }))
    const onPreviewFileChanged = vi.fn(() => vi.fn())
    $connection.set({ mode: 'remote' } as never)
    vi.stubGlobal('window', {
      ...window,
      hermesDesktop: {
        onPreviewFileChanged,
        watchPreviewFile
      }
    })

    render(
      <PreviewPane
        setTitlebarToolGroup={vi.fn()}
        target={{
          kind: 'file',
          label: 'file.txt',
          path: '/remote/file.txt',
          previewKind: 'text',
          source: '/remote/file.txt',
          url: 'file:///remote/file.txt'
        }}
      />
    )

    expect(watchPreviewFile).not.toHaveBeenCalled()
    expect(onPreviewFileChanged).not.toHaveBeenCalled()
  })

  it('does not rebuild the pane titlebar group for streamed console logs', () => {
    const setTitlebarToolGroup = vi.fn()

    const rendered = render(
      <PreviewPane
        setTitlebarToolGroup={setTitlebarToolGroup}
        target={{
          kind: 'url',
          label: 'Preview',
          source: 'http://localhost:5174',
          url: 'http://localhost:5174'
        }}
      />
    )

    const initialCalls = setTitlebarToolGroup.mock.calls.length
    const webview = rendered.container.querySelector('webview')

    expect(webview).toBeInstanceOf(HTMLElement)

    act(() => {
      webview?.dispatchEvent(
        Object.assign(new Event('console-message'), {
          level: 0,
          message: 'streamed log line',
          sourceId: 'http://localhost:5174/src/main.tsx'
        })
      )
    })

    expect(setTitlebarToolGroup).toHaveBeenCalledTimes(initialCalls)
  })

  it('uses a sandboxed iframe for Ace-embedded local HTML previews', () => {
    window.history.replaceState({}, '', '/?aceProfile=king')

    const rendered = render(
      <PreviewPane
        target={{
          kind: 'file',
          label: 'demo.html',
          path: '/tmp/demo.html',
          previewKind: 'html',
          source: '/tmp/demo.html',
          url: 'file:///tmp/demo.html'
        }}
      />
    )
    const frame = rendered.container.querySelector('iframe')

    expect(frame).toBeInstanceOf(HTMLIFrameElement)
    expect(rendered.container.querySelector('webview')).toBeNull()
    expect(frame?.getAttribute('sandbox')).toContain('allow-scripts')
    expect(frame?.getAttribute('sandbox')).not.toContain('allow-same-origin')
  })

  it('detects Ace embedding and preserves HTTP preview origins only', () => {
    expect(isAceEmbeddedRenderer('?aceProfile=king')).toBe(true)
    expect(isAceEmbeddedRenderer('?capellaProfile=king')).toBe(true)
    expect(isAceEmbeddedRenderer('')).toBe(false)
    expect(embeddedPreviewFrameSandbox('file:///tmp/demo.html')).not.toContain('allow-same-origin')
    expect(embeddedPreviewFrameSandbox('http://localhost:5173')).toContain('allow-same-origin')
  })
})
