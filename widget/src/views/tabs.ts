// Panel tabs: Sessions | Chat | Réglages, inserted right under a panel's header bar.
import { h } from "../dom";

export type Tab = "sessions" | "chat" | "settings";

const TABS: ReadonlyArray<readonly [Tab, string]> = [
  ["sessions", "Sessions"],
  ["chat", "Chat"],
  ["settings", "Réglages"],
];

function onTabKey(event: KeyboardEvent): void {
  if (event.key !== "ArrowRight" && event.key !== "ArrowLeft") return;
  const tabs = Array.from(document.querySelectorAll<HTMLButtonElement>("[role=tab]"));
  const index = tabs.indexOf(document.activeElement as HTMLButtonElement);
  const next = tabs[(index + (event.key === "ArrowRight" ? 1 : tabs.length - 1)) % tabs.length];
  if (next) {
    event.preventDefault();
    next.focus();
    next.click();
  }
}

export function tabBar(active: Tab, select: (tab: Tab) => void): HTMLElement {
  return h(
    "nav",
    { class: "tabs", role: "tablist", "aria-label": "Sections", onkeydown: onTabKey as EventListener },
    ...TABS.map(([id, label]) =>
      h(
        "button",
        {
          class: `tab ${id === active ? "tab--active" : ""}`,
          role: "tab",
          type: "button",
          "aria-selected": id === active ? "true" : "false",
          tabindex: id === active ? 0 : -1,
          onclick: () => select(id),
        },
        label,
      ),
    ),
  );
}

/** Puts the tab bar between a panel's header and its body. */
export function withTabs(panel: HTMLElement, tabs: HTMLElement): HTMLElement {
  const head = panel.querySelector(":scope > header");
  if (head) head.after(tabs);
  else panel.prepend(tabs);
  return panel;
}
