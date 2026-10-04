// Fixed local preview operations; no caller-supplied JavaScript or shell commands.
import { createRequire } from 'node:module';
import { join } from 'node:path';

let input = '';
for await (const chunk of process.stdin) input += chunk;
const request = JSON.parse(input);
const require = createRequire(join(request.runtime, 'package.json'));
const { chromium } = require('playwright');
const browser = await chromium.launch({ channel: request.channel, headless: true,
  args: ['--disable-background-networking', '--disable-extensions', '--disable-sync'] });
const snapshots = [], blocked = [];
const measure = () => {
  const result = [], walker = document.createTreeWalker(document.body, NodeFilter.SHOW_TEXT);
  while (walker.nextNode() && result.length < 2000) {
    const node = walker.currentNode, text = node.textContent.trim(), el = node.parentElement;
    if (!text || !el || ['SCRIPT', 'STYLE', 'NOSCRIPT'].includes(el.tagName)) continue;
    const style = getComputedStyle(el);
    if (style.visibility === 'hidden' || style.display === 'none') continue;
    const range = document.createRange(); range.selectNodeContents(node);
    const rects = Array.from(range.getClientRects()).filter(r => r.width && r.height)
      .map(r => ({ x: r.x, y: r.y, width: r.width, height: r.height }));
    if (rects.length) result.push({ text, tag: el.tagName, id: el.id, classes: el.className, rects });
  }
  const controls = Array.from(document.querySelectorAll('a,button,input,select,[role=tab],[role=button]'))
    .slice(0, 100).map(e => ({ tag: e.tagName, id: e.id,
      text: (e.innerText || e.getAttribute('aria-label') || '').slice(0, 120),
      href: e.getAttribute('href'), type: e.getAttribute('type') }));
  return { title: document.title, url: location.href, scroll_y: scrollY, viewport_width: innerWidth,
    document_width: Math.max(document.documentElement.scrollWidth, document.body.scrollWidth),
    visible_text: document.body.innerText, text_rects: result, controls };
};
try {
  const context = await browser.newContext({ acceptDownloads: false, serviceWorkers: 'block' });
  await context.route('**/*', route => {
    const url = route.request().url();
    if (url.startsWith(request.origin + '/') && request.assets.includes(decodeURIComponent(new URL(url).pathname)))
      return route.continue();
    blocked.push(url); return route.abort();
  });
  await context.routeWebSocket('**/*', socket => socket.close());
  context.on('dialog', dialog => dialog.dismiss());
  const page = await context.newPage(); page.setDefaultTimeout(3000);
  for (const width of request.widths) {
    await page.setViewportSize({ width, height: 900 });
    await page.goto(request.entry, { waitUntil: 'domcontentloaded', timeout: 10000 });
    await page.waitForTimeout(250);
    const initial = await page.evaluate(measure), changes = [];
    for (const action of request.actions) {
      const selector = String(action.selector || '');
      if (!selector || selector.length > 200) throw new Error('Each action requires a bounded selector');
      const target = page.locator(selector).first();
      const kind = action.type || 'click';
      if (kind === 'click') await target.click();
      else if (kind === 'fill') await target.fill(String(action.value || ''));
      else if (kind === 'select') await target.selectOption(String(action.value || ''));
      else throw new Error('Only click, fill and select actions are supported');
      await page.waitForTimeout(100);
      changes.push({ action, after: await page.evaluate(measure) });
    }
    const screenshot = join(request.folder, `browser-${width}.png`);
    await page.screenshot({ path: screenshot, fullPage: true, timeout: 10000 });
    snapshots.push({ width, initial, actions: changes, screenshot });
  }
  process.stdout.write(JSON.stringify({ renderer: 'Headless Chromium', snapshots, blocked_requests: blocked,
    limitation: 'Actual local browser layout and finite interactions were measured. Geometry cannot establish every visual defect; screenshots are archived. External requests, WebSockets and downloads are blocked.' }));
} finally { await browser.close(); }
