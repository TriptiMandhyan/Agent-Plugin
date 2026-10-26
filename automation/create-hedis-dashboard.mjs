// Usage:
//   node create-hedis-dashboard.mjs --measure saa [--year 2026] [--customer "2026 HEDIS RES Consolidated"]
//
// Credentials (optional): set HEDIS_EMAIL and HEDIS_PASSWORD environment variables.
// If they are not set, the script waits for you to sign in manually in the browser window.

import { chromium } from 'playwright';
import { parseArgs } from 'node:util';
import readline from 'node:readline/promises';
import { stdin as input, stdout as output } from 'node:process';

const { values } = parseArgs({
  options: {
    measure: { type: 'string' },
    year: { type: 'string', default: String(new Date().getFullYear()) },
    customer: { type: 'string' },
    'qm-version': { type: 'string', default: '2025' },
  },
});

if (!values.measure) {
  console.error('Missing --measure (e.g. --measure saa)');
  process.exit(1);
}

const measureSlug = values.measure.trim().toLowerCase();
const MEASURE = measureSlug.toUpperCase();
const year = values.year.trim();
const baseUrl = `https://hedis-${measureSlug}.external-qa.ablehealth.com`;

const rl = readline.createInterface({ input, output });
const ask = async (question) => (await rl.question(question)).trim();

async function signIn(page) {
  await page.goto(`${baseUrl}/users/sign_in`);
  const email = process.env.HEDIS_EMAIL;
  const password = process.env.HEDIS_PASSWORD;

  if (email && password) {
    await page.getByRole('textbox', { name: 'Email' }).fill(email);
    await page.getByRole('textbox', { name: 'Password' }).fill(password);
    await page.getByRole('button', { name: 'Sign in' }).click();
  } else {
    console.log('Sign in manually in the browser window...');
  }
  await page.waitForURL((url) => !url.pathname.startsWith('/users/sign_in'), { timeout: 5 * 60_000 });
}

async function openAbleAdminTools(page) {
  await page.getByRole('link', { name: 'Able Admin' }).click();
  await page.waitForURL('**/able_admin/tools');
}

// Step 1: Dashboard settings -> name + customer -> Update Dashboard
async function updateDashboard(page) {
  await page.getByRole('link', { name: 'Dashboard Settings' }).click();
  await page.waitForURL('**/able_admin/dashboards/*/edit');

  await page.getByLabel('Name', { exact: true }).fill(`HEDIS ${year} ${MEASURE} Certification`);

  // The Customer dropdown's <label> is not linked to the <select>, so find the select after the label text.
  const customerSelect = page.locator('label', { hasText: /^\s*Customer\s*$/ }).locator('xpath=following::select[1]');
  await customerSelect.waitFor({ state: 'attached' });
  const options = await customerSelect.locator('option').evaluateAll((els) =>
    els.map((o) => ({ value: o.value, label: o.textContent.trim(), selected: o.selected })).filter((o) => o.label),
  );
  if (options.length === 0) {
    throw new Error('Could not find any options in the Customer dropdown.');
  }
  console.log(`Found ${options.length} customers.`);

  let chosen = values.customer && options.find((o) => o.label.toLowerCase() === values.customer.toLowerCase());
  if (values.customer && !chosen) {
    console.log(`Customer "${values.customer}" not found.`);
  }
  if (!chosen) {
    console.log('\nCustomers:');
    options.forEach((o) => console.log(`  - ${o.label}${o.selected ? '  (current)' : ''}`));
  }
  while (!chosen) {
    const answer = (await ask('\nType the customer name: ')).toLowerCase();
    if (!answer) continue;

    // Exact name wins, otherwise accept a partial name that matches only one customer.
    chosen = options.find((o) => o.label.toLowerCase() === answer);
    if (!chosen) {
      const partial = options.filter((o) => o.label.toLowerCase().includes(answer));
      if (partial.length === 1) {
        chosen = partial[0];
      } else if (partial.length > 1) {
        console.log('More than one customer matches, type the full name:');
        partial.forEach((o) => console.log(`  - ${o.label}`));
      } else {
        console.log(`No customer matches "${answer}".`);
      }
    }
  }

  await customerSelect.selectOption({ value: chosen.value });
  console.log(`Customer: ${chosen.label}`);

  await page.getByRole('button', { name: 'Update Dashboard' }).click();
  await page.waitForLoadState('networkidle');
  console.log('Dashboard updated.');
}

// Step 2: Programs -> optionally delete existing programs
async function readPrograms(page) {
  await page.goto(`${baseUrl}/able_admin/programs`);
  const rows = page.locator('table tbody tr');
  const names = (await rows.locator('td:first-child').allTextContents()).map((t) => t.trim()).filter(Boolean);
  return { rows, names };
}

async function deleteExistingPrograms(page) {
  let { rows, names } = await readPrograms(page);
  if (names.length === 0) return;

  console.log('\nExisting programs:');
  names.forEach((n) => console.log(`  - ${n}`));
  const answer = (await ask('Delete all existing programs? (y/N): ')).toLowerCase();
  if (answer !== 'y' && answer !== 'yes') {
    console.log('Keeping existing programs.');
    return;
  }

  while (names.length > 0) {
    const name = names[0];
    await rows.first().click();
    await page.waitForURL('**/able_admin/programs/*/edit');
    page.once('dialog', (dialog) => dialog.accept());
    await page.getByRole('link', { name: 'Delete Program' }).click();
    await page.waitForURL(/\/able_admin\/programs\/?(\?.*)?$/, { timeout: 5 * 60_000 });
    console.log(`Deleted: ${name}`);
    ({ rows, names } = await readPrograms(page));
  }
}

// Step 3: Create custom program
async function createProgram(page) {
  const programName = `${year} ${MEASURE} HEDIS Certification`;

  // Programs -> Create Program -> custom program
  await page.goto(`${baseUrl}/able_admin/programs/new?program_type=Custom`);

  await page.getByLabel('Name', { exact: true }).fill(programName);
  await page.getByLabel('Short name', { exact: true }).fill(programName);
  await page.getByLabel('Sponsor', { exact: true }).fill('NCQA');

  // Required by the app: creating the program fails if this is left blank.
  await page.getByLabel('Requested quality measure version').selectOption({ label: values['qm-version'] });

  await page.getByLabel('Requested measurement period start date').fill(`01/01/${year}`);
  await page.keyboard.press('Tab');
  await page.getByLabel('Requested measurement period end date').fill(`12/31/${year}`);
  await page.keyboard.press('Tab');

  await page.getByLabel(/Assign to all patients/).check();
  await page.getByRole('button', { name: 'Create Program' }).click();
  await page.waitForLoadState('networkidle');

  // After creating, the app redirects to the programs list, so open the program from there.
  let match = page.url().match(/\/able_admin\/programs\/(\d+)/);
  if (!match) {
    await page.goto(`${baseUrl}/able_admin/programs`);
    await page.locator('table tbody tr', { hasText: programName }).first().click();
    await page.waitForURL('**/able_admin/programs/*/edit');
    match = page.url().match(/\/able_admin\/programs\/(\d+)/);
  }
  console.log(`Created program "${programName}" (id ${match[1]}).`);
  return match[1];
}

// Step 3b: The create form needs a quality measure version, but the finished program should have it blank.
async function clearQualityMeasureVersion(page, programId) {
  await page.goto(`${baseUrl}/able_admin/programs/${programId}/edit`);
  const versionSelect = page.getByLabel('Requested quality measure version');
  await versionSelect.selectOption({ value: '' });
  await page.getByRole('button', { name: 'Update program' }).click();
  await page.waitForLoadState('networkidle');

  // Confirm the blank value was saved.
  await page.goto(`${baseUrl}/able_admin/programs/${programId}/edit`);
  const saved = await page.getByLabel('Requested quality measure version').inputValue();
  if (saved === '') {
    console.log('Requested quality measure version cleared (blank) and program updated.');
  } else {
    console.warn(`Could not confirm the blank version (still "${saved}"). Check the program settings.`);
  }
}

// Step 4: Add every quality measure that belongs to the measure
async function addQualityMeasures(page, programId) {
  const newMeasureUrl = `${baseUrl}/able_admin/programs/${programId}/measure_program_memberships/new`;
  await page.goto(newMeasureUrl);

  const prefix = `${year} HEDIS ${MEASURE}`.toLowerCase();
  const options = await page
    .getByLabel('Quality Measure', { exact: true })
    .locator('option')
    .evaluateAll((els) => els.map((o) => ({ value: o.value, label: o.textContent.trim() })));
  const matches = options.filter((o) => o.label.toLowerCase().startsWith(prefix));

  if (matches.length === 0) {
    console.log(`No quality measures found starting with "${year} HEDIS ${MEASURE}".`);
    return;
  }

  console.log('\nMatching quality measures:');
  matches.forEach((m, i) => console.log(`  ${i + 1}. ${m.label}`));
  const answer = await ask('Add all? Press Enter for all, or enter numbers (e.g. 1,3), or "n" to skip: ');

  let selected = matches;
  if (answer.toLowerCase() === 'n') return;
  if (answer) {
    selected = answer
      .split(',')
      .map((s) => matches[Number(s.trim()) - 1])
      .filter(Boolean);
  }

  for (const measure of selected) {
    await page.goto(newMeasureUrl);
    await page.getByLabel('Quality Measure', { exact: true }).selectOption({ value: measure.value });
    await page.getByRole('button', { name: 'Create measure' }).click();
    await page.waitForLoadState('networkidle');

    // The form stays on /new after creating, so confirm on the program page instead.
    await page.goto(`${baseUrl}/able_admin/programs/${programId}/edit`);
    const added = await page.getByRole('link', { name: measure.label, exact: true }).count();
    if (added > 0) {
      console.log(`Added: ${measure.label}`);
    } else {
      console.warn(`Could not confirm: ${measure.label}. Check the program page for errors.`);
    }
  }
}

// Reads the Recent Ad-Hoc Imports table on the import page.
async function readImportRows(page) {
  return page.locator('table tbody tr').evaluateAll((trs) =>
    trs.map((tr) => {
      const cells = [...tr.querySelectorAll('td')].map((td) => td.innerText.trim());
      return {
        href: tr.querySelector('a')?.getAttribute('href') ?? '',
        dashboard: cells[0] ?? '',
        status: cells[cells.length - 1] ?? '',
      };
    }),
  );
}

function notify(message) {
  console.log(`\n*** ${message} ***`);
  process.stdout.write('\u0007\u0007\u0007'); // terminal beep
}

// Keeps checking the import we started until it reaches a final status. No time limit.
async function waitForImport(page, existingHrefs) {
  const POLL_MS = 30_000;
  const dashboard = `hedis-${measureSlug}`;
  const startedAt = Date.now();
  const elapsed = () => {
    const s = Math.floor((Date.now() - startedAt) / 1000);
    return `${Math.floor(s / 60)}m ${String(s % 60).padStart(2, '0')}s`;
  };

  let importHref = '';
  let lastStatus = '';
  let lastHeartbeat = Date.now();
  console.log('\nWaiting for the import to finish. Keep this window open (checks every 30 seconds)...');

  while (true) {
    await page.goto(`${baseUrl}/able_admin/imports/new`);
    if (new URL(page.url()).pathname.startsWith('/users/sign_in')) {
      notify('Signed out while waiting. Check the import status in the portal.');
      return 'unknown';
    }

    const rows = await readImportRows(page);
    if (!importHref) {
      // The import we started is the new row for this dashboard that was not there before.
      importHref = rows.find((r) => r.dashboard === dashboard && r.href && !existingHrefs.has(r.href))?.href ?? '';
      if (!importHref && Date.now() - startedAt > 3 * 60_000) {
        notify('Could not find the new import in the list. Check the import status in the portal.');
        return 'unknown';
      }
    }

    const status = importHref ? (rows.find((r) => r.href === importHref)?.status ?? '') : '';
    if (status && status !== lastStatus) {
      console.log(`[${elapsed()}] Import status: ${status}`);
      lastStatus = status;
    } else if (Date.now() - lastHeartbeat > 5 * 60_000) {
      console.log(`[${elapsed()}] Still waiting... status: ${lastStatus || 'not listed yet'}`);
      lastHeartbeat = Date.now();
    }

    // The portal only uses three statuses: Queued (keep waiting), Succeeded and Failed.
    if (/^failed$/i.test(status)) {
      notify(`Import FAILED after ${elapsed()}. Details: ${baseUrl}${importHref}`);
      return 'failed';
    }
    if (/^succeeded$/i.test(status)) {
      notify(`Import SUCCEEDED after ${elapsed()}.`);
      return 'succeeded';
    }

    await new Promise((resolve) => setTimeout(resolve, POLL_MS));
  }
}

// Step 5 (optional): Able Admin > Run dashboard import
async function runImport(page) {
  await page.goto(`${baseUrl}/able_admin/tools`);
  await page.getByRole('link', { name: 'Run dashboard import' }).click();
  await page.waitForURL('**/able_admin/imports/new');

  // Remember the imports that already exist so we can tell which one is ours.
  const existingHrefs = new Set((await readImportRows(page)).map((r) => r.href));

  await page.getByRole('checkbox', { name: /Require full import/ }).check();

  // The search needs 3 or more characters, then the matching dashboard shows up as an option.
  const search = page.getByRole('searchbox', { name: 'Search for an option' });
  await search.click();
  await search.fill(measureSlug);
  const option = page.getByRole('option').filter({ hasText: `hedis-${measureSlug} - ` }).first();
  await option.waitFor({ timeout: 30_000 });
  const optionLabel = (await option.textContent()).trim();
  await option.click();
  console.log(`Selected: ${optionLabel}`);

  await page.getByRole('button', { name: 'Start', exact: true }).click();
  await page.waitForLoadState('networkidle');
  console.log('Import started.');

  const result = await waitForImport(page, existingHrefs);
  if (result === 'failed') process.exitCode = 1;
}

const browser = await chromium.launch({ headless: false });
const page = await (await browser.newContext()).newPage();
let failed = false;

try {
  await signIn(page);
  await openAbleAdminTools(page);
  await updateDashboard(page);

  await page.goto(`${baseUrl}/able_admin/tools`);
  await page.locator('a[href="/able_admin/programs"]').click();
  await page.waitForURL('**/able_admin/programs');
  await deleteExistingPrograms(page);

  const programId = await createProgram(page);
  await clearQualityMeasureVersion(page, programId);
  await addQualityMeasures(page, programId);

  // Leave the finished program on screen so the user can verify it.
  await page.goto(`${baseUrl}/able_admin/programs/${programId}/edit`);
  console.log('\nSetup finished. Check the program in the browser window.');

  const answer = (await ask('Does everything look fine? Run the import now? (y/N): ')).toLowerCase();
  if (answer === 'y' || answer === 'yes') {
    await runImport(page);
    await ask('\nPress Enter to close the browser...');
  } else {
    console.log('Import skipped. Task complete.');
  }
} catch (error) {
  failed = true;
  console.error('\nThe automation stopped because of an error:');
  console.error(error);
  console.error('The browser is still open on the page where it stopped.');
  process.exitCode = 1;
} finally {
  if (failed) {
    await ask('\nPress Enter to close the browser...').catch(() => {});
  }
  rl.close();
  await browser.close();
}
