# MeasureAble HEDIS Setup Automation

A Playwright script that sets up a HEDIS measure in the MeasureAble **external-qa** environment, replacing the manual clicks in the Able Admin pages.

For a given measure (for example `saa`) it works against `https://hedis-<measure>.external-qa.ablehealth.com` and:

1. Signs in.
2. Opens **Able Admin > Dashboard Settings**, sets `Name` to `HEDIS <year> <MEASURE> Certification`, lets you choose the `Customer`, and clicks **Update Dashboard**. `Specialty` and `Product name` are left unchanged.
3. Opens **Able Admin > Programs**. If programs already exist, it lists them and asks whether to delete them.
4. Creates a custom program named `<year> <MEASURE> HEDIS Certification`:
   - Short name: same as the name
   - Sponsor: `NCQA`
   - Requested quality measure version: `2025` (required by the app; can be changed)
   - Measurement period: `01/01/<year>` to `12/31/<year>`
   - "Assign to all patients when doing a patient program membership ETL import": checked
5. Reopens the new program's settings, changes **Requested quality measure version** from `2025` to the blank option and clicks **Update program**. (The create form requires a version, so it is set first and cleared right after.)
6. Adds the quality measures that belong to the measure (options starting with `<year> HEDIS <MEASURE>`), one at a time with **Create measure**.
7. Leaves the finished program open in the browser so you can check it, then asks whether to run the import. If you answer yes, it opens **Able Admin > Run dashboard import**, ticks **Require full import for all dashboard importable tables**, searches for the measure (for example `saa`), selects the matching dashboard (`hedis-saa - HEDIS 2026 SAA Certification`) and clicks **Start**.
8. Waits for the import to finish, with no time limit. It checks the Recent Ad-Hoc Imports list every 30 seconds, prints each status change, and when the import reaches a final status it prints `Import SUCCEEDED` or `Import FAILED` in the terminal and beeps.

## Prerequisites

- Node.js 18 or later
- Access to the MeasureAble external-qa environment with Able Admin permissions

## Setup (one time)

```powershell
cd automation
npm install
npx playwright install chromium
```

## Run

```powershell
cd automation
node create-hedis-dashboard.mjs --measure saa --year 2026
```

You can also use `npm run setup -- --measure saa --year 2026`.

### Options

| Option | Required | Default | Description |
| --- | --- | --- | --- |
| `--measure` | Yes | | Measure name. Sets the URL (`hedis-<measure>...`) and the names, for example `saa`. |
| `--year` | No | Current year | Year used in names and the measurement period. |
| `--customer` | No | Prompted | Exact customer name, for example `"2026 HEDIS RES Consolidated"`. Skips the customer prompt. |
| `--qm-version` | No | `2025` | Value for "Requested quality measure version". |

### Credentials

Set these environment variables to sign in automatically:

```powershell
$env:HEDIS_EMAIL = "you@example.com"
$env:HEDIS_PASSWORD = "your-password"
```

If they are not set, the script opens the sign-in page and waits for you to sign in manually in the browser window. Never put credentials in the code or commit them.

### Prompts during the run

- **Customer:** type the customer name (a partial name works if it matches only one customer), unless `--customer` is given.
- **Existing programs:** `Delete all existing programs? (y/N)`. Answering `y` deletes every program listed.
- **Quality measures:** the matching measures are listed. Press Enter to add all, enter numbers such as `1,3` to add some, or `n` to skip.
- **Run the import:** `Does everything look fine? Run the import now? (y/N)`. Check the program in the browser first. Answer `y` to start a full import for the dashboard. Answer `n` (or just press Enter) and the script finishes and closes the browser.

A browser window opens so you can watch the run. When the setup finishes, the browser is left open on the new program so you can check it. If you start the import, the browser stays open afterwards until you press Enter in the terminal. If a step fails, the browser also stays open on the page where it stopped, so you can see what went wrong.

## Notes

- Deleting a program removes it from the portal. Make sure nobody still needs it before answering `y`.
- Some measures have several rates, so more than one quality measure can match (for example `IET`, `W30`, `PCR`). Review the list before confirming.
- After **Create measure** the form stays on the same page, so the script confirms each measure by checking the program page.
- The script targets the external-qa environment only.
- While waiting for an import (which can take from about 10 minutes to over an hour), keep the terminal and the browser window open. Closing either stops the wait. The import itself keeps running in the portal. If the portal signs you out during a long wait, the script tells you to check the status in the portal.

## Project structure

```
automation/
  create-hedis-dashboard.mjs   The automation script
  package.json                 Dependencies (Playwright)
  package-lock.json
  .gitignore                   Ignores node_modules
```
