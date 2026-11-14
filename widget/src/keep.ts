// The whole view is rebuilt on every change. These helpers carry what the owner is doing
// across a rebuild: the focused `data-keep` field (text + caret) and the panel's scroll
// (stuck to the bottom when it was at the bottom, so streamed chat replies stay visible).

const BOTTOM_SLACK_PX = 24;

export interface UiState {
  keep: { key: string; value: string; start: number; end: number } | null;
  scrollTop: number;
  atBottom: boolean;
}

export function keepUiState(root: HTMLElement): UiState {
  const active = document.activeElement;
  const field = active instanceof HTMLTextAreaElement || active instanceof HTMLInputElement ? active : null;
  const key = field && root.contains(field) ? field.dataset.keep : undefined;
  const body = root.querySelector<HTMLElement>(".panel__body");
  return {
    keep:
      field && key
        ? { key, value: field.value, start: field.selectionStart ?? field.value.length, end: field.selectionEnd ?? field.value.length }
        : null,
    scrollTop: body?.scrollTop ?? 0,
    atBottom: body ? body.scrollHeight - body.scrollTop - body.clientHeight <= BOTTOM_SLACK_PX : false,
  };
}

export function restoreUiState(root: HTMLElement, ui: UiState): void {
  const body = root.querySelector<HTMLElement>(".panel__body");
  if (body) body.scrollTop = ui.atBottom && body.querySelector(".chat__log") ? body.scrollHeight : ui.scrollTop;
  if (!ui.keep) return;
  const field = Array.from(root.querySelectorAll<HTMLTextAreaElement | HTMLInputElement>("[data-keep]")).find(
    (el) => el.dataset.keep === ui.keep?.key,
  );
  if (!field) return;
  field.value = ui.keep.value;
  field.focus({ preventScroll: true });
  field.setSelectionRange(ui.keep.start, ui.keep.end);
}
