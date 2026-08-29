/** Wraps a raw `<svg>...</svg>` source string (from a Vite `?raw` import of
 * one of the custom tree icons in `src/assets/tree-icons/`) as a Vue
 * component matching @ant-design/icons-vue's drop-in shape: the rendered
 * root IS the `<svg>` element, so `<component :is="..." :style="...">`'s
 * fallthrough `style` (width/height/font-size/color from
 * LeftSideView.vue's TREE_ICON_SLOT_STYLE) lands directly on it exactly
 * like the Ant icon components it replaces -- callers need no changes.
 *
 * Every icon in that folder is a single `fill="currentColor"` path on a
 * `viewBox="0 0 24 24"` root (see the design spec these were commissioned
 * from), so inlining the source's own inner markup via the DOM `innerHTML`
 * property -- rather than re-parsing it into vnodes -- is enough for
 * `currentColor` to correctly inherit the caller's `color` style. An
 * `<img src="...">` reference would NOT work here: images don't let page
 * CSS reach into the SVG, so `currentColor` would never resolve to the
 * app's theme text color. */
import { h, type Component } from 'vue'

export function svgIcon(source: string): Component {
  const inner = source.replace(/^<svg[^>]*>/, '').replace(/<\/svg>\s*$/, '')
  return {
    name: 'TreeSvgIcon',
    render() {
      return h('svg', { viewBox: '0 0 24 24', innerHTML: inner })
    },
  }
}
