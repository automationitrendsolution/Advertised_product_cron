# `config/`

## `selectors.json` — Amazon UI locators

Ordered candidate selectors for each element AdProduct interacts with on the
Sponsored ads reports page. The first one that becomes visible wins.

**They were built from the screenshots of the manual process and have not yet
been run against the live page.** Do the first run headed:

```bash
uv run python run.py --period today --headed --dry-run
```

When a run fails with a `ReportConfigError` or `NavigationError` naming a
selector key, add a new candidate at the **top** of that key's list. You do not
need to change code or redeploy. A screenshot of the failure is written to
`data/screenshots/`.

Some candidates contain placeholders, which the code fills in:

| Placeholder | Filled with | Used by |
|---|---|---|
| `{label}` | a form label, e.g. `Report category` | `form_dropdown` |
| `{value}` | an option, e.g. `Attributed Purchases`, `Daily`, `Today` | `dropdown_option`, `time_unit_option`, `date_preset_option` |
| `{name}` | the run's unique report name | `report_row` |

Keys starting with `_` are comments.
