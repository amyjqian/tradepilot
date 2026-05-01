import { PanelResizeHandle } from 'react-resizable-panels'

interface Props {
  /** "horizontal" splits get a vertical bar, "vertical" splits get a
   * horizontal bar. The PanelGroup direction tells us which axis the
   * panels lie along; the handle is perpendicular to that. */
  direction?: 'horizontal' | 'vertical'
}

/** Thin draggable divider for `react-resizable-panels`. The visible
 * surface is just 1px so panels meet flush, but the hit area is 6px so
 * the cursor catches it without precision aiming. Hover + drag states
 * brighten the line.
 */
export function ResizeHandle({ direction = 'horizontal' }: Props) {
  const isHorizontalSplit = direction === 'horizontal'
  // For a horizontal PanelGroup, panels sit side-by-side, so the handle
  // is a thin VERTICAL bar that grows/shrinks the column widths. Inverse
  // for vertical.
  const sizeClass = isHorizontalSplit
    ? 'w-1.5 cursor-col-resize'
    : 'h-1.5 cursor-row-resize'
  const lineClass = isHorizontalSplit
    ? 'h-full w-px'
    : 'h-px w-full'

  return (
    <PanelResizeHandle
      className={`group relative flex shrink-0 items-center justify-center bg-transparent ${sizeClass}`}
    >
      <div
        className={`${lineClass} bg-neutral-800 transition-colors group-hover:bg-[var(--color-accent)] group-data-[resize-handle-state=drag]:bg-[var(--color-accent)]`}
      />
    </PanelResizeHandle>
  )
}
