// 仅为基础设施探针；不会将本程序作为候选工具提供。
import { chromium } from 'playwright';
import { readFile, writeFile, mkdir } from 'node:fs/promises';
import assert from 'node:assert/strict';

const input = JSON.parse(await readFile('/input/browser-probe.json', 'utf8'));
const output = '/data/browser-artifacts';
await mkdir(output, { recursive: true });
const browser = await chromium.launch({ headless: true, args: ['--no-sandbox'] });
const context = await browser.newContext({ acceptDownloads: true });
await context.tracing.start({ screenshots: true, snapshots: true });
const page = await context.newPage();
try {
  await page.goto(input.url + '/login');
  await page.getByLabel('用户名').fill(input.username);
  await page.getByLabel('密码').fill(input.password);
  await page.getByRole('button', { name: '登录' }).click();
  await page.waitForURL('**/items');
  await page.getByRole('link', { name: '下一页' }).click();
  await page.waitForURL('**/items?page=2');
  const item_id = await page.locator('tbody tr').first().locator('td').first().innerText();
  await page.getByRole('link', { name: '填写业务记录' }).click();
  await page.getByLabel('操作编号').fill(input.operation_id);
  await page.getByLabel('数据编号').fill(item_id);
  await page.getByLabel('数量').fill('0');
  await page.getByRole('button', { name: '提交', exact: true }).click();
  const invalid = await page.getByLabel('数量').evaluate(el => !el.checkValidity());
  assert(invalid);
  assert(page.url().endsWith('/order'));
  await page.getByLabel('数量').fill('2');
  await page.getByRole('button', { name: '提交', exact: true }).click();
  await page.waitForURL('**/receipt?operation_id=*');
  const receipt = JSON.parse(await page.locator('#receipt').innerText());
  assert.equal(receipt.write_count, 1);
  const downloadEvent = page.waitForEvent('download');
  await page.getByRole('link', { name: '下载结果' }).click();
  const download = await downloadEvent;
  await download.saveAs(output + '/download.csv');
  await page.screenshot({ path: output + '/receipt.png', fullPage: true });
  await writeFile(output + '/primitive-result.json', JSON.stringify({
    passed: true, operation_id: input.operation_id, item_id, quantity: 2,
    invalid_quantity_rejected: invalid, browser_version: browser.version(),
  }, null, 2));
} catch (error) {
  await writeFile(output + '/primitive-error.txt', String(error));
  throw error;
} finally {
  await context.tracing.stop({ path: output + '/trace.zip' });
  await browser.close();
}
