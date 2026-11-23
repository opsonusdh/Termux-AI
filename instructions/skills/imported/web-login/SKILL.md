---
name: web-login
description: "Log the user into any website through the browser tool without the agent ever seeing credential values: open the site, have the user write a credentials JSON to workspace/<site>.json, fill and submit via a CDP driver script, handle 2FA / login-approval / captcha handoff, verify the logged-in page state, and save the session cookies (default ON) for later restore. Use when the user wants to sign into a site (Instagram, etc.) in the persistent browser session."
---

# General Web Login Workflow

How to log the user into **any** website through the browser tool — without ever
seeing their credentials — and keep the session for reuse.

Reference implementation: `insta_login.py` / `insta_restore.py` in this folder
(built and verified against Instagram, 2026-11).

---

## 0. Ground rules (read first — they are hard rules)

1. **The agent never sees credential values.** The user writes them into a JSON
   file. Never `cat`, read, print, echo or log that file's values. The agent
   knows only the *field names*.
2. **Values move only inside a script.** A driver script reads the JSON *in its
   own process* and types into the page. Its stdout carries only booleans,
   counts, element selectors, cookie *names* and URLs — never values.
3. **Never put credentials in command-line arguments, `env` in a visible
   command, or chat text.** Shell history and transcripts would capture them.
4. **Verify the page before typing into it.** Check the URL is the genuine
   domain and the form is the site's real login form (screenshot / `find`).
   Never submit credentials to a page that redirected somewhere unexpected.
5. **Saved cookies are credentials too.** A `sessionid` cookie *is* the
   account. Store with mode `0600`, never print values, never commit them.
6. **Saving the session is the default.** Save after every successful login
   unless the user explicitly says not to.
7. **Short-lived verification codes are the one exception** to rule 1: a 2FA
   code typed by the user in chat is acceptable because it expires in minutes.
   Prefer having them drop it into the JSON anyway.

---

## 1. Open the site and inspect the form

```
browser(action="open", url="https://site.com/login")
```

- Note the returned **session id** and the **CDP port** (also visible as the
  second number in `workspace/.browser/session_<pid>_<port>/`).
- `screenshot=true` to see the form, or `find` to list fields — every field
  comes back with its label and a ready CSS selector. Never guess coordinates.

## 2. Ask the user for a credentials JSON

Tell them **exactly which fields the form needs** (learned in step 1) and where
to put them:

```
workspace/<site>.json        ← user writes this, mode 0600
{ "id": "...", "pass": "..." }
```

Field names follow the site (`id`/`pass`, `email`/`pass`, `phone`, security
answers, …). The agent reads this file **only through a script** (rule 1–2).

## 3. Fill and submit — without seeing the values

Write or adapt a driver script (see §7) and run:

```
python3 workspace/browser_work/<site>_login.py fill <port>
```

The script, inside one process:

1. loads the JSON and picks the fields,
2. locates each input, click-focuses it, types per-character (CDP
   `Input.dispatchKeyEvent` — same event pattern as `core/tools.py`),
3. confirms the fields are filled **as booleans only**,
4. clicks the real submit control (`div[role="button"]`, `button[type=submit]`,
   or `input[type=submit]` — check the live markup),
5. polls page state until one of: logged in · 2FA/challenge text · `/challenge`
   in URL · error alert.

## 4. Verification / 2FA step

The site will stop at one of these. Ask the user — never guess:

| Type | What to do |
|---|---|
| **Login approval notification** ("we sent a notification to your phone") | Tell the user to approve on their device, then keep polling page state until it flips to logged-in. No code needed. |
| **2FA / OTP code** | Ask the user for the code (chat is fine, it's short-lived — or have them add `"code": "..."` to the JSON), then `<site>_login.py code <port> <code>`. Some forms auto-submit at the last digit; the script clicks submit otherwise. |
| **Email/link verification** | Tell the user to click it, keep polling. |
| **Captcha** | Tell the user to solve it in a visible browser window, or abort. Never auto-solve. |

Never re-ask for the password during verification, and never type credentials
into a verification screen.

## 5. Confirm the login actually happened

Do not trust the click — trust the page:

- URL has left `/login` / `/challenge`;
- logged-in markers present (`nav`, avatar, account menu — site-specific) and
  the login form is gone;
- final screenshot as visible proof.

Then report: what was verified, what was saved, what the user still needs to do.

## 6. Save the session (default: ON)

```
python3 workspace/browser_work/<site>_login.py save <port>
```

→ `workspace/browser_work/<site>_cookies.json`, mode `0600`, filtered to the
site's domains. Record alongside it: account name/id, capture date, cookie
**names + count** (never values) — e.g. in a short `README_<site>_session.md`.

Afterwards: delete the credentials JSON **unless the user wants to keep it**
(re-logins will need it when the session expires).

### Reuse next time

```
browser(action="open", url="https://site.com")        # fresh session → port
python3 workspace/browser_work/<site>_restore.py <port> [url]
```

The restore script injects the saved cookies via CDP `Network.setCookies`,
navigates, and reports whether the page is logged in. Continue driving the page
with the normal `browser` `act`/`navigate` tools. If it comes back showing the
login form, the session expired → redo the fill/save flow.

---

## 7. Adapting to a new site — checklist

Copy the two reference scripts and edit:

- [ ] `<site>_login.py` — from `insta_login.py`: field selectors, JSON path
      (`workspace/<site>.json`), cookies path (`<site>_cookies.json`), the
      `loggedIn` heuristic, and the submit-control selectors.
- [ ] `<site>_restore.py` — from `insta_restore.py`: cookie domain filter and
      default URL.
- [ ] Keep the **no-secrets-on-stdout** property; test `status` subcommand
      first and eyeball its output before running `fill`.
- [ ] Site quirks to expect: `div[role="button"]` instead of `<button>`
      submits; OTP inputs that auto-submit; login forms inside iframes (attach
      to the right CDP target); JS-driven validation that needs a real
      key-event sequence rather than `el.value = …`.

## File layout

```
workspace/browser_work/
  WEB_LOGIN_WORKFLOW.md      ← this file
  insta_login.py             ← reference login driver  (status|fill|code|submit|save)
  insta_restore.py           ← reference session restore
  insta_cookies.json         ← saved Instagram session (0600)
  README_insta_session.md    ← Instagram-specific notes
  <site>_login.py / <site>_restore.py / <site>_cookies.json   ← per new site
workspace/<site>.json        ← user-written credentials (0600, deletable)
```
