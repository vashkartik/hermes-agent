import { cleanup, render, screen } from '@testing-library/react'
import { afterEach, describe, expect, it } from 'vitest'

import { ControllerStatus, orderedControllerStages } from './controller-status'
import type { ControllerProjection } from './types'

const projection: ControllerProjection = {
  ace_identity: 'ace:pending:t_1',
  controller_assignee: 'vectorctrl1',
  correlation_id: 'kanban:t_1',
  opted_in_at: '2026-08-13T12:00:00Z',
  protocol: 'ace.controller.v1',
  status_projection: [
    { reached: true, status: 'SENT' },
    { reached: true, status: 'ACE ACCEPTED' },
    { reached: false, status: 'RESPONSE RECEIVED' },
    { reached: false, status: 'VECTOR ACKNOWLEDGED' }
  ],
  terminal: false
}

afterEach(cleanup)

describe('controller status projection', () => {
  it('renders exactly the four external stages from structured state', () => {
    render(<ControllerStatus controller={projection} label="Controller" />)

    for (const label of ['SENT', 'ACE ACCEPTED', 'RESPONSE RECEIVED', 'VECTOR ACKNOWLEDGED']) {
      expect(screen.getByText(label)).toBeTruthy()
    }
    expect(document.querySelectorAll('[data-controller-status="detailed"] [data-reached]')).toHaveLength(4)
    expect(document.querySelectorAll('[data-reached="true"]')).toHaveLength(2)
  })

  it('normalizes adversarial duplicate and out-of-order input without inventing a fifth stage', () => {
    const hostile = {
      ...projection,
      status_projection: [
        { reached: true, status: 'VECTOR ACKNOWLEDGED' as const },
        { reached: true, status: 'SENT' as const },
        { reached: false, status: 'SENT' as const }
      ]
    }

    const stages = orderedControllerStages(hostile)
    expect(stages.map(stage => stage.status)).toEqual([
      'SENT',
      'ACE ACCEPTED',
      'RESPONSE RECEIVED',
      'VECTOR ACKNOWLEDGED'
    ])
    expect(stages[0]?.reached).toBe(true)
  })

  it('shows the latest reached stage on the board-card projection', () => {
    render(<ControllerStatus compact controller={projection} label="Controller" />)

    expect(screen.getByText('ACE ACCEPTED')).toBeTruthy()
    expect(document.querySelectorAll('[data-controller-status="compact"] [aria-hidden] span')).toHaveLength(4)
  })
})
