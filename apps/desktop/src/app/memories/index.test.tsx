import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { MemoryRouter } from 'react-router-dom'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import type { MemoryFact, MemoryFactsResponse } from '@/types/hermes'

const getMemoryFacts = vi.fn()

vi.mock('@/hermes', () => ({
  getMemoryFacts: (params?: unknown) => getMemoryFacts(params)
}))

function fact(overrides: Partial<MemoryFact> = {}): MemoryFact {
  return {
    id: 1,
    content: 'User prefers dark editor themes',
    category: 'user_pref',
    tags: 'editor,theme',
    trust_score: 0.8,
    retrieval_count: 4,
    helpful_count: 2,
    created_at: '2026-06-01 10:00:00',
    updated_at: '2026-06-08 10:00:00',
    ...overrides
  }
}

function response(overrides: Partial<MemoryFactsResponse> = {}): MemoryFactsResponse {
  return {
    facts: [
      fact(),
      fact({
        category: 'project',
        content: 'Deploy process uses GitHub Actions',
        helpful_count: 0,
        id: 2,
        retrieval_count: 9,
        tags: '',
        trust_score: 0.55
      })
    ],
    total: 2,
    categories: { project: 1, user_pref: 1 },
    provider: 'holographic',
    db_mtime: 1_780_000_000,
    ...overrides
  }
}

function renderMemories(initialEntry = '/memories') {
  return import('./index').then(({ MemoriesView }) =>
    render(
      <MemoryRouter initialEntries={[initialEntry]}>
        <MemoriesView />
      </MemoryRouter>
    )
  )
}

describe('MemoriesView', () => {
  beforeEach(() => {
    getMemoryFacts.mockReset()
    getMemoryFacts.mockResolvedValue(response())
  })

  afterEach(() => {
    cleanup()
  })

  it('renders fact cards with trust, counters, tags, and category chips', async () => {
    await renderMemories()

    expect(await screen.findByText('User prefers dark editor themes')).toBeTruthy()
    expect(screen.getByText('Deploy process uses GitHub Actions')).toBeTruthy()
    // Trust percentages + recall/helpful counters per card.
    expect(screen.getByText('80%')).toBeTruthy()
    expect(screen.getByText('55%')).toBeTruthy()
    expect(screen.getByText('4 recalls')).toBeTruthy()
    expect(screen.getByText('2 helpful')).toBeTruthy()
    // Tag chips.
    expect(screen.getByText('editor')).toBeTruthy()
    // Category chips from the categories map (chip + card header both render
    // the pretty name, so assert via the chip's button role).
    expect(screen.getByRole('button', { name: /project/i })).toBeTruthy()
    expect(screen.getByRole('button', { name: /user pref/i })).toBeTruthy()
    // Header total.
    expect(screen.getByText('2 facts')).toBeTruthy()
  })

  it('filters by category when a chip is clicked', async () => {
    await renderMemories()
    await screen.findByText('User prefers dark editor themes')

    fireEvent.click(screen.getByRole('button', { name: /project/i }))

    await waitFor(() => {
      expect(getMemoryFacts).toHaveBeenLastCalledWith(expect.objectContaining({ category: 'project' }))
    })
  })

  it('debounces the search box into a ?q= fetch', async () => {
    await renderMemories()
    await screen.findByText('User prefers dark editor themes')

    fireEvent.change(screen.getByPlaceholderText('Search memories...'), { target: { value: 'deploy' } })

    await waitFor(() => {
      expect(getMemoryFacts).toHaveBeenLastCalledWith(expect.objectContaining({ q: 'deploy' }))
    })
  })

  it('seeds the search from an initial ?q= route param', async () => {
    await renderMemories('/memories?q=themes')

    await waitFor(() => {
      expect(getMemoryFacts).toHaveBeenCalledWith(expect.objectContaining({ q: 'themes' }))
    })
  })

  it('shows the error state when the store is unavailable', async () => {
    getMemoryFacts.mockResolvedValue(
      response({ categories: {}, error: 'memory store not found at /tmp/nope.db', facts: [], total: 0 })
    )

    await renderMemories()

    expect(await screen.findByText('Memory store unavailable')).toBeTruthy()
    expect(screen.getByText('memory store not found at /tmp/nope.db')).toBeTruthy()
  })

  it('shows the empty state for a pristine store', async () => {
    getMemoryFacts.mockResolvedValue(response({ categories: {}, facts: [], total: 0 }))

    await renderMemories()

    expect(await screen.findByText('No memories yet')).toBeTruthy()
  })

  it('offers Load more when more facts exist server-side', async () => {
    getMemoryFacts.mockResolvedValue(response({ total: 150 }))

    await renderMemories()
    await screen.findByText('User prefers dark editor themes')

    fireEvent.click(screen.getByRole('button', { name: 'Load more' }))

    await waitFor(() => {
      expect(getMemoryFacts).toHaveBeenLastCalledWith(expect.objectContaining({ offset: 2 }))
    })
  })
})
