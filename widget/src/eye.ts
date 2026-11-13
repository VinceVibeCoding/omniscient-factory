// The omniscient eye mark, drawn with theme variables so it follows GET /theme.
import { svg } from "./dom";

export function eyeMark(size = 22, watching = false): SVGElement {
  const root = svg("svg", {
    viewBox: "0 0 64 64",
    width: size,
    height: size,
    class: watching ? "eye eye--watching" : "eye",
    "aria-hidden": "true",
    focusable: "false",
  });
  root.append(
    svg("path", {
      d: "M6 32 C 16 18, 48 18, 58 32 C 48 46, 16 46, 6 32 Z",
      fill: "none",
      stroke: "var(--accent)",
      "stroke-width": 4,
      "stroke-linejoin": "round",
    }),
    svg("circle", { class: "eye__iris", cx: 32, cy: 32, r: 10, fill: "var(--primary)" }),
    svg("circle", { cx: 32, cy: 32, r: 4.5, fill: "var(--background)" }),
    svg("circle", { cx: 35, cy: 29, r: 2, fill: "var(--foreground)" }),
  );
  return root;
}
