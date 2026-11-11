# Free Render hackathon demo

Deploy [Aryan-Bodhe/AvanZamp](https://github.com/Aryan-Bodhe/AvanZamp) as one Docker web service using `render.yaml`. `Dockerfile.hackathon` runs the existing UI, a separate SQLite workspace and one invoice worker together. The PostgreSQL/OIDC deployment in `Dockerfile` and `docs/PILOT.md` remains separate.

This is a shared judge workspace. Everyone with the demo login can see uploaded invoices, raw extraction artifacts, activity and procurement records, and can change records or approve invoices. Use synthetic invoices only. The UI displays **“Hackathon demo—data may reset.”** No payments are executed. Every hackathon startup ensures the synthetic Apple vendor and PO-1241 exist without replacing records judges have edited or replacing existing invoice history.

## Publish the source

The blueprint must exist on the GitHub branch Render deploys. Review and push the application changes, including any existing uncommitted application files they depend on, plus:

- `invoicedesk/interfaces/hackathon.py` and the local API/UI changes
- `Dockerfile.hackathon` and `Dockerfile.hackathon.dockerignore`
- `render.yaml`, this guide and the deployment tests

Do not commit `.env`, runtime databases, uploads or real provider recordings. The dedicated Docker ignore file allowlists application code, the pinned requirements, policy, and **only** `data/vendors.csv` and `data/purchase_orders.csv`. The image contains no invoice history, invoice PDFs, provider cache, migrations or credentials. Keep `Dockerfile.hackathon.dockerignore` beside its Dockerfile; Docker uses it instead of the production `.dockerignore` for this build.

## Create the service

1. Sign in at [Render](https://dashboard.render.com/) with GitHub. Authorize access to `Aryan-Bodhe/AvanZamp` (grant access to that repository if GitHub asks).
2. Choose **New → Blueprint**. Connect the repository and select the branch containing these files. Leave the blueprint path as `render.yaml` and the repository root directory empty.
3. Confirm there is exactly one **Web Service**, named `avanzamp-demo`, with Docker runtime, **Free** instance type, one instance, and `/healthz` as its health check. There is no database, persistent disk or separate worker to create. Do not select a paid plan.
4. Supply the prompted environment values below and create/deploy the blueprint. The initial origin can be `https://avanzamp-demo.onrender.com`.
5. Open the created service and copy its actual public HTTPS URL. If Render assigned a different subdomain, set `DEMO_PUBLIC_ORIGIN` to that exact origin under **Environment**, then **Save, rebuild, and deploy**. A mismatched hostname fails health checks until corrected. Do not add a trailing slash.
6. Wait for **Live**. If a deploy failed before the origin was corrected, use **Manual Deploy → Deploy latest commit** after saving the correction. Verify the service still uses the Free instance type in **Settings**.

The blueprint uses runtime environment variables marked `sync: false` for secrets; values are entered in Render and do not go in GitHub. Later edits to these values happen in the service's **Environment** page. Render supplies `PORT`; do not override it or add a Docker command override.

| Variable | Exact value or how to choose it |
| --- | --- |
| `DEMO_PUBLIC_ORIGIN` | Actual assigned origin, for example `https://avanzamp-demo.onrender.com`; HTTPS only, no path, port or trailing slash |
| `DEMO_USERNAME` | Your judge username, for example `judge`; letters, digits, `_`, `.`, `@`, `-` only |
| `DEMO_PASSWORD` | A newly generated random password of at least 16 characters; share privately with judges |
| `LLM_PROVIDER` | `openai` (blueprint default), or `anthropic` |
| `LLM_MODEL` | Exact model ID enabled for your provider account and supporting invoice/image extraction; for OpenAI, `gpt-4.1` is an example |
| `OPENAI_API_KEY` | Your OpenAI project key when using `openai`; set only as a Render secret |
| `ANTHROPIC_API_KEY` | Add this secret when selecting `anthropic`; the OpenAI key is then unused and can be removed |

The blueprint prompts for `OPENAI_API_KEY` by default. To deploy with Anthropic from the outset, change the provider value and replace the API-key entry's **name** in the blueprint before pushing; never put a key value there. Leave the model and key entries as `sync: false`.

The server refuses to start without an exact HTTPS origin, valid judge credentials, provider, model and the selected provider key. It makes no provider call at startup. Each accepted upload makes a fresh live extraction request, even when the document was processed previously. Actual provider calls use your provider's billing.

The demo image and blueprint set `LOG_TO_FILE=false` and `LOG_TO_CONSOLE=true`; Render collects console logs. If an earlier deployment fails with `PermissionError: /app/logs`, add these two values under **Environment** and save/redeploy. The non-root container cannot write to `/app`. Optional file logs, if explicitly enabled, default to `/tmp/invoicedesk-hackathon/logs` in the corrected image.

## Hosted acceptance

These checks must be performed against the actual HTTPS URL; offline tests do not establish hosted success. Record the deployed commit, URL, date and results privately, without credentials.

1. In a private browser window open `/healthz` without logging in. Expect `{"status":"ok"}` and no invoice information. Open `/invoices` and confirm the browser asks for a username/password. Wrong credentials fail. The unauthenticated API and `/assets/app.js` also require login.
2. Log in with the judge credentials. Confirm the reset notice is visible, invoice history is empty on a fresh deployment, and Vendors/Purchase orders contain the sample masters, including synthetic Apple Technologies Ltd. and PO-1241. The demo never imports your local SQLite history. Other vendors/POs can be added in the UI.
3. Upload the repository's synthetic `data/invoices/01_clean_exact.pdf` from your computer. Wait for completion. Open the expanded invoice details, source preview, extracted facts, evidence, checks, decision, timeline and Developer view. Confirm extraction reports the configured provider/model, fresh API provenance and no cache hit. A provider error is a failed acceptance check; inspect Render logs and the structured error, correct the key/model, and retry explicitly.
4. Confirm the clean invoice automatically becomes Approved and its PO shows accepted billing. Uploading the same invoice again must still extract freshly and must not double-post it.
5. Upload synthetic `data/invoices/04_over_tolerance.pdf` and confirm it requires review as expected by the sample data. Inspect the report, choose Approve, supply the required review reason/source confirmation, and confirm the audit entry and PO accepted amount update once. If procurement has already changed, use a fresh deployment for a repeatable run. Do not approve a source you have not checked.
6. Open the related PO's details. Compare accepted and remaining amounts with the approved invoices. The clean exact invoice from step 3 should fully consume PO-1001. For partial fulfillment on fresh data, upload `06_split_po_1of3.pdf`, `07_split_po_2of3.pdf`, then `08_split_po_3of3_overbill.pdf`: accepted partials consume the balance; the overbilling invoice must not post automatically. The PO display measures accepted billing, not physical receipt.
7. In browser developer tools inspect Config, HTML, JavaScript and API responses for provider keys or the demo password. Config returns only safe provider/model metadata and the notice. The browser will hold the Basic Authorization header used for login; never share network captures containing it. The provider key must never appear in responses.
8. Test a cross-origin mutation with a valid login: it must fail with 403. Mutations also require the exact `Origin` for non-browser clients. Invalid documents must still be rejected. An unexpected `Host` must fail validation. Do not relax host/origin checks to fix a wrong public-origin setting.
9. Use **Manual Deploy → Deploy latest commit** to replace the instance. A process restart on an intact filesystem preserves its workspace; if Render discards it, invoice history resets and the sample masters, including the synthetic Apple vendor/PO, are seeded again. Judge edits to existing records are kept across ordinary deploys.
10. Share only the HTTPS URL and judge credentials privately. Open the app shortly before judging so the service can wake.

## Runtime and limits

All demo state is under `/tmp/invoicedesk-hackathon`: SQLite, uploads, events, artifacts, ledger and extraction recordings. The app ensures sample procurement, including one explicitly synthetic Apple test PO, exists at startup; it does not process invoices or reuse recordings. The synthetic master is not real purchasing authorization. A worker lock prevents another process from owning the same workspace. Run exactly one instance and one Uvicorn worker.

Render terminates public TLS and forwards the request scheme to Uvicorn. The demo trusts proxy headers because its container is deployed behind Render's ingress; do not expose this command directly on an untrusted network. Application requests require HTTPS; the public health probe alone can use internal HTTP. Mutations require the configured HTTPS Origin, while upload size, content inspection, revisions, idempotency and backend approval checks remain active. Basic login has one shared identity and browser-managed credentials; close the private browser window to end a judging session. Production role separation remains in the OIDC deployment.

Render Free services sleep after 15 minutes without traffic and take about a minute to wake. Local files are lost on redeploy, restart or spin-down. Free service allowances are shared within the Render workspace (currently 750 instance hours per month), with bandwidth/build limits as well. Review usage and billing settings; free hosting within allowances does not make provider calls free. See [Render Free limits](https://render.com/docs/free), [Blueprint reference](https://render.com/docs/blueprint-spec) and [health checks](https://render.com/docs/health-checks).

## Offline verification

```powershell
.venv/Scripts/python.exe -m pytest -q
```

On POSIX use `.venv/bin/python -m pytest -q`. `tests/test_hackathon.py` covers authentication, health, HTTPS/host/origin rejection, configuration failure, upload validation, isolation from local history, intact-filesystem restart, empty-filesystem reseeding, single-worker ownership and Render's assigned port. All provider fixtures remain synthetic/offline. Docker build and hosted acceptance require a Docker host/Render account and are separate checks:

```sh
docker build -f Dockerfile.hackathon -t avanzamp-hackathon .
```

Do not pass secrets as build arguments. Runtime secrets belong in Render's environment settings.

### Verification performed on 2026-11-11

- Python 3.12 offline regression: **275 passed, 6 skipped**. The existing environment-dependent checks retain their skips.
- All **34** hackathon tests passed, including an authenticated HTTP/worker flow with explicitly synthetic provider facts, expanded reports, automatic/manual approval and PO fulfillment. A fresh-process startup regression also denies writes to the application log directory, reproducing the initial Render failure before the logging-default fix and passing afterwards.
- A separate temporary copy containing only the application files copied by the demo Dockerfile booted successfully with the worker, public health check, protected UI/assets, sample masters and empty history. No local database or provider recording was copied.
- `git diff --check` passed. Existing local workspace and production files were preserved.
- Docker is unavailable in the implementation environment, so the actual container build was not run. Render is not connected to this session; no service was published and hosted acceptance remains pending. No public URL is claimed by these results.
