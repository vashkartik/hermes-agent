import { AnimatePresence, motion } from 'motion/react'
import type * as React from 'react'
import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import { useLocation, useNavigate } from 'react-router-dom'

import { Button } from '@/components/ui/button'
import { Codicon } from '@/components/ui/codicon'
import { ErrorIcon } from '@/components/ui/error-state'
import { Skeleton } from '@/components/ui/skeleton'
import { TextTab, TextTabMeta } from '@/components/ui/text-tab'
import { getMemoryFacts } from '@/hermes'
import { type Translations, useI18n } from '@/i18n'
import { cn } from '@/lib/utils'
import type { MemoryFact, MemoryFactsResponse } from '@/types/hermes'

import { useRefreshHotkey } from '../hooks/use-refresh-hotkey'
import { PAGE_INSET_X } from '../layout-constants'
import { PageSearchShell } from '../page-search-shell'
import { prettyName } from '../settings/helpers'

// Server page size: facts accumulate via "Load more" (offset pagination).
const PAGE_SIZE = 100
// Debounce for wiring the search box to ?q= (and the FTS-backed fetch).
const SEARCH_DEBOUNCE_MS = 250

const CARD_SPRING = { damping: 26, stiffness: 380, type: 'spring' as const }

// SQLite CURRENT_TIMESTAMP is 'YYYY-MM-DD HH:MM:SS' in UTC, no zone suffix.
function parseStoreTimestamp(value: string): Date | null {
  if (!value) {
    return null
  }

  const date = new Date(value.includes('T') ? value : `${value.replace(' ', 'T')}Z`)

  return Number.isNaN(date.getTime()) ? null : date
}

function relativeTime(value: string, m: Translations['memories']): string {
  const date = parseStoreTimestamp(value)

  if (!date) {
    return ''
  }

  const minutes = Math.floor((Date.now() - date.getTime()) / 60_000)

  if (minutes < 1) {
    return m.justNow
  }

  if (minutes < 60) {
    return m.minutesAgo(minutes)
  }

  if (minutes < 60 * 24) {
    return m.hoursAgo(Math.floor(minutes / 60))
  }

  return m.daysAgo(Math.floor(minutes / (60 * 24)))
}

function splitTags(tags: string): string[] {
  return tags
    .split(',')
    .map(tag => tag.trim())
    .filter(Boolean)
    .slice(0, 6)
}

type MemoriesViewProps = React.ComponentProps<'section'>

export function MemoriesView(props: MemoriesViewProps) {
  const { t } = useI18n()
  const m = t.memories
  const { hash, pathname, search } = useLocation()
  const navigate = useNavigate()

  // Search input is immediate; `query` is the debounced value that drives the
  // ?q= param and the FTS-backed fetch.
  const [input, setInput] = useState(() => new URLSearchParams(search).get('q') ?? '')
  const [query, setQuery] = useState(input)
  const [category, setCategory] = useState<string | null>(null)
  const [facts, setFacts] = useState<MemoryFact[] | null>(null)

  const [meta, setMeta] = useState<Pick<MemoryFactsResponse, 'categories' | 'error' | 'provider' | 'total'> | null>(
    null
  )

  const [refreshing, setRefreshing] = useState(false)
  const [loadingMore, setLoadingMore] = useState(false)
  const requestRef = useRef(0)

  useEffect(() => {
    const timer = window.setTimeout(() => setQuery(input), SEARCH_DEBOUNCE_MS)

    return () => window.clearTimeout(timer)
  }, [input])

  // Keep ?q= in the URL so a search survives refresh/back, like page tabs do.
  useEffect(() => {
    const params = new URLSearchParams(search)
    const current = params.get('q') ?? ''

    if (current === query.trim()) {
      return
    }

    if (query.trim()) {
      params.set('q', query.trim())
    } else {
      params.delete('q')
    }

    const qs = params.toString()
    navigate({ hash, pathname, search: qs ? `?${qs}` : '' }, { replace: true })
  }, [hash, navigate, pathname, query, search])

  const fetchFacts = useCallback(
    async (offset: number) => {
      const requestId = ++requestRef.current
      const append = offset > 0

      if (append) {
        setLoadingMore(true)
      } else {
        setRefreshing(true)
      }

      try {
        const result = await getMemoryFacts({
          category: category ?? undefined,
          limit: PAGE_SIZE,
          offset,
          q: query
        })

        if (requestId !== requestRef.current) {
          return
        }

        setMeta({ categories: result.categories, error: result.error, provider: result.provider, total: result.total })
        setFacts(current => (append && current ? [...current, ...result.facts] : result.facts))
      } catch (err) {
        if (requestId === requestRef.current) {
          setMeta({ categories: {}, error: err instanceof Error ? err.message : m.loadFailed, provider: '', total: 0 })
          setFacts(current => (append ? current : []))
        }
      } finally {
        if (requestId === requestRef.current) {
          setRefreshing(false)
          setLoadingMore(false)
        }
      }
    },
    [category, m.loadFailed, query]
  )

  useEffect(() => {
    void fetchFacts(0)
  }, [fetchFacts])

  const refresh = useCallback(() => void fetchFacts(0), [fetchFacts])
  useRefreshHotkey(refresh)

  const categories = useMemo(
    () =>
      Object.entries(meta?.categories ?? {})
        .map(([key, count]) => ({ count, key }))
        .sort((a, b) => b.count - a.count || a.key.localeCompare(b.key)),
    [meta?.categories]
  )

  const totalFacts = useMemo(() => categories.reduce((sum, entry) => sum + entry.count, 0), [categories])
  const maxCategoryCount = categories[0]?.count ?? 0
  const filtered = Boolean(query.trim()) || category !== null
  const loading = facts === null
  const storeError = meta?.error

  return (
    <PageSearchShell
      {...props}
      filters={
        categories.length > 0 ? (
          <>
            <TextTab active={category === null} onClick={() => setCategory(null)}>
              {m.all} <TextTabMeta>{totalFacts}</TextTabMeta>
            </TextTab>
            {categories.map(entry => (
              <TextTab
                active={category === entry.key}
                key={entry.key}
                onClick={() => setCategory(current => (current === entry.key ? null : entry.key))}
              >
                {prettyName(entry.key)} <TextTabMeta>{entry.count}</TextTabMeta>
              </TextTab>
            ))}
          </>
        ) : undefined
      }
      onSearchChange={setInput}
      searchHidden={!loading && !storeError && totalFacts === 0 && !filtered}
      searchPlaceholder={m.search}
      searchTrailingAction={
        <Button
          aria-label={refreshing ? m.refreshing : m.refresh}
          className="text-(--ui-text-tertiary) hover:bg-transparent hover:text-foreground"
          disabled={refreshing}
          onClick={refresh}
          size="icon-xs"
          title={refreshing ? m.refreshing : m.refresh}
          type="button"
          variant="ghost"
        >
          <Codicon name="refresh" size="0.875rem" spinning={refreshing} />
        </Button>
      }
      searchValue={input}
      tabs={
        <div className="flex min-w-0 items-center gap-2">
          <span className="text-[length:var(--conversation-caption-font-size)] font-medium text-foreground">
            {m.title}
          </span>
          <span className="text-[0.72em] font-normal text-(--ui-text-tertiary)">
            {filtered && meta ? m.matchingCount(meta.total) : m.factsCount(totalFacts)}
          </span>
        </div>
      }
    >
      <div className={cn('h-full overflow-y-auto py-3', PAGE_INSET_X)}>
        {loading ? (
          <MemoriesSkeleton />
        ) : storeError && totalFacts === 0 ? (
          <ErrorState description={storeError} onRetry={refresh} title={m.errorTitle} />
        ) : facts.length === 0 && !filtered ? (
          <EmptyState description={m.emptyDesc} title={m.emptyTitle} />
        ) : (
          <div className="space-y-3">
            {categories.length > 1 && <CategoryStatsStrip categories={categories} max={maxCategoryCount} />}

            {facts.length === 0 ? (
              <EmptyState description={m.noMatchDesc} title={m.noMatchTitle} />
            ) : (
              <motion.div
                className="grid grid-cols-[repeat(auto-fill,minmax(17rem,1fr))] items-start gap-2"
                layout
              >
                <AnimatePresence initial mode="popLayout">
                  {facts.map((fact, index) => (
                    <FactCard fact={fact} index={index} key={fact.id} m={m} />
                  ))}
                </AnimatePresence>
              </motion.div>
            )}

            {meta && facts.length < meta.total && (
              <div className="flex justify-center pb-2 pt-1">
                <Button
                  disabled={loadingMore}
                  onClick={() => void fetchFacts(facts.length)}
                  size="xs"
                  type="button"
                  variant="textStrong"
                >
                  <Codicon name="chevron-down" size="0.75rem" spinning={loadingMore} />
                  {m.loadMore}
                </Button>
              </div>
            )}
          </div>
        )}
      </div>
    </PageSearchShell>
  )
}

// Compact per-category distribution: tiny animated bars, widths relative to
// the largest category so the strip reads as a sparkline of the store.
function CategoryStatsStrip({ categories, max }: { categories: { count: number; key: string }[]; max: number }) {
  return (
    <div className="flex flex-wrap items-end gap-x-4 gap-y-1.5 rounded-lg border border-(--ui-stroke-tertiary) bg-(--ui-chat-bubble-background) px-3 py-2">
      {categories.map((entry, index) => (
        <div className="min-w-[5.5rem]" key={entry.key}>
          <div className="flex items-baseline justify-between gap-2">
            <span className="text-[0.625rem] uppercase tracking-[0.08em] text-(--ui-text-tertiary)">
              {prettyName(entry.key)}
            </span>
            <span className="font-mono text-[0.625rem] text-(--ui-text-secondary)">{entry.count}</span>
          </div>
          <div className="mt-1 h-1 overflow-hidden rounded-full bg-(--ui-bg-quinary)">
            <motion.div
              animate={{ width: `${Math.max(6, Math.round((entry.count / Math.max(1, max)) * 100))}%` }}
              className="h-full rounded-full bg-primary/60"
              initial={{ width: 0 }}
              transition={{ delay: index * 0.05, duration: 0.45, ease: 'easeOut' }}
            />
          </div>
        </div>
      ))}
    </div>
  )
}

function FactCard({ fact, index, m }: { fact: MemoryFact; index: number; m: Translations['memories'] }) {
  const tags = splitTags(fact.tags)
  const trustPct = Math.round(Math.min(1, Math.max(0, fact.trust_score)) * 100)

  return (
    <motion.article
      animate={{ opacity: 1, scale: 1, y: 0 }}
      className="flex h-full flex-col gap-2 rounded-lg border border-(--ui-stroke-tertiary) bg-(--ui-chat-bubble-background) p-2.5"
      exit={{ opacity: 0, scale: 0.97, transition: { duration: 0.12 } }}
      initial={{ opacity: 0, scale: 0.98, y: 8 }}
      layout
      transition={{ ...CARD_SPRING, delay: Math.min(index, 12) * 0.025 }}
    >
      <div className="flex items-center justify-between gap-2">
        <span className="truncate text-[0.625rem] uppercase tracking-[0.08em] text-(--ui-text-tertiary)">
          {prettyName(fact.category || 'general')}
        </span>
        <span className="shrink-0 text-[0.625rem] text-(--ui-text-tertiary)">{relativeTime(fact.updated_at, m)}</span>
      </div>

      <p className="min-w-0 flex-1 whitespace-pre-wrap break-words text-[length:var(--conversation-caption-font-size)] leading-(--conversation-caption-line-height) text-foreground">
        {fact.content}
      </p>

      {tags.length > 0 && (
        <div className="flex flex-wrap gap-1">
          {tags.map(tag => (
            <span
              className="rounded-md bg-(--ui-bg-quinary) px-1.5 py-0.5 font-mono text-[0.65rem] text-(--ui-text-tertiary)"
              key={tag}
            >
              {tag}
            </span>
          ))}
        </div>
      )}

      <div className="space-y-1.5">
        <div className="flex items-center gap-2">
          <span className="w-9 shrink-0 text-[0.625rem] uppercase tracking-[0.08em] text-(--ui-text-tertiary)">
            {m.trust}
          </span>
          <div className="h-1 flex-1 overflow-hidden rounded-full bg-(--ui-bg-quinary)">
            <motion.div
              animate={{ width: `${trustPct}%` }}
              className={cn('h-full rounded-full', trustPct >= 50 ? 'bg-primary/70' : 'bg-amber-500/70')}
              initial={{ width: 0 }}
              transition={{ delay: Math.min(index, 12) * 0.025 + 0.15, duration: 0.5, ease: 'easeOut' }}
            />
          </div>
          <span className="w-8 shrink-0 text-right font-mono text-[0.625rem] text-(--ui-text-secondary)">
            {trustPct}%
          </span>
        </div>

        <div className="flex items-center gap-3 text-[0.625rem] text-(--ui-text-tertiary)">
          <span className="inline-flex items-center gap-1">
            <Codicon name="history" size="0.6875rem" />
            {m.recalls(fact.retrieval_count)}
          </span>
          <span className="inline-flex items-center gap-1">
            <Codicon name="thumbsup" size="0.6875rem" />
            {m.helpfulVotes(fact.helpful_count)}
          </span>
        </div>
      </div>
    </motion.article>
  )
}

function MemoriesSkeleton() {
  return (
    <div aria-hidden="true" className="grid grid-cols-[repeat(auto-fill,minmax(17rem,1fr))] gap-2">
      {Array.from({ length: 9 }, (_, index) => (
        <div
          className="space-y-2.5 rounded-lg border border-(--ui-stroke-tertiary) bg-(--ui-chat-bubble-background) p-2.5"
          key={index}
        >
          <div className="flex items-center justify-between">
            <Skeleton className="h-2.5 w-16" />
            <Skeleton className="h-2.5 w-10" />
          </div>
          <Skeleton className="h-3 w-full" />
          <Skeleton className="h-3 w-4/5" />
          <div className="flex items-center gap-2 pt-1">
            <Skeleton className="h-1.5 flex-1 rounded-full" />
            <Skeleton className="h-2.5 w-8" />
          </div>
        </div>
      ))}
    </div>
  )
}

function EmptyState({ description, title }: { description: string; title: string }) {
  return (
    <motion.div
      animate={{ opacity: 1, y: 0 }}
      className="grid min-h-52 place-items-center text-center"
      initial={{ opacity: 0, y: 6 }}
    >
      <div>
        <div className="text-sm font-medium">{title}</div>
        <div className="mt-1 text-xs text-muted-foreground">{description}</div>
      </div>
    </motion.div>
  )
}

function ErrorState({ description, onRetry, title }: { description: string; onRetry: () => void; title: string }) {
  const { t } = useI18n()

  return (
    <motion.div
      animate={{ opacity: 1, y: 0 }}
      className="grid min-h-52 place-items-center text-center"
      initial={{ opacity: 0, y: 6 }}
    >
      <div className="flex flex-col items-center gap-2">
        <ErrorIcon size="1.25rem" />
        <div className="text-sm font-medium">{title}</div>
        <div className="max-w-prose text-xs text-muted-foreground">{description}</div>
        <Button onClick={onRetry} size="xs" type="button" variant="textStrong">
          <Codicon name="refresh" size="0.75rem" />
          {t.memories.refresh}
        </Button>
      </div>
    </motion.div>
  )
}
