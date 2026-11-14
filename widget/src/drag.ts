// Drag to move: press the left button anywhere except a text field and move a few pixels;
// Rust then hands the window to the OS move loop. A press without movement stays a click,
// and the click that may follow a drag is swallowed so the pill does not open by accident.

const THRESHOLD_PX = 4;
const FIELDS = "input, textarea, select, [contenteditable]";

export function installDrag(root: Document, startDrag: () => Promise<void>): void {
  let origin: { x: number; y: number } | null = null;
  let dragged = false;

  root.addEventListener("pointerdown", (event) => {
    const target = event.target as Element | null;
    dragged = false;
    origin = event.button === 0 && !target?.closest(FIELDS) ? { x: event.screenX, y: event.screenY } : null;
  });

  root.addEventListener("pointermove", (event) => {
    if (!origin || (event.buttons & 1) === 0) return;
    const moved = Math.hypot(event.screenX - origin.x, event.screenY - origin.y);
    if (moved < THRESHOLD_PX) return;
    origin = null;
    dragged = true;
    startDrag().catch((err: unknown) => console.warn("omnigroup: drag failed", err));
  });

  root.addEventListener("pointerup", () => {
    origin = null;
  });

  root.addEventListener(
    "click",
    (event) => {
      if (!dragged) return;
      dragged = false;
      event.stopPropagation();
      event.preventDefault();
    },
    true,
  );
}
