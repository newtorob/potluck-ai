import { ipc } from '@/lib/ipc';

/**
 * Custom frameless-window drag bar for macOS.
 *
 * On macOS, Electron is launched with `titleBarStyle: 'hiddenInset'`, which
 * hides the native title bar but keeps the traffic-light buttons. This
 * component provides the complementary drag region so users can still move
 * the window.
 *
 * On Windows and Linux the native frame is shown, so this renders nothing.
 * The CSS property `-webkit-app-region: drag` is applied via the
 * `data-drag-region` attribute styled in globals.css.
 */
export function TitleBar() {
  const isMac = ipc.platform === 'darwin';

  if (!isMac) return null;

  return (
    <div
      data-drag-region
      className="h-8 w-full shrink-0 select-none bg-background"
      aria-hidden="true"
    />
  );
}
