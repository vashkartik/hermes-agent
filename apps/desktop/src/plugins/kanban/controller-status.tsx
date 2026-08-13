import { cn, Codicon } from '@hermes/plugin-sdk'

import {
  type ControllerExternalStage,
  type ControllerProjection,
  type ControllerStageProjection
} from './types'

export const CONTROLLER_EXTERNAL_STAGES: readonly ControllerExternalStage[] = [
  'SENT',
  'ACE ACCEPTED',
  'RESPONSE RECEIVED',
  'VECTOR ACKNOWLEDGED'
]

export function orderedControllerStages(controller: ControllerProjection): ControllerStageProjection[] {
  const byStatus = new Map<ControllerExternalStage, ControllerStageProjection>()
  for (const stage of controller.status_projection) {
    if (!CONTROLLER_EXTERNAL_STAGES.includes(stage.status)) {
      continue
    }
    const previous = byStatus.get(stage.status)
    if (!previous || (!previous.reached && stage.reached)) {
      byStatus.set(stage.status, stage)
    }
  }

  return CONTROLLER_EXTERNAL_STAGES.map(
    status => byStatus.get(status) ?? { reached: false, status }
  )
}

export function ControllerStatus({
  compact = false,
  controller,
  label
}: {
  compact?: boolean
  controller: ControllerProjection
  label: string
}) {
  const stages = orderedControllerStages(controller)
  const latest = [...stages].reverse().find(stage => stage.reached)

  if (compact) {
    return (
      <div
        aria-label={label}
        className="flex min-w-0 items-center gap-1.5 text-[0.625rem] text-(--ui-text-tertiary)"
        data-controller-status="compact"
      >
        <span aria-hidden className="flex shrink-0 gap-1">
          {stages.map(stage => (
            <span
              className={cn(
                'size-1.5 rounded-full border border-(--ui-stroke-secondary)',
                stage.reached && 'border-(--dt-composer-ring) bg-(--dt-composer-ring)'
              )}
              key={stage.status}
            />
          ))}
        </span>
        <span className="truncate font-medium">{latest?.status ?? label}</span>
      </div>
    )
  }

  return (
    <div className="flex flex-col gap-1.5" data-controller-status="detailed">
      {stages.map(stage => (
        <div
          className={cn(
            'flex items-center gap-2 text-[0.6875rem]',
            stage.reached ? 'text-(--ui-text-secondary)' : 'text-(--ui-text-quaternary)'
          )}
          data-reached={stage.reached ? 'true' : 'false'}
          key={stage.status}
        >
          <Codicon name={stage.reached ? 'pass-filled' : 'circle-outline'} size="0.75rem" />
          <span className="font-medium tracking-wide">{stage.status}</span>
        </div>
      ))}
    </div>
  )
}
